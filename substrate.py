from contextlib import (
    contextmanager,
)
from copy import deepcopy
import json
import logging
import os
import subprocess
from time import sleep
import urlparse

from boto import ec2
from boto.exception import EC2ResponseError

import get_ami
from jujuconfig import (
    get_euca_env,
    translate_to_env,
)
from jujupy import (
    EnvJujuClient1X
)
from utility import (
    temp_dir,
    until_timeout,
)
import winazurearm


__metaclass__ = type


log = logging.getLogger("substrate")


LIBVIRT_DOMAIN_RUNNING = 'running'
LIBVIRT_DOMAIN_SHUT_OFF = 'shut off'


class StillProvisioning(Exception):
    """Attempted to terminate instances still provisioning."""

    def __init__(self, instance_ids):
        super(StillProvisioning, self).__init__(
            'Still provisioning: {}'.format(', '.join(instance_ids)))
        self.instance_ids = instance_ids


def terminate_instances(env, instance_ids):
    if len(instance_ids) == 0:
        log.info("No instances to delete.")
        return
    provider_type = env.config.get('type')
    environ = dict(os.environ)
    if provider_type == 'ec2':
        environ.update(get_euca_env(env.config))
        command_args = ['euca-terminate-instances'] + instance_ids
    elif provider_type in ('openstack', 'rackspace'):
        environ.update(translate_to_env(env.config))
        command_args = ['nova', 'delete'] + instance_ids
    elif provider_type == 'maas':
        with maas_account_from_config(env.config) as substrate:
            substrate.terminate_instances(instance_ids)
        return
    else:
        with make_substrate_manager(env.config,
                                    env.get_cloud_credentials()) as substrate:
            if substrate is None:
                raise ValueError(
                    "This test does not support the %s provider"
                    % provider_type)
            return substrate.terminate_instances(instance_ids)
    log.info("Deleting %s." % ', '.join(instance_ids))
    subprocess.check_call(command_args, env=environ)


class AWSAccount:
    """Represent the credentials of an AWS account."""

    @classmethod
    @contextmanager
    def manager_from_config(cls, config, region=None):
        """Create an AWSAccount from a juju environment dict."""
        euca_environ = get_euca_env(config)
        if region is None:
            region = config["region"]
        client = ec2.connect_to_region(
            region, aws_access_key_id=euca_environ['EC2_ACCESS_KEY'],
            aws_secret_access_key=euca_environ['EC2_SECRET_KEY'])
        yield cls(euca_environ, region, client)

    def __init__(self, euca_environ, region, client):
        self.euca_environ = euca_environ
        self.region = region
        self.client = client

    def iter_security_groups(self):
        """Iterate through security groups created by juju in this account.

        :return: an iterator of (group-id, group-name) tuples.
        """
        groups = self.client.get_all_security_groups(
            filters={'description': 'juju group'})
        for group in groups:
            yield group.id, group.name

    def iter_instance_security_groups(self, instance_ids=None):
        """List the security groups used by instances in this account.

        :param instance_ids: If supplied, list only security groups used by
            the specified instances.
        :return: an iterator of (group-id, group-name) tuples.
        """
        log.info('Listing security groups in use.')
        reservations = self.client.get_all_instances(instance_ids=instance_ids)
        for reservation in reservations:
            for instance in reservation.instances:
                for group in instance.groups:
                    yield group.id, group.name

    def destroy_security_groups(self, groups):
        """Destroy the specified security groups.

        :return: a list of groups that could not be destroyed.
        """
        failures = []
        for group in groups:
            deleted = self.client.delete_security_group(name=group)
            if not deleted:
                failures.append(group)
        return failures

    def delete_detached_interfaces(self, security_groups):
        """Delete detached network interfaces for supplied groups.

        :param security_groups: A collection of security_group ids.
        :return: A collection of security groups which still have interfaces in
            them.
        """
        interfaces = self.client.get_all_network_interfaces(
            filters={'status': 'available'})
        unclean = set()
        for interface in interfaces:
            for group in interface.groups:
                if group.id in security_groups:
                    try:
                        interface.delete()
                    except EC2ResponseError as e:
                        if e.error_code not in (
                                'InvalidNetworkInterface.InUse',
                                'InvalidNetworkInterfaceID.NotFound'):
                            raise
                        log.info(
                            'Failed to delete interface {!r}. {}'.format(
                                interface.id, e.message))
                        unclean.update(g.id for g in interface.groups)
                    break
        return unclean


