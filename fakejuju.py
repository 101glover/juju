from argparse import ArgumentParser
from base64 import b64encode
from contextlib import contextmanager
import copy
from hashlib import sha512
from itertools import count
import json
import logging
import subprocess

import yaml

from jujupy import (
    EnvJujuClient,
    JESNotSupported,
    JujuData,
    SoftDeadlineExceeded,
)

__metaclass__ = type


def check_juju_output(func):
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if 'service' in result:
            raise AssertionError('Result contained service')
        return result
    return wrapper


class ControllerOperation(Exception):

    def __init__(self, operation):
        super(ControllerOperation, self).__init__(
            'Operation "{}" is only valid on controller models.'.format(
                operation))


def assert_juju_call(test_case, mock_method, client, expected_args,
                     call_index=None):
    if call_index is None:
        test_case.assertEqual(len(mock_method.mock_calls), 1)
        call_index = 0
    empty, args, kwargs = mock_method.mock_calls[call_index]
    test_case.assertEqual(args, (expected_args,))


class FakeControllerState:

    def __init__(self):
        self.name = 'name'
        self.state = 'not-bootstrapped'
        self.models = {}
        self.users = {
            'admin': {
                'state': '',
                'permission': 'write'
            }
        }
        self.shares = ['admin']

    def add_model(self, name):
        state = FakeEnvironmentState(self)
        state.name = name
        self.models[name] = state
        state.controller.state = 'created'
        return state

    def require_controller(self, operation, name):
        if name != self.controller_model.name:
            raise ControllerOperation(operation)

    def grant(self, username, permission):
        model_permissions = ['read', 'write', 'admin']
        if permission in model_permissions:
            permission = 'login'
        self.users[username]['access'] = permission

    def add_user_perms(self, username, permissions):
        self.users.update(
            {username: {'state': '', 'permission': permissions}})
        self.shares.append(username)

    def bootstrap(self, model_name, config, separate_controller):
        default_model = self.add_model(model_name)
        default_model.name = model_name
        if separate_controller:
            controller_model = default_model.controller.add_model('controller')
        else:
            controller_model = default_model
        self.controller_model = controller_model
        controller_model.state_servers.append(controller_model.add_machine())
        self.state = 'bootstrapped'
        default_model.model_config = copy.deepcopy(config)
        self.models[default_model.name] = default_model
        return default_model


