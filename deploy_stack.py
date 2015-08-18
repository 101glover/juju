#!/usr/bin/env python
from __future__ import print_function
__metaclass__ = type


from argparse import ArgumentParser
from contextlib import (
    contextmanager,
    nested,
)
import glob
import logging
import os
import random
import re
import string
import subprocess
import sys
import time
import json
import shutil

from chaos import background_chaos
from jujuconfig import (
    get_jenv_path,
    get_juju_home,
    translate_to_env,
)
from jujupy import (
    EnvJujuClient,
    get_cache_path,
    get_local_root,
    jes_home_path,
    SimpleEnvironment,
    temp_bootstrap_env,
)
from remote import (
    remote_from_address,
    remote_from_unit,
)
from substrate import (
    destroy_job_instances,
    LIBVIRT_DOMAIN_RUNNING,
    resolve_remote_dns_names,
    run_instances,
    start_libvirt_domain,
    stop_libvirt_domain,
    verify_libvirt_domain,
)
from utility import (
    add_basic_testing_arguments,
    configure_logging,
    ensure_deleted,
    PortTimeoutError,
    print_now,
    until_timeout,
    wait_for_port,
)


def destroy_environment(client, instance_tag):
    client.destroy_environment()
    if (client.env.config['type'] == 'manual'
            and 'AWS_ACCESS_KEY' in os.environ):
        destroy_job_instances(instance_tag)


def deploy_dummy_stack(client, charm_prefix):
    """"Deploy a dummy stack in the specified environment.
    """
    client.deploy(charm_prefix + 'dummy-source')
    token = get_random_string()
    client.juju('set', ('dummy-source', 'token=%s' % token))
    client.deploy(charm_prefix + 'dummy-sink')
    client.juju('add-relation', ('dummy-source', 'dummy-sink'))
    client.juju('expose', ('dummy-sink',))
    if client.env.kvm or client.env.maas:
        # A single virtual machine may need up to 30 minutes before
        # "apt-get update" and other initialisation steps are
        # finished; two machines initializing concurrently may
        # need even 40 minutes. In addition Windows image blobs or
        # any system deployment using MAAS requires extra time.
        client.wait_for_started(3600)
    else:
        client.wait_for_started()
    check_token(client, token)


GET_TOKEN_SCRIPT = """
        for x in $(seq 120); do
          if [ -f /var/run/dummy-sink/token ]; then
            if [ "$(cat /var/run/dummy-sink/token)" != "" ]; then
              break
            fi
          fi
          sleep 1
        done
        cat /var/run/dummy-sink/token
    """


def check_token(client, token):
    # Wait up to 120 seconds for token to be created.
    # Utopic is slower, maybe because the devel series gets more
    # package updates.
    logging.info('Retrieving token.')
    remote = remote_from_unit(client, "dummy-sink/0")
    # Update remote with real address if needed.
    resolve_remote_dns_names(client.env, [remote])
    start = time.time()
    while True:
        if remote.is_windows():
            result = remote.cat("%ProgramData%\\dummy-sink\\token")
        else:
            result = remote.run(GET_TOKEN_SCRIPT)
        result = re.match(r'([^\n\r]*)\r?\n?', result).group(1)
        if result == token:
            logging.info("Token matches expected %r", result)
            return
        if time.time() - start > 120:
            raise ValueError('Token is %r' % result)
        logging.info("Found token %r expected %r", result, token)
        time.sleep(5)


def get_random_string():
    allowed_chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(allowed_chars) for n in range(20))


def _can_run_ssh():
    """Returns true if local system can use ssh to access remote machines"""
    # When client is run on a windows machine, we have no local ssh binary.
    return sys.platform != "win32"