class OpenStackAccount:
    """Represent the credentials/region of an OpenStack account."""

    def __init__(self, username, password, tenant_name, auth_url, region_name):
        self._username = username
        self._password = password
        self._tenant_name = tenant_name
        self._auth_url = auth_url
        self._region_name = region_name
        self._client = None

    @classmethod
    @contextmanager
    def manager_from_config(cls, config):
        """Create an OpenStackAccount from a juju environment dict."""
        yield cls(
            config['username'], config['password'], config['tenant-name'],
            config['auth-url'], config['region'])

    def get_client(self):
        """Return a novaclient Client for this account."""
        from novaclient import client
        return client.Client(
            '1.1', self._username, self._password, self._tenant_name,
            self._auth_url, region_name=self._region_name,
            service_type='compute', insecure=False)

    @property
    def client(self):
        """A novaclient Client for this account.  May come from cache."""
        if self._client is None:
            self._client = self.get_client()
        return self._client

    def iter_security_groups(self):
        """Iterate through security groups created by juju in this account.

        :return: an iterator of (group-id, group-name) tuples.
        """
        return ((g.id, g.name) for g in self.client.security_groups.list()
                if g.description == 'juju group')

    def iter_instance_security_groups(self, instance_ids=None):
        """List the security groups used by instances in this account.

        :param instance_ids: If supplied, list only security groups used by
            the specified instances.
        :return: an iterator of (group-id, group-name) tuples.
        """
        group_names = set()
        for server in self.client.servers.list():
            if instance_ids is not None and server.id not in instance_ids:
                continue
            # A server that errors before security groups are assigned will
            # have no security_groups attribute.
            groups = (getattr(server, 'security_groups', []))
            group_names.update(group['name'] for group in groups)
        return ((k, v) for k, v in self.iter_security_groups()
                if v in group_names)


class JoyentAccount:
    """Represent a Joyent account."""

    def __init__(self, client):
        self.client = client

    @classmethod
    @contextmanager
    def manager_from_config(cls, config):
        """Create a ContextManager for a JoyentAccount.

         Using a juju environment dict, the private key is written to a
         tmp file. Then, the Joyent client is inited with the path to the
         tmp key. The key is removed when done.
         """
        from joyent import Client
        with temp_dir() as key_dir:
            key_path = os.path.join(key_dir, 'joyent.key')
            open(key_path, 'w').write(config['private-key'])
            client = Client(
                config['sdc-url'], config['manta-user'],
                config['manta-key-id'], key_path, '')
            yield cls(client)

    def terminate_instances(self, instance_ids):
        """Terminate the specified instances."""
        provisioning = []
        for instance_id in instance_ids:
            machine_info = self.client._list_machines(instance_id)
            if machine_info['state'] == 'provisioning':
                provisioning.append(instance_id)
                continue
            self._terminate_instance(instance_id)
        if len(provisioning) > 0:
            raise StillProvisioning(provisioning)

    def _terminate_instance(self, machine_id):
        log.info('Stopping instance {}'.format(machine_id))
        self.client.stop_machine(machine_id)
        for ignored in until_timeout(30):
            stopping_machine = self.client._list_machines(machine_id)
            if stopping_machine['state'] == 'stopped':
                break
            sleep(3)
        else:
            raise Exception('Instance did not stop: {}'.format(machine_id))
        log.info('Terminating instance {}'.format(machine_id))
        self.client.delete_machine(machine_id)