class FakeEnvironmentState:
    """A Fake environment state that can be used by multiple FakeBackends."""

    def __init__(self, controller=None):
        self._clear()
        if controller is not None:
            self.controller = controller
        else:
            self.controller = FakeControllerState()

    def _clear(self):
        self.name = None
        self.machine_id_iter = count()
        self.state_servers = []
        self.services = {}
        self.machines = set()
        self.containers = {}
        self.relations = {}
        self.token = None
        self.exposed = set()
        self.machine_host_names = {}
        self.current_bundle = None
        self.model_config = None
        self.ssh_keys = []

    @property
    def state(self):
        return self.controller.state

    def add_machine(self, host_name=None, machine_id=None):
        if machine_id is None:
            machine_id = str(self.machine_id_iter.next())
        self.machines.add(machine_id)
        if host_name is None:
            host_name = '{}.example.com'.format(machine_id)
        self.machine_host_names[machine_id] = host_name
        return machine_id

    def add_ssh_machines(self, machines):
        for machine in machines:
            self.add_machine()

    def add_container(self, container_type, host=None, container_num=None):
        if host is None:
            host = self.add_machine()
        host_containers = self.containers.setdefault(host, set())
        if container_num is None:
            same_type_containers = [x for x in host_containers if
                                    container_type in x]
            container_num = len(same_type_containers)
        container_name = '{}/{}/{}'.format(host, container_type, container_num)
        host_containers.add(container_name)
        host_name = '{}.example.com'.format(container_name)
        self.machine_host_names[container_name] = host_name

    def remove_container(self, container_id):
        for containers in self.containers.values():
            containers.discard(container_id)

    def remove_machine(self, machine_id):
        self.machines.remove(machine_id)
        self.containers.pop(machine_id, None)

    def remove_state_server(self, machine_id):
        self.remove_machine(machine_id)
        self.state_servers.remove(machine_id)

    def destroy_environment(self):
        self._clear()
        self.controller.state = 'destroyed'
        return 0

    def kill_controller(self):
        self._clear()
        self.controller.state = 'controller-killed'

    def destroy_model(self):
        del self.controller.models[self.name]
        self._clear()
        self.controller.state = 'model-destroyed'

    def _fail_stderr(self, message, returncode=1, cmd='juju', stdout=''):
        exc = subprocess.CalledProcessError(returncode, cmd, stdout)
        exc.stderr = message
        raise exc

    def restore_backup(self):
        self.controller.require_controller('restore', self.name)
        if len(self.state_servers) > 0:
            return self._fail_stderr('Operation not permitted')
        self.state_servers.append(self.add_machine())

    def enable_ha(self):
        self.controller.require_controller('enable-ha', self.name)
        for n in range(2):
            self.state_servers.append(self.add_machine())

    def deploy(self, charm_name, service_name):
        self.add_unit(service_name)

    def deploy_bundle(self, bundle_path):
        self.current_bundle = bundle_path

    def add_unit(self, service_name):
        machines = self.services.setdefault(service_name, set())
        machines.add(
            ('{}/{}'.format(service_name, str(len(machines))),
             self.add_machine()))

    def remove_unit(self, to_remove):
        for units in self.services.values():
            for unit_id, machine_id in units:
                if unit_id == to_remove:
                    self.remove_machine(machine_id)
                    units.remove((unit_id, machine_id))
                    break

    def destroy_service(self, service_name):
        for unit, machine_id in self.services.pop(service_name):
            self.remove_machine(machine_id)

    def get_status_dict(self):
        machines = {}
        for machine_id in self.machines:
            machine_dict = {'juju-status': {'current': 'idle'}}
            hostname = self.machine_host_names.get(machine_id)
            machine_dict['instance-id'] = machine_id
            if hostname is not None:
                machine_dict['dns-name'] = hostname
            machines[machine_id] = machine_dict
            if machine_id in self.state_servers:
                machine_dict['controller-member-status'] = 'has-vote'
        for host, containers in self.containers.items():
            container_dict = dict((c, {}) for c in containers)
            for container in containers:
                dns_name = self.machine_host_names.get(container)
                if dns_name is not None:
                    container_dict[container]['dns-name'] = dns_name

            machines[host]['containers'] = container_dict
        services = {}
        for service, units in self.services.items():
            unit_map = {}
            for unit_id, machine_id in units:
                unit_map[unit_id] = {
                    'machine': machine_id,
                    'juju-status': {'current': 'idle'}}
            services[service] = {
                'units': unit_map,
                'relations': self.relations.get(service, {}),
                'exposed': service in self.exposed,
                }
        return {'machines': machines, 'applications': services}

    def add_ssh_key(self, keys_to_add):
        errors = []
        for key in keys_to_add:
            if not key.startswith("ssh-rsa "):
                errors.append(
                    'cannot add key "{0}": invalid ssh key: {0}'.format(key))
            elif key in self.ssh_keys:
                errors.append(
                    'cannot add key "{0}": duplicate ssh key: {0}'.format(key))
            else:
                self.ssh_keys.append(key)
        return '\n'.join(errors)

    def remove_ssh_key(self, keys_to_remove):
        errors = []
        for i in reversed(range(len(keys_to_remove))):
            key = keys_to_remove[i]
            if key in ('juju-client-key', 'juju-system-key'):
                keys_to_remove = keys_to_remove[:i] + keys_to_remove[i + 1:]
                errors.append(
                    'cannot remove key id "{0}": may not delete internal key:'
                    ' {0}'.format(key))
        for i in range(len(self.ssh_keys)):
            if self.ssh_keys[i] in keys_to_remove:
                keys_to_remove.remove(self.ssh_keys[i])
                del self.ssh_keys[i]
        errors.extend(
            'cannot remove key id "{0}": invalid ssh key: {0}'.format(key)
            for key in keys_to_remove)
        return '\n'.join(errors)

    def import_ssh_key(self, names_to_add):
        for name in names_to_add:
            self.ssh_keys.append('ssh-rsa FAKE_KEY a key {}'.format(name))
        return ""


class FakeExpectChild:

    def __init__(self, backend, juju_home, extra_env):
        self.backend = backend
        self.juju_home = juju_home
        self.extra_env = extra_env
        self.last_expect = None
        self.exitstatus = None

    def expect(self, line):
        self.last_expect = line

    def sendline(self, line):
        """Do-nothing implementation of sendline.

        Subclassess will likely override this.
        """

    def close(self):
        self.exitstatus = 0

    def isalive(self):
        return bool(self.exitstatus is not None)


