#!/usr/bin/env python
# Backup and restore a stack.

from __future__ import print_function

__metaclass__ = type

from argparse import ArgumentParser
import os
import re
import subprocess
import sys

from deploy_stack import (
    destroy_environment,
    dump_env_logs,
    get_machine_dns_name,
)
from jujuconfig import translate_to_env
from jujupy import (
    Environment,
    format_listing,
    until_timeout,
)
from utility import (
    print_now,
    wait_for_port,
)


backup_file_pattern = re.compile('(juju-backup-[0-9-]+\.tgz)')
running_instance_pattern = re.compile('\["([^"]+)"\]')


def setup_juju_path(juju_path):
    """Ensure the binaries and scripts under test are found first."""
    full_path = os.path.abspath(juju_path)
    if not os.path.isdir(full_path):
        raise ValueError("The juju_path does not exist: %s" % full_path)
    os.environ['PATH'] = '%s:%s' % (full_path, os.environ['PATH'])
    sys.path.insert(0, full_path)


def deploy_stack(env, charm_prefix):
    """"Deploy a simple stack, state-server and ubuntu."""
    if charm_prefix and not charm_prefix.endswith('/'):
        charm_prefix = charm_prefix + '/'
    agent_version = env.get_matching_agent_version()
    instance_id = env.get_status().status['machines']['0']['instance-id']
    for ignored in until_timeout(30):
        agent_versions = env.get_status().get_agent_versions()
        if 'unknown' not in agent_versions and len(agent_versions) == 1:
            break
    if agent_versions.keys() != [agent_version]:
        print_now("Current versions: %s" % ', '.join(agent_versions.keys()))
        env.juju('upgrade-juju', '--version', agent_version)
    env.wait_for_version(env.get_matching_agent_version())
    env.juju('deploy', charm_prefix + 'ubuntu')
    env.wait_for_started().status
    print_now("%s is ready to testing" % env.environment)
    return instance_id


def backup_state_server(env):
    """juju-backup provides a tarball."""
    environ = dict(os.environ)
    # juju-backup does not support the -e flag.
    environ['JUJU_ENV'] = env.environment
    output = subprocess.check_output(["juju-backup"], env=environ)
    print_now(output)
    match = backup_file_pattern.search(output)
    if match is None:
        raise Exception("The backup file was not found in output: %s" % output)
    backup_file_name = match.group(1)
    backup_file_path = os.path.abspath(backup_file_name)
    print_now("State-Server backup at %s" % backup_file_path)
    return backup_file_path