def convert_to_azure_ids(client, instance_ids):
    """Return a list of ARM ids from a list juju machine instance-ids.

    The Juju 2 machine instance-id is not an ARM VM id, it is the non-unique
    machine name. For any juju controller, there are 2 or more machines named
    0. Using the client, the machine ids machine names can be found.

    See: https://bugs.launchpad.net/juju-core/+bug/1586089

    :param client: An EnvJujuClient instance.
    :param instance_ids: a list of Juju machine instance-ids
    :return: A list of ARM VM instance ids.
    """
    if isinstance(client, EnvJujuClient1X):
        # Juju 1.x reports the true vm instance-id.
        return instance_ids
    else:
        with AzureARMAccount.manager_from_config(
                client.env.config) as substrate:
            return substrate.convert_to_azure_ids(client, instance_ids)


class AzureARMAccount:
    """Represent an Azure ARM Account."""

    def __init__(self, arm_client):
        """Constructor.

        :param arm_client: An instance of winazurearm.ARMClient.
        """
        self.arm_client = arm_client

    @classmethod
    @contextmanager
    def manager_from_config(cls, config):
        """A context manager for a Azure RM account.

        In the case of the Juju 1x, the ARM keys must be in the env's config.
        subscription_id is the same. The PEM for the SMS is ignored.
        """
        arm_client = winazurearm.ARMClient(
            config['subscription-id'], config['application-id'],
            config['application-password'], config['tenant-id'])
        arm_client.init_services()
        yield cls(arm_client)

    def convert_to_azure_ids(self, client, instance_ids):
        if not instance_ids[0].startswith('machine'):
            log.info('Bug Lp 1586089 is fixed in {}.'.format(client.version))
            log.info('AzureARMAccount.convert_to_azure_ids can be deleted.')
            return instance_ids

        models = client.get_models()['models']
        model = [m for m in models if m['name'] == client.model_name][0]
        resource_group = 'juju-{}-model-{}'.format(
            model['name'], model['model-uuid'])
        resources = winazurearm.list_resources(
            self.arm_client, glob=resource_group, recursive=True)
        vm_ids = []
        for machine_name in instance_ids:
            rgd, vm = winazurearm.find_vm_instance(
                resources, machine_name, resource_group)
            vm_ids.append(vm.vm_id)
        return vm_ids

    def terminate_instances(self, instance_ids):
        """Terminate the specified instances."""
        for instance_id in instance_ids:
            winazurearm.delete_instance(
                self.arm_client, instance_id, resource_group=None)


class AzureAccount:
    """Represent an Azure Account."""

    def __init__(self, service_client):
        """Constructor.

        :param service_client: An instance of
            azure.servicemanagement.ServiceManagementService.
        """
        self.service_client = service_client

    @classmethod
    @contextmanager
    def manager_from_config(cls, config):
        """A context manager for a AzureAccount.

        It writes the certificate to a temp file because the Azure client
        library requires it, then deletes the temp file when done.
        """
        from azure.servicemanagement import ServiceManagementService
        with temp_dir() as cert_dir:
            cert_file = os.path.join(cert_dir, 'azure.pem')
            open(cert_file, 'w').write(config['management-certificate'])
            service_client = ServiceManagementService(
                config['management-subscription-id'], cert_file)
            yield cls(service_client)

    @staticmethod
    def convert_instance_ids(instance_ids):
        """Convert juju instance ids into Azure service/role names.

        Return a dict mapping service name to role names.
        """
        services = {}
        for instance_id in instance_ids:
            service, role = instance_id.rsplit('-', 1)
            services.setdefault(service, set()).add(role)
        return services

    @contextmanager
    def terminate_instances_cxt(self, instance_ids):
        """Terminate instances in a context.

        This context manager requests termination, then allows the "with"
        block to happen.  When the block is exited, it waits until the
        operations complete.

        The strategy for terminating instances varies depending on whether all
        roles are being terminated.  If all roles are being terminated, the
        deployment and hosted service are deleted.  If not all roles are being
        terminated, the roles themselves are deleted.
        """
        converted = self.convert_instance_ids(instance_ids)
        requests = set()
        services_to_delete = set(converted.keys())
        for service, roles in converted.items():
            properties = self.service_client.get_hosted_service_properties(
                service, embed_detail=True)
            for deployment in properties.deployments:
                role_names = set(
                    d_role.role_name for d_role in deployment.role_list)
                if role_names.difference(roles) == set():
                    requests.add(self.service_client.delete_deployment(
                        service, deployment.name))
                else:
                    services_to_delete.discard(service)
                    for role in roles:
                        requests.add(
                            self.service_client.delete_role(
                                service, deployment.name, role))
        yield
        self.block_on_requests(requests)
        for service in services_to_delete:
            self.service_client.delete_hosted_service(service)

    def block_on_requests(self, requests):
        """Wait until the requests complete."""
        requests = set(requests)
        while len(requests) > 0:
            for request in list(requests):
                op = self.service_client.get_operation_status(
                    request.request_id)
                if op.status == 'Succeeded':
                    requests.remove(request)

    def terminate_instances(self, instance_ids):
        """Terminate the specified instances.

        See terminate_instances_cxt for details.
        """
        with self.terminate_instances_cxt(instance_ids):
            return