class AutoloadCredentials(FakeExpectChild):

    def __init__(self, backend, juju_home, extra_env):
        super(AutoloadCredentials, self).__init__(backend, juju_home,
                                                  extra_env)
        self.cloud = None

    def sendline(self, line):
        if self.last_expect == (
                '(Select the cloud it belongs to|'
                'Enter cloud to which the credential).* Q to quit.*'):
            self.cloud = line

    def isalive(self):
        juju_data = JujuData('foo', juju_home=self.juju_home)
        juju_data.load_yaml()
        creds = juju_data.credentials.setdefault('credentials', {})
        creds.update({self.cloud: {
            'default-region': self.extra_env['OS_REGION_NAME'],
            self.extra_env['OS_USERNAME']: {
                'domain-name': '',
                'auth-type': 'userpass',
                'username': self.extra_env['OS_USERNAME'],
                'password': self.extra_env['OS_PASSWORD'],
                'tenant-name': self.extra_env['OS_TENANT_NAME'],
                }}})
        juju_data.dump_yaml(self.juju_home, {})
        return False


class FakeBackend:
    """A fake juju backend for tests.

    This is a partial implementation, but should be suitable for many uses,
    and can be extended.

    The state is provided by controller_state, so that multiple clients and
    backends can manipulate the same state.
    """

    def __init__(self, controller_state, feature_flags=None, version=None,
                 full_path=None, debug=False, past_deadline=False):
        assert isinstance(controller_state, FakeControllerState)
        self.controller_state = controller_state
        if feature_flags is None:
            feature_flags = set()
        self.feature_flags = feature_flags
        self.version = version
        self.full_path = full_path
        self.debug = debug
        self.juju_timings = {}
        self.log = logging.getLogger('jujupy')
        self._past_deadline = past_deadline
        self._ignore_soft_deadline = False

    def clone(self, full_path=None, version=None, debug=None,
              feature_flags=None):
        if version is None:
            version = self.version
        if full_path is None:
            full_path = self.full_path
        if debug is None:
            debug = self.debug
        if feature_flags is None:
            feature_flags = set(self.feature_flags)
        controller_state = self.controller_state
        return self.__class__(controller_state, feature_flags, version,
                              full_path, debug,
                              past_deadline=self._past_deadline)

    def set_feature(self, feature, enabled):
        if enabled:
            self.feature_flags.add(feature)
        else:
            self.feature_flags.discard(feature)

    def is_feature_enabled(self, feature):
        if feature == 'jes':
            return True
        return bool(feature in self.feature_flags)

    @contextmanager
    def ignore_soft_deadline(self):
        """Ignore the client deadline.  For cleanup code."""
        old_val = self._ignore_soft_deadline
        self._ignore_soft_deadline = True
        try:
            yield
        finally:
            self._ignore_soft_deadline = old_val

    @contextmanager
    def _check_timeouts(self):
        try:
            yield
        finally:
            if self._past_deadline and not self._ignore_soft_deadline:
                raise SoftDeadlineExceeded()

    def deploy(self, model_state, charm_name, service_name=None, series=None):
        if service_name is None:
            service_name = charm_name.split(':')[-1].split('/')[-1]
        model_state.deploy(charm_name, service_name)

    def bootstrap(self, args):
        parser = ArgumentParser()
        parser.add_argument('cloud_name_region')
        parser.add_argument('controller_name')
        parser.add_argument('--constraints')
        parser.add_argument('--config')
        parser.add_argument('--default-model')
        parser.add_argument('--agent-version')
        parser.add_argument('--bootstrap-series')
        parser.add_argument('--upload-tools', action='store_true')
        parsed = parser.parse_args(args)
        with open(parsed.config) as config_file:
            config = yaml.safe_load(config_file)
        cloud_region = parsed.cloud_name_region.split('/', 1)
        cloud = cloud_region[0]
        # Although they are specified with specific arguments instead of as
        # config, these values are listed by model-config:
        # name, region, type (from cloud).
        config['type'] = cloud
        if len(cloud_region) > 1:
            config['region'] = cloud_region[1]
        config['name'] = parsed.default_model
        if parsed.bootstrap_series is not None:
            config['default-series'] = parsed.bootstrap_series
        self.controller_state.bootstrap(parsed.default_model, config,
                                        self.is_feature_enabled('jes'))

    def quickstart(self, model_name, config, bundle):
        default_model = self.controller_state.bootstrap(
            model_name, config, self.is_feature_enabled('jes'))
        default_model.deploy_bundle(bundle)

    def destroy_environment(self, model_name):
        try:
            state = self.controller_state.models[model_name]
        except KeyError:
            return 0
        state.destroy_environment()
        return 0

    def add_machines(self, model_state, args):
        if len(args) == 0:
            return model_state.add_machine()
        ssh_machines = [a[4:] for a in args if a.startswith('ssh:')]
        if len(ssh_machines) == len(args):
            return model_state.add_ssh_machines(ssh_machines)
        parser = ArgumentParser()
        parser.add_argument('host_placement', nargs='*')
        parser.add_argument('-n', type=int, dest='count', default='1')
        parsed = parser.parse_args(args)
        if len(parsed.host_placement) == 1:
            split = parsed.host_placement[0].split(':')
            if len(split) == 1:
                container_type = split[0]
                host = None
            else:
                container_type, host = split
            for x in range(parsed.count):
                model_state.add_container(container_type, host=host)
        else:
            for x in range(parsed.count):
                model_state.add_machine()

    def get_controller_model_name(self):
        return self.controller_state.controller_model.name

    def make_controller_dict(self, controller_name):
        controller_model = self.controller_state.controller_model
        server_id = list(controller_model.state_servers)[0]
        server_hostname = controller_model.machine_host_names[server_id]
        api_endpoint = '{}:23'.format(server_hostname)
        return {controller_name: {'details': {'api-endpoints': [
            api_endpoint]}}}

    def list_models(self):
        model_names = [state.name for state in
                       self.controller_state.models.values()]
        return {'models': [{'name': n} for n in model_names]}

    def list_users(self):
        user_names = [name for name in
                      self.controller_state.users.keys()]
        user_list = []
        for n in user_names:
            if n == 'admin':
                append_dict = {'access': 'superuser', 'user-name': n,
                               'display-name': n}
            else:
                access = self.controller_state.users[n]['access']
                append_dict = {
                    'access': access, 'user-name': n}
            user_list.append(append_dict)
        return user_list

    def show_user(self, user_name):
        if user_name is None:
            raise Exception("No user specified")
        if user_name == 'admin':
            user_status = {'access': 'superuser', 'user-name': user_name,
                           'display-name': user_name}
        else:
            user_status = {'user-name': user_name, 'display-name': ''}
        return user_status

    def get_users(self):
        share_names = self.controller_state.shares
        permissions = []
        for key, value in self.controller_state.users.iteritems():
            if key in share_names:
                permissions.append(value['permission'])
        share_list = {}
        for i, (share_name, permission) in enumerate(
                zip(share_names, permissions)):
            share_list[share_name] = {'display-name': share_name,
                                      'access': permission}
            if share_name != 'admin':
                share_list[share_name].pop('display-name')
            else:
                share_list[share_name]['access'] = 'admin'
        return share_list

    def show_model(self):
        # To get data from the model we would need:
        # self.controller_state.current_model
        model_name = 'name'
        data = {
            'name': model_name,
            'owner': 'admin',
            'life': 'alive',
            'status': {'current': 'available', 'since': '15 minutes ago'},
            'users': self.get_users(),
            }
        return {model_name: data}

    def _log_command(self, command, args, model, level=logging.INFO):
        full_args = ['juju', command]
        if model is not None:
            full_args.extend(['-m', model])
        full_args.extend(args)
        self.log.log(level, ' '.join(full_args))

    def juju(self, command, args, used_feature_flags,
             juju_home, model=None, check=True, timeout=None, extra_env=None):
        if 'service' in command:
            raise Exception('Command names must not contain "service".')
        if isinstance(args, basestring):
            args = (args,)
        self._log_command(command, args, model)
        if model is not None:
            if ':' in model:
                model = model.split(':')[1]
            model_state = self.controller_state.models[model]
            if ((command, args[:1]) == ('set-config', ('dummy-source',)) or
                    (command, args[:1]) == ('config', ('dummy-source',))):
                name, value = args[1].split('=')
                if name == 'token':
                    model_state.token = value
            if command == 'deploy':
                parser = ArgumentParser()
                parser.add_argument('charm_name')
                parser.add_argument('service_name', nargs='?')
                parser.add_argument('--to')
                parser.add_argument('--series')
                parsed = parser.parse_args(args)
                self.deploy(model_state, parsed.charm_name,
                            parsed.service_name, parsed.series)
            if command == 'remove-application':
                model_state.destroy_service(*args)
            if command == 'add-relation':
                if args[0] == 'dummy-source':
                    model_state.relations[args[1]] = {'source': [args[0]]}
            if command == 'expose':
                (service,) = args
                model_state.exposed.add(service)
            if command == 'unexpose':
                (service,) = args
                model_state.exposed.remove(service)
            if command == 'add-unit':
                (service,) = args
                model_state.add_unit(service)
            if command == 'remove-unit':
                (unit_id,) = args
                model_state.remove_unit(unit_id)
            if command == 'add-machine':
                return self.add_machines(model_state, args)
            if command == 'remove-machine':
                parser = ArgumentParser()
                parser.add_argument('machine_id')
                parser.add_argument('--force', action='store_true')
                parsed = parser.parse_args(args)
                machine_id = parsed.machine_id
                if '/' in machine_id:
                    model_state.remove_container(machine_id)
                else:
                    model_state.remove_machine(machine_id)
            if command == 'quickstart':
                parser = ArgumentParser()
                parser.add_argument('--constraints')
                parser.add_argument('--no-browser', action='store_true')
                parser.add_argument('bundle')
                parsed = parser.parse_args(args)
                # Released quickstart doesn't seem to provide the config via
                # the commandline.
                self.quickstart(model, {}, parsed.bundle)
        else:
            if command == 'bootstrap':
                self.bootstrap(args)
            if command == 'kill-controller':
                if self.controller_state.state == 'not-bootstrapped':
                    return
                model = args[0]
                model_state = self.controller_state.models[model]
                model_state.kill_controller()
            if command == 'destroy-model':
                if not self.is_feature_enabled('jes'):
                    raise JESNotSupported()
                model = args[0]
                model_state = self.controller_state.models[model]
                model_state.destroy_model()
            if command == 'enable-ha':
                parser = ArgumentParser()
                parser.add_argument('-n', '--number')
                parser.add_argument('-c', '--controller')
                parsed = parser.parse_args(args)
                if not self.controller_state.name == parsed.controller:
                    raise AssertionError('Test does not setup controller name')
                model_state = self.controller_state.controller_model
                model_state.enable_ha()
            if command == 'add-model':
                if not self.is_feature_enabled('jes'):
                    raise JESNotSupported()
                parser = ArgumentParser()
                parser.add_argument('-c', '--controller')
                parser.add_argument('--config')
                parser.add_argument('model_name')
                parsed = parser.parse_args(args)
                self.controller_state.add_model(parsed.model_name)
            if command == 'revoke':
                user_name = args[2]
                permissions = args[3]
                per = self.controller_state.users[user_name]['permission']
                if per == permissions:
                    if permissions == 'read':
                        self.controller_state.shares.remove(user_name)
                        per = ''
                    else:
                        per = 'read'
            if command == 'grant':
                username = args[0]
                permission = args[1]
                self.controller_state.grant(username, permission)
            if command == 'remove-user':
                username = args[0]
                self.controller_state.users.pop(username)
                if username in self.controller_state.shares:
                    self.controller_state.shares.remove(username)
            if command == 'restore-backup':
                model_state.restore_backup()

    @contextmanager
    def juju_async(self, command, args, used_feature_flags,
                   juju_home, model=None, timeout=None):
        yield
        self.juju(command, args, used_feature_flags,
                  juju_home, model, timeout=timeout)

    @check_juju_output
    def get_juju_output(self, command, args, used_feature_flags, juju_home,
                        model=None, timeout=None, user_name=None,
                        merge_stderr=False):
        if 'service' in command:
            raise Exception('No service')
        with self._check_timeouts():
            self._log_command(command, args, model, logging.DEBUG)
            if model is not None:
                if ':' in model:
                    model = model.split(':')[1]
                model_state = self.controller_state.models[model]
            from deploy_stack import GET_TOKEN_SCRIPT
            if (command, args) == ('ssh', ('dummy-sink/0', GET_TOKEN_SCRIPT)):
                return model_state.token
            if (command, args) == ('ssh', ('0', 'lsb_release', '-c')):
                return 'Codename:\t{}\n'.format(
                    model_state.model_config['default-series'])
            if command in ('model-config', 'get-model-config'):
                return yaml.safe_dump(model_state.model_config)
            if command == 'show-controller':
                return yaml.safe_dump(self.make_controller_dict(args[0]))
            if command == 'list-models':
                return yaml.safe_dump(self.list_models())
            if command == 'list-users':
                return json.dumps(self.list_users())
            if command == 'show-model':
                return json.dumps(self.show_model())
            if command == 'show-user':
                return json.dumps(self.show_user(user_name))
            if command == 'add-user':
                permissions = 'read'
                if set(["--acl", "write"]).issubset(args):
                    permissions = 'write'
                username = args[0]
                info_string = 'User "{}" added\n'.format(username)
                self.controller_state.add_user_perms(username, permissions)
                register_string = get_user_register_command_info(username)
                return info_string + register_string
            if command == 'show-status':
                status_dict = model_state.get_status_dict()
                # Parsing JSON is much faster than parsing YAML, and JSON is a
                # subset of YAML, so emit JSON.
                return json.dumps(status_dict)
            if command == 'create-backup':
                self.controller_state.require_controller('backup', model)
                return 'juju-backup-0.tar.gz'
            if command == 'ssh-keys':
                lines = ['Keys used in model: ' + model_state.name]
                if '--full' in args:
                    lines.extend(model_state.ssh_keys)
                else:
                    lines.extend(':fake:fingerprint: ({})'.format(
                        k.split(' ', 2)[-1]) for k in model_state.ssh_keys)
                return '\n'.join(lines)
            if command == 'add-ssh-key':
                return model_state.add_ssh_key(args)
            if command == 'remove-ssh-key':
                return model_state.remove_ssh_key(args)
            if command == 'import-ssh-key':
                return model_state.import_ssh_key(args)
            return ''

    def expect(self, command, args, used_feature_flags, juju_home, model=None,
               timeout=None, extra_env=None):
        if command == 'autoload-credentials':
            return AutoloadCredentials(self, juju_home, extra_env)
        else:
            return FakeExpectChild(self, juju_home, extra_env)

    def pause(self, seconds):
        pass