def restore_present_state_server(env, backup_file):
    """juju-restore wont restore when the state-server is still present."""
    environ = dict(os.environ)
    proc = subprocess.Popen(
        ["juju', '--show-log', 'restore", '-e', env.environment, backup_file],
        env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, err = proc.communicate()
    if proc.returncode == 0:
        raise Exception(
            "juju-restore restored to an operational state-server: %s" % err)
    else:
        print_now(
            "juju-restore correctly refused to restore "
            "because the state-server was still up.")
        match = running_instance_pattern.search(err)
        if match is None:
            raise Exception("The instance was not found in output above.")
        instance_id = match.group(1)
    return instance_id


def delete_instance(env, instance_id):
    """Delete the instance using the providers tools."""
    print_now("Instrumenting a bootstrap node failure.")
    provider_type = env.config.get('type')
    if provider_type == 'ec2':
        environ = dict(os.environ)
        ec2_url = 'https://%s.ec2.amazonaws.com' % env.config['region']
        environ['EC2_URL'] = ec2_url
        environ['EC2_ACCESS_KEY'] = env.config['access-key']
        environ['EC2_SECRET_KEY'] = env.config['secret-key']
        command_args = ['euca-terminate-instances', instance_id]
    elif provider_type == 'openstack':
        environ = dict(os.environ)
        environ.update(translate_to_env(env.config))
        command_args = ['nova', 'delete', instance_id]
    else:
        raise ValueError(
            "This test does not support the %s provider" % provider_type)
    print_now("Deleting %s." % instance_id)
    output = subprocess.check_output(command_args, env=environ)
    print_now(output)


def delete_extra_state_servers(env, instance_id):
    """Delete the extra state-server instances."""
    status = env.get_status()
    for machine, info in status.iter_machines():
        extra_instance_id = info.get('instance-id')
        status = info.get('state-server-member-status')
        if extra_instance_id != instance_id and status is not None:
            print_now("Deleting state-server-member {}".format(machine))
            host = get_machine_dns_name(env, machine)
            delete_instance(env, extra_instance_id)
            wait_for_state_server_to_shutdown(host, env, extra_instance_id)


def restore_missing_state_server(env, backup_file):
    """juju-restore creates a replacement state-server for the services."""
    environ = dict(os.environ)
    print_now("Starting restore.")
    proc = subprocess.Popen(
        ['juju', '--show-log', 'restore', '-e', env.environment,
         '--constraints', 'mem=2G', backup_file],
        env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, err = proc.communicate()
    if proc.returncode != 0:
        raise Exception("Restore failed: \n%s" % err)
    print_now(output)
    env.wait_for_started(600).status
    print_now("%s restored" % env.environment)
    print_now("PASS")


def wait_for_ha(env):
    desired_state = 'has-vote'
    for remaining in until_timeout(1200):
        status = env.get_status()
        states = {}
        for machine, info in status.iter_machines():
            status = info.get('state-server-member-status')
            if status is None:
                continue
            states.setdefault(status, []).append(machine)
        if states.keys() == [desired_state]:
            return
        print_now(format_listing(states, desired_state))
    else:
        raise Exception('Timed out waiting for voting to be enabled.')


def wait_for_state_server_to_shutdown(host, env, instance_id):
    print_now("Waiting for port to close on %s" % host)
    wait_for_port(host, 17070, closed=True)
    print_now("Closed.")
    provider_type = env.config.get('type')
    if provider_type == 'openstack':
        environ = dict(os.environ)
        environ.update(translate_to_env(env.config))
        for ignored in until_timeout(300):
            output = subprocess.check_output(['nova', 'list'], env=environ)
            if instance_id not in output:
                print_now('{} was removed from nova list'.format(instance_id))
                break
        else:
            raise Exception(
                '{} was not deleted:\n{}'.format(instance_id, output))


def parse_new_state_server_from_error(error):
    output = str(error) + getattr(error, 'output', '')
    matches = re.findall(r'Attempting to connect to (.*):22', output)
    if matches:
        return matches[-1]
    return None


def main():
    parser = ArgumentParser('Test recovery strategies.')
    parser.add_argument(
        '--charm-prefix', help='A prefix for charm urls.', default='')
    strategy = parser.add_argument_group('test strategy')
    strategy.add_argument(
        '--ha', action='store_const', dest='strategy', const='ha',
        default='backup', help="Test HA.")
    strategy.add_argument(
        '--backup', action='store_const', dest='strategy', const='backup',
        help="Test backup/restore.")
    strategy.add_argument(
        '--ha-backup', action='store_const', dest='strategy', const='ha-backup',
        help="Test backup/restore of HA.")
    parser.add_argument('juju_path')
    parser.add_argument('env_name')
    args = parser.parse_args()
    try:
        setup_juju_path(args.juju_path)
        env = Environment.from_config(args.env_name)
        env.bootstrap()
        bootstrap_host = get_machine_dns_name(env, 0)
        try:
            instance_id = deploy_stack(env, args.charm_prefix)
            if args.strategy in ('ha', 'ha-backup'):
                env.juju('ensure-availability', '-n', '3')
                wait_for_ha(env)
            if args.strategy in ('ha-backup', 'backup'):
                backup_file = backup_state_server(env)
                restore_present_state_server(env, backup_file)
            if args.strategy == 'ha-backup':
                delete_extra_state_servers(env, instance_id)
            delete_instance(env, instance_id)
            wait_for_state_server_to_shutdown(bootstrap_host, env, instance_id)
            bootstrap_host = None
            if args.strategy == 'ha':
                env.get_status(600)
            else:
                restore_missing_state_server(env, backup_file)
        except Exception as e:
            if bootstrap_host is None:
                bootstrap_host = parse_new_state_server_from_error(e)
            dump_env_logs(env, bootstrap_host,
                          os.path.join(os.environ['WORKSPACE'], 'artifacts'))
            raise
        finally:
            destroy_environment(env)
    except Exception as e:
        print_now("\nEXCEPTION CAUGHT:\n")
        print_now(e)
        if getattr(e, 'output', None):
            print_now('\n')
            print_now(e.output)
        print_now("\nFAIL")
        sys.exit(1)


if __name__ == '__main__':
    main()