class MAASAccount:
    """Represent a MAAS 2.0 account."""

    _API_PATH = 'api/2.0/'

    def __init__(self, profile, url, oauth):
        self.profile = profile
        self.url = urlparse.urljoin(url, self._API_PATH)
        self.oauth = oauth

    def _maas(self, *args):
        """Call maas api with given arguments and parse json result."""
        output = subprocess.check_output(('maas',) + args)
        if not output:
            return None
        return json.loads(output)

    def login(self):
        """Login with the maas cli."""
        subprocess.check_call([
            'maas', 'login', self.profile, self.url, self.oauth])

    def logout(self):
        """Logout with the maas cli."""
        subprocess.check_call(['maas', 'logout', self.profile])

    def _machine_release_args(self, machine_id):
        return (self.profile, 'machine', 'release', machine_id)

    def terminate_instances(self, instance_ids):
        """Terminate the specified instances."""
        for instance in instance_ids:
            maas_system_id = instance.split('/')[5]
            log.info('Deleting %s.' % instance)
            self._maas(*self._machine_release_args(maas_system_id))

    def _list_allocated_args(self):
        return (self.profile, 'machines', 'list-allocated')

    def get_allocated_nodes(self):
        """Return a dict of allocated nodes with the hostname as keys."""
        nodes = self._maas(*self._list_allocated_args())
        allocated = {node['hostname']: node for node in nodes}
        return allocated

    def get_allocated_ips(self):
        """Return a dict of allocated ips with the hostname as keys.

        A maas node may have many ips. The method selects the first ip which
        is the address used for virsh access and ssh.
        """
        allocated = self.get_allocated_nodes()
        ips = {k: v['ip_addresses'][0] for k, v in allocated.items()
               if v['ip_addresses']}
        return ips

    def fabrics(self):
        """Return list of all fabrics."""
        return self._maas(self.profile, 'fabrics', 'read')

    def create_fabric(self, name, class_type=None):
        """Create a new fabric."""
        args = [self.profile, 'fabrics', 'create', 'name=' + name]
        if class_type is not None:
            args.append('class_type=' + class_type)
        return self._maas(*args)

    def delete_fabric(self, fabric_id):
        """Delete a fabric with given id."""
        return self._maas(self.profile, 'fabric', 'delete', str(fabric_id))

    def spaces(self):
        """Return list of all spaces."""
        return self._maas(self.profile, 'spaces', 'read')

    def create_space(self, name):
        """Create a new space with given name."""
        return self._maas(self.profile, 'spaces', 'create', 'name=' + name)

    def delete_space(self, space_id):
        """Delete a space with given id."""
        return self._maas(self.profile, 'space', 'delete', str(space_id))

    def create_vlan(self, fabric_id, vid, name=None):
        """Create a new vlan on fabric with given fabric_id."""
        args = [
            self.profile, 'vlans', 'create', str(fabric_id), 'vid=' + str(vid),
            ]
        if name is not None:
            args.append('name=' + name)
        return self._maas(*args)

    def delete_vlan(self, fabric_id, vid):
        """Delete a vlan on given fabric_id with vid."""
        return self._maas(
            self.profile, 'vlan', 'delete', str(fabric_id), str(vid))

    def interfaces(self, system_id):
        """Return list of interfaces belonging to node with given system_id."""
        return self._maas(self.profile, 'interfaces', 'read', system_id)

    def interface_create_vlan(self, system_id, parent, vlan_id):
        """Create a vlan interface on machine with given system_id."""
        args = [
            self.profile, 'interfaces', 'create-vlan', system_id,
            'parent=' + str(parent), 'vlan=' + str(vlan_id),
        ]
        # TODO(gz): Add support for optional parameters as needed.
        return self._maas(*args)

    def delete_interface(self, system_id, interface_id):
        """Delete interface on node with given system_id with interface_id."""
        return self._maas(
            self.profile, 'interface', 'delete', system_id, str(interface_id))