def get_user_register_command_info(username):
    code = get_user_register_token(username)
    return 'Please send this command to {}\n    juju register {}'.format(
        username, code)


def get_user_register_token(username):
    return b64encode(sha512(username).digest())


class FakeBackend2B7(FakeBackend):

    def juju(self, command, args, used_feature_flags,
             juju_home, model=None, check=True, timeout=None, extra_env=None):
        if model is not None:
            model_state = self.controller_state.models[model]
        if command == 'destroy-service':
            model_state.destroy_service(*args)
        if command == 'remove-service':
            model_state.destroy_service(*args)
        return super(FakeBackend2B7).juju(command, args, used_feature_flags,
                                          juju_home, model, check, timeout,
                                          extra_env)


class FakeBackendOptionalJES(FakeBackend):

    def is_feature_enabled(self, feature):
        return bool(feature in self.feature_flags)


def fake_juju_client(env=None, full_path=None, debug=False, version='2.0.0',
                     _backend=None, cls=EnvJujuClient):
    if env is None:
        env = JujuData('name', {
            'type': 'foo',
            'default-series': 'angsty',
            'region': 'bar',
            }, juju_home='foo')
        env.credentials = {'credentials': {'foo': {'creds': {}}}}
    juju_home = env.juju_home
    if juju_home is None:
        juju_home = 'foo'
    if _backend is None:
        backend_state = FakeControllerState()
        _backend = FakeBackend(
            backend_state, version=version, full_path=full_path,
            debug=debug)
        _backend.set_feature('jes', True)
    client = cls(
        env, version, full_path, juju_home, debug, _backend=_backend)
    client.bootstrap_replaces = {}
    return client


def fake_juju_client_optional_jes(env=None, full_path=None, debug=False,
                                  jes_enabled=True, version='2.0.0',
                                  _backend=None):
    if _backend is None:
        backend_state = FakeControllerState()
        _backend = FakeBackendOptionalJES(
            backend_state, version=version, full_path=full_path,
            debug=debug)
        _backend.set_feature('jes', jes_enabled)
    client = fake_juju_client(env, full_path, debug, version, _backend,
                              cls=FakeJujuClientOptionalJES)
    client.used_feature_flags = frozenset(['address-allocation', 'jes'])
    return client


class FakeJujuClientOptionalJES(EnvJujuClient):

    def get_controller_model_name(self):
        return self._backend.controller_state.controller_model.name