def dump_env_logs(client, bootstrap_host, artifacts_dir, runtime_config=None):
    if client.env.local:
        logging.info("Retrieving logs for local environment")
        copy_local_logs(client.env, artifacts_dir)
    else:
        remote_machines = get_remote_machines(client, bootstrap_host)

        for machine_id in sorted(remote_machines, key=int):
            remote = remote_machines[machine_id]
            if not _can_run_ssh() and not remote.is_windows():
                logging.info("No ssh, skipping logs for machine-%s using %r",
                             machine_id, remote)
                continue
            logging.info("Retrieving logs for machine-%s using %r", machine_id,
                         remote)
            machine_dir = os.path.join(artifacts_dir,
                                       "machine-%s" % machine_id)
            os.mkdir(machine_dir)
            copy_remote_logs(remote, machine_dir)
    archive_logs(artifacts_dir)
    retain_config(runtime_config, artifacts_dir)


def retain_config(runtime_config, log_directory):
    if not runtime_config:
        return False

    try:
        shutil.copy(runtime_config, log_directory)
        return True
    except IOError:
        print_now("Failed to copy file. Source: %s Destination: %s" %
                  (runtime_config, log_directory))
    return False


def dump_juju_timings(client, log_directory):
    try:
        with open(os.path.join(log_directory, 'juju_command_times.json'),
                  'w') as timing_file:
            json.dump(client.get_juju_timings(), timing_file, indent=2,
                      sort_keys=True)
            timing_file.write('\n')
    except Exception as e:
        print_now("Failed to save timings")
        print_now(str(e))


def get_remote_machines(client, bootstrap_host):
    """Return a dict of machine_id to remote machines.

    A bootstrap_host address may be provided as a fallback for machine 0 if
    status fails. For some providers such as MAAS the dns-name will be
    resolved to a real ip address using the substrate api.
    """
    # Try to get machine details from environment if possible.
    machines = dict(iter_remote_machines(client))
    # The bootstrap host is added as a fallback in case status failed.
    if bootstrap_host and '0' not in machines:
        machines['0'] = remote_from_address(bootstrap_host)
    # Update remote machines in place with real addresses if substrate needs.
    resolve_remote_dns_names(client.env, machines.itervalues())
    return machines


def iter_remote_machines(client):
    try:
        status = client.get_status()
    except Exception as err:
        logging.warning("Failed to retrieve status for dumping logs: %s", err)
        return

    for machine_id, machine in status.iter_machines():
        hostname = machine.get('dns-name')
        if hostname:
            remote = remote_from_address(hostname, machine.get('series'))
            yield machine_id, remote


def archive_logs(log_dir):
    """Compress log files in given log_dir using gzip."""
    log_files = glob.glob(os.path.join(log_dir, '*.log'))
    if log_files:
        subprocess.check_call(['gzip', '--best', '-f'] + log_files)


lxc_template_glob = '/var/lib/juju/containers/juju-*-lxc-template/*.log'


def copy_local_logs(env, directory):
    """Copy logs for all machines in local environment."""
    local = get_local_root(get_juju_home(), env)
    log_names = [os.path.join(local, 'cloud-init-output.log')]
    log_names.extend(glob.glob(os.path.join(local, 'log', '*.log')))
    log_names.extend(glob.glob(lxc_template_glob))
    try:
        subprocess.check_call(['sudo', 'chmod', 'go+r'] + log_names)
        subprocess.check_call(['cp'] + log_names + [directory])
    except subprocess.CalledProcessError as e:
        logging.warning("Could not retrieve local logs: %s", e)