class MAAS1Account(MAASAccount):
    """Represent a MAAS 1.X account."""

    _API_PATH = 'api/1.0/'

    def _list_allocated_args(self):
        return (self.profile, 'nodes', 'list-allocated')

    def _machine_release_args(self, machine_id):
        return (self.profile, 'node', 'release', machine_id)


@contextmanager
def maas_account_from_config(config):
    """Create a ContextManager for either a MAASAccount or a MAAS1Account.

    As it's not possible to tell from the maas config which version of the api
    to use, try 2.0 and if that fails on login fallback to 1.0 instead.
    """
    args = (config['name'], config['maas-server'], config['maas-oauth'])
    manager = MAASAccount(*args)
    try:
        manager.login()
    except subprocess.CalledProcessError:
        log.info("Could not login with MAAS 2.0 API, trying 1.0")
        manager = MAAS1Account(*args)
        manager.login()
    yield manager
    manager.logout()


class LXDAccount:
    """Represent a LXD account."""

    def __init__(self, remote=None):
        self.remote = remote

    @classmethod
    @contextmanager
    def manager_from_config(cls, config):
        """Create a ContextManager for a LXDAccount."""
        remote = config.get('region', None)
        yield cls(remote=remote)

    def terminate_instances(self, instance_ids):
        """Terminate the specified instances."""
        for instance_id in instance_ids:
            subprocess.check_call(['lxc', 'stop', '--force', instance_id])
            if self.remote:
                instance_id = '{}:{}'.format(self.remote, instance_id)
            subprocess.check_call(['lxc', 'delete', '--force', instance_id])


@contextmanager
def make_substrate_manager(config, credentials):
    """A ContextManager that returns an Account for the config's substrate.

    Returns None if the substrate is not supported.
    """
    config = deepcopy(config)
    config.update(credentials)
    substrate_factory = {
        'ec2': AWSAccount.manager_from_config,
        'openstack': OpenStackAccount.manager_from_config,
        'rackspace': OpenStackAccount.manager_from_config,
        'joyent': JoyentAccount.manager_from_config,
        'azure': AzureAccount.manager_from_config,
        'azure-arm': AzureARMAccount.manager_from_config,
        'lxd': LXDAccount.manager_from_config,
    }
    substrate_type = config['type']
    if substrate_type == 'azure' and 'application-id' in config:
        substrate_type = 'azure-arm'
    factory = substrate_factory.get(substrate_type)
    if factory is None:
        yield None
    else:
        with factory(config) as substrate:
            yield substrate