def copy_remote_logs(remote, directory):
    """Copy as many logs from the remote host as possible to the directory."""
    # This list of names must be in the order of creation to ensure they
    # are retrieved.
    if remote.is_windows():
        log_paths = [
            "%ProgramFiles(x86)%\\Cloudbase Solutions\\Cloudbase-Init\\log\\*",
            "C:\\Juju\\log\\juju\\*.log",
        ]
    else:
        log_paths = [
            '/var/log/cloud-init*.log',
            '/var/log/juju/*.log',
        ]

        try:
            wait_for_port(remote.address, 22, timeout=60)
        except PortTimeoutError:
            logging.warning("Could not dump logs because port 22 was closed.")
            return

        try:
            remote.run('sudo chmod go+r /var/log/juju/*')
        except subprocess.CalledProcessError as e:
            # The juju log dir is not created until after cloud-init succeeds.
            logging.warning("Could not allow access to the juju logs:")
            logging.warning(e.output)

    try:
        remote.copy(directory, log_paths)
    except subprocess.CalledProcessError as e:
        # The juju logs will not exist if cloud-init failed.
        logging.warning("Could not retrieve some or all logs:")
        logging.warning(e.output)


def assess_juju_run(client):
    responses = client.get_juju_output('run', '--format', 'json', '--service',
                                       'dummy-source,dummy-sink', 'uname')
    responses = json.loads(responses)
    for machine in responses:
        if machine.get('ReturnCode', 0) != 0:
            raise ValueError('juju run on machine %s returned %d: %s' % (
                             machine.get('MachineId'),
                             machine.get('ReturnCode'),
                             machine.get('Stderr')))
    logging.info(
        "juju run succeeded on machines: %r",
        [str(machine.get("MachineId")) for machine in responses])
    return responses


def assess_upgrade(old_client, juju_path, skip_juju_run=False):
    client = EnvJujuClient.by_version(old_client.env, juju_path,
                                      old_client.debug)
    upgrade_juju(client)
    if client.env.config['type'] == 'maas':
        timeout = 1200
    else:
        timeout = 600
    client.wait_for_version(client.get_matching_agent_version(), timeout)
    if not skip_juju_run:
        assess_juju_run(client)
    token = get_random_string()
    client.juju('set', ('dummy-source', 'token=%s' % token))
    check_token(client, token)


def upgrade_juju(client):
    client.set_testing_tools_metadata_url()
    tools_metadata_url = client.get_env_option('tools-metadata-url')
    print(
        'The tools-metadata-url is %s' % tools_metadata_url)
    client.upgrade_juju()


def deploy_job_parse_args(argv=None):
    parser = ArgumentParser('deploy_job')
    add_basic_testing_arguments(parser)
    parser.add_argument('--upgrade', action="store_true", default=False,
                        help='Perform an upgrade test.')
    parser.add_argument('--with-chaos', default=0, type=int,
                        help='Deploy and run Chaos Monkey in the background.')
    parser.add_argument('--jes', action='store_true',
                        help='Use JES to control environments.')
    parser.add_argument('--pre-destroy', action='store_true',
                        help='Destroy any environment in the way first.')
    return parser.parse_args(argv)


def deploy_job():
    args = deploy_job_parse_args()
    configure_logging(args.verbose)
    series = args.series
    if series is None:
        series = 'precise'
    charm_prefix = 'local:{}/'.format(series)
    # Don't need windows state server to test windows charms, trusty is faster.
    if series.startswith("win"):
        logging.info('Setting default series to trusty for windows deploy.')
        series = 'trusty'
    return _deploy_job(args.temp_env_name, args.env, args.upgrade,
                       charm_prefix, args.bootstrap_host, args.machine,
                       series, args.logs, args.debug, args.juju_bin,
                       args.agent_url, args.agent_stream,
                       args.keep_env, args.upload_tools, args.with_chaos,
                       args.jes, args.pre_destroy)


def update_env(env, new_env_name, series=None, bootstrap_host=None,
               agent_url=None, agent_stream=None):
    # Rename to the new name.
    env.environment = new_env_name
    env.config['name'] = new_env_name
    if series is not None:
        env.config['default-series'] = series
    if bootstrap_host is not None:
        env.config['bootstrap-host'] = bootstrap_host
    if agent_url is not None:
        env.config['tools-metadata-url'] = agent_url
    if agent_stream is not None:
        env.config['agent-stream'] = agent_stream


@contextmanager
def boot_context(temp_env_name, client, bootstrap_host, machines, series,
                 agent_url, agent_stream, log_dir, keep_env, upload_tools,
                 permanent=False):
    """Create a temporary environment in a context manager to run tests in.

    Bootstrap a new environment from a temporary config that is suitable to
    run tests in. Logs will be collected from the machines. The environment
    will be destroyed when the test completes or there is an unrecoverable
    error.

    The temporary environment is created by updating a EnvJujuClient's config
    with series, agent_url, agent_stream.

    :param temp_env_name: a unique name for the juju env, such as a Jenkins
        job name.
    :param client: an EnvJujuClient.
    :param bootstrap_host: None, or the address of a manual or MAAS host to
        bootstrap on.
    :param machine: [] or a list of machines to use add to a manual env
        before deploying services.
    :param series: None or the default-series for the temp config.
    :param agent_url: None or the agent-metadata-url for the temp config.
    :param agent_stream: None or the agent-stream for the temp config.
    :param log_dir: The path to the directory to store logs.
    :param keep_env: False or True to not destroy the environment and keep
        it alive to do an autopsy.
    :param upload_tools: False or True to upload the local agent instead of
        using streams.
    """
    created_machines = False
    running_domains = dict()
    try:
        if client.env.config['type'] == 'manual' and bootstrap_host is None:
            instances = run_instances(3, temp_env_name)
            created_machines = True
            bootstrap_host = instances[0][1]
            machines.extend(i[1] for i in instances[1:])
        if client.env.config['type'] == 'maas' and machines:
            for machine in machines:
                name, URI = machine.split('@')
                # Record already running domains, so they can be left running,
                # when finished with the test.
                if verify_libvirt_domain(URI, name, LIBVIRT_DOMAIN_RUNNING):
                    running_domains = {machine: True}
                    logging.info("%s is already running" % name)
                else:
                    running_domains = {machine: False}
                    logging.info("Attempting to start %s at %s" % (name, URI))
                    status_msg = start_libvirt_domain(URI, name)
                    logging.info("%s" % status_msg)
            # No further handling of machines down the line is required.
            machines = []

        update_env(client.env, temp_env_name, series=series,
                   bootstrap_host=bootstrap_host, agent_url=agent_url,
                   agent_stream=agent_stream)
        try:
            host = bootstrap_host
            ssh_machines = [] + machines
            if host is not None:
                ssh_machines.append(host)
            for machine in ssh_machines:
                logging.info('Waiting for port 22 on %s' % machine)
                wait_for_port(machine, 22, timeout=120)
            juju_home = get_juju_home()
            jenv_path = get_jenv_path(juju_home, client.env.environment)
            ensure_deleted(jenv_path)
            try:
                with temp_bootstrap_env(juju_home, client,
                                        permanent=permanent):
                    client.bootstrap(upload_tools)
            except:
                # If run from a windows machine may not have ssh to get logs
                if host is not None and _can_run_ssh():
                    remote = remote_from_address(host, series=series)
                    copy_remote_logs(remote, log_dir)
                    archive_logs(log_dir)
                raise
            try:
                if host is None:
                    host = get_machine_dns_name(client, 0)
                if host is None:
                    raise Exception('Could not get machine 0 host')
                try:
                    yield
                except BaseException as e:
                    logging.exception(e)
                    sys.exit(1)
            finally:
                safe_print_status(client)
                jes_enabled = client.is_jes_enabled()
                if jes_enabled:
                    runtime_config = get_cache_path(client.juju_home)
                else:
                    runtime_config = get_jenv_path(client.juju_home,
                                                   client.env.environment)
                if host is not None:
                    dump_env_logs(client, host, log_dir,
                                  runtime_config=runtime_config)
                if not keep_env:
                    if jes_enabled:
                        client.juju(
                            'system kill', (client.env.environment, '-y'),
                            include_e=False, check=False, timeout=600)
                    else:
                        client.destroy_environment()
        finally:
            if created_machines and not keep_env:
                destroy_job_instances(temp_env_name)
    finally:
        logging.info(
            'Juju command timings: {}'.format(client.get_juju_timings()))
        dump_juju_timings(client, log_dir)
        if client.env.config['type'] == 'maas' and not keep_env:
            logging.info("Waiting for destroy-environment to complete")
            time.sleep(90)
            for machine, running in running_domains.items():
                name, URI = machine.split('@')
                if running:
                    logging.warning("%s at %s was running when deploy_job "
                                    "started. Shutting it down to ensure a "
                                    "clean environment." % (name, URI))
                logging.info("Attempting to stop %s at %s" % (name, URI))
                status_msg = stop_libvirt_domain(URI, name)
                logging.info("%s" % status_msg)