def start_libvirt_domain(uri, domain):
    """Call virsh to start the domain.

    @Parms URI: The address of the libvirt service.
    @Parm domain: The name of the domain.
    """

    command = ['virsh', '-c', uri, 'start', domain]
    try:
        subprocess.check_output(command, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        if 'already active' in e.output:
            return '%s is already running; nothing to do.' % domain
        raise Exception('%s failed:\n %s' % (command, e.output))
    sleep(30)
    for ignored in until_timeout(120):
        if verify_libvirt_domain(uri, domain, LIBVIRT_DOMAIN_RUNNING):
            return "%s is now running" % domain
        sleep(2)
    raise Exception('libvirt domain %s did not start.' % domain)


def stop_libvirt_domain(uri, domain):
    """Call virsh to shutdown the domain.

    @Parms URI: The address of the libvirt service.
    @Parm domain: The name of the domain.
    """

    command = ['virsh', '-c', uri, 'shutdown', domain]
    try:
        subprocess.check_output(command, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        if 'domain is not running' in e.output:
            return ('%s is not running; nothing to do.' % domain)
        raise Exception('%s failed:\n %s' % (command, e.output))
    sleep(30)
    for ignored in until_timeout(120):
        if verify_libvirt_domain(uri, domain, LIBVIRT_DOMAIN_SHUT_OFF):
            return "%s is now shut off" % domain
        sleep(2)
    raise Exception('libvirt domain %s is not shut off.' % domain)


def verify_libvirt_domain(uri, domain, state=LIBVIRT_DOMAIN_RUNNING):
    """Returns a bool based on if the domain is in the given state.

    @Parms URI: The address of the libvirt service.
    @Parm domain: The name of the domain.
    @Parm state: The state to verify (e.g. "running or "shut off").
    """

    dom_status = get_libvirt_domstate(uri, domain)
    return state in dom_status


def get_libvirt_domstate(uri, domain):
    """Call virsh to get the state of the given domain.

    @Parms URI: The address of the libvirt service.
    @Parm domain: The name of the domain.
    """

    command = ['virsh', '-c', uri, 'domstate', domain]
    try:
        sub_output = subprocess.check_output(command)
    except subprocess.CalledProcessError:
        raise Exception('%s failed' % command)
    return sub_output


def parse_euca(euca_output):
    for line in euca_output.splitlines():
        fields = line.split('\t')
        if fields[0] != 'INSTANCE':
            continue
        yield fields[1], fields[3]


def run_instances(count, job_name, series, region=None):
    """create a number of instances in ec2 and tag them.

    :param count: The number of instances to create.
    :param job_name: The name of job that owns the instances (used as a tag).
    :param series: The series to run in the instance.
        If None, Precise will be used.
    """
    if series is None:
        series = 'precise'
    environ = dict(os.environ)
    ami = get_ami.query_ami(series, "amd64", region=region)
    command = [
        'euca-run-instances', '-k', 'id_rsa', '-n', '%d' % count,
        '-t', 'm3.large', '-g', 'manual-juju-test', ami]
    run_output = subprocess.check_output(command, env=environ).strip()
    machine_ids = dict(parse_euca(run_output)).keys()
    for remaining in until_timeout(300):
        try:
            names = dict(describe_instances(machine_ids, env=environ))
            if '' not in names.values():
                subprocess.check_call(
                    ['euca-create-tags', '--tag', 'job_name=%s' % job_name] +
                    machine_ids, env=environ)
                return names.items()
        except subprocess.CalledProcessError:
            subprocess.call(['euca-terminate-instances'] + machine_ids)
            raise
        sleep(1)


def describe_instances(instances=None, running=False, job_name=None,
                       env=None):
    command = ['euca-describe-instances']
    if job_name is not None:
        command.extend(['--filter', 'tag:job_name=%s' % job_name])
    if running:
        command.extend(['--filter', 'instance-state-name=running'])
    if instances is not None:
        command.extend(instances)
    log.info(' '.join(command))
    return parse_euca(subprocess.check_output(command, env=env))


def get_job_instances(job_name):
    description = describe_instances(job_name=job_name, running=True)
    return (machine_id for machine_id, name in description)


def destroy_job_instances(job_name):
    instances = list(get_job_instances(job_name))
    if len(instances) == 0:
        return
    subprocess.check_call(['euca-terminate-instances'] + instances)


def resolve_remote_dns_names(env, remote_machines):
    """Update addresses of given remote_machines as needed by providers."""
    if env.config['type'] != 'maas':
        # Only MAAS requires special handling at prsent.
        return
    # MAAS hostnames are not resolvable, but we can adapt them to IPs.
    with maas_account_from_config(env.config) as account:
        allocated_ips = account.get_allocated_ips()
    for remote in remote_machines:
        if remote.get_address() in allocated_ips:
            remote.update_address(allocated_ips[remote.address])