def _deploy_job(temp_env_name, base_env, upgrade, charm_prefix, bootstrap_host,
                machines, series, log_dir, debug, juju_path, agent_url,
                agent_stream, keep_env, upload_tools, with_chaos, use_jes,
                pre_destroy):
    start_juju_path = None if upgrade else juju_path
    if sys.platform == 'win32':
        # Ensure OpenSSH is never in the path for win tests.
        sys.path = [p for p in sys.path if 'OpenSSH' not in p]
    if pre_destroy:
        client = EnvJujuClient.by_version(
            SimpleEnvironment(temp_env_name, {}), juju_path, debug)
        if use_jes:
            client.enable_jes()
            client.juju_home = jes_home_path(client.juju_home, temp_env_name)
        client.destroy_environment()
    client = EnvJujuClient.by_version(
        SimpleEnvironment.from_config(base_env), start_juju_path, debug)
    if use_jes:
        client.enable_jes()
    with boot_context(temp_env_name, client, bootstrap_host, machines,
                      series, agent_url, agent_stream, log_dir, keep_env,
                      upload_tools, permanent=use_jes):
        if machines is not None:
            client.add_ssh_machines(machines)
        if sys.platform in ('win32', 'darwin'):
            # The win and osx client tests only verify the client
            # can bootstrap and call the state-server.
            return
        client.juju('status', ())
        if with_chaos > 0:
            manager = background_chaos(temp_env_name, client, log_dir,
                                       with_chaos)
        else:
            # Create a no-op context manager, to avoid duplicate calls of
            # deploy_dummy_stack(), as was the case prior to this revision.
            manager = nested()
        with manager:
            deploy_dummy_stack(client, charm_prefix)
        is_windows_charm = charm_prefix.startswith("local:win")
        if not is_windows_charm:
            assess_juju_run(client)
        if upgrade:
            client.juju('status', ())
            assess_upgrade(client, juju_path, skip_juju_run=is_windows_charm)


def safe_print_status(client):
    """Show the output of juju status without raising exceptions."""
    try:
        client.juju('status', ())
    except Exception as e:
        logging.exception(e)


def get_machine_dns_name(client, machine):
    timeout = 600
    for remaining in until_timeout(timeout):
        bootstrap = client.get_status(
            timeout=timeout).status['machines'][str(machine)]
        host = bootstrap.get('dns-name')
        if host is not None and not host.startswith('172.'):
            return host


def wait_for_state_server_to_shutdown(host, client, instance_id):
    print_now("Waiting for port to close on %s" % host)
    wait_for_port(host, 17070, closed=True)
    print_now("Closed.")
    provider_type = client.env.config.get('type')
    if provider_type == 'openstack':
        environ = dict(os.environ)
        environ.update(translate_to_env(client.env.config))
        for ignored in until_timeout(300):
            output = subprocess.check_output(['nova', 'list'], env=environ)
            if instance_id not in output:
                print_now('{} was removed from nova list'.format(instance_id))
                break
        else:
            raise Exception(
                '{} was not deleted:\n{}'.format(instance_id, output))
