#!/usr/bin/env python
from argparse import ArgumentParser
from contextlib import contextmanager
from textwrap import dedent
from subprocess import CalledProcessError

from jujuconfig import get_juju_home
from jujupy import (
    Environment,
    EnvJujuClient,
    JujuClientDevel,
    SimpleEnvironment,
    temp_bootstrap_env,
    until_timeout,
    )
from deploy_stack import (
    check_token,
    dump_logs,
    get_machine_dns_name,
    get_random_string,
    update_env,
    )


def bootstrap_client(client, upload_tools):
    """Bootstrap using a client and its environment.

    Any stale environment is destroyed, first.
    The machine's DNS name or IP address is returned.
    If bootstrapping fails, the environment is forcibly torn down.
    """
    juju_home = get_juju_home()
    try:
        with temp_bootstrap_env(juju_home, client):
            client.bootstrap(upload_tools=upload_tools)
        host = get_machine_dns_name(client, 0)
        return host
    except:
        client.destroy_environment()
        raise


@contextmanager
def dumping_env(client, host, log_dir):
    """Provide a context that always has its logs dumped at the end.

    If an exception is encountered, the environment is destroyed after
    dumping logs.
    """
    try:
        try:
            yield
        finally:
            dump_logs(client, host, log_dir)
    except:
        client.destroy_environment()
        raise


def prepare_dummy_env(client):
    """Use a client to prepare a dummy environment."""
    client.deploy('local:dummy-source')
    client.deploy('local:dummy-sink')
    token = get_random_string()
    client.juju('set', ('dummy-source', 'token=%s' % token))
    client.juju('add-relation', ('dummy-source', 'dummy-sink'))
    client.juju('expose', ('dummy-sink',))
    return token


def assess_heterogeneous(initial, other, base_env, environment_name, log_dir,
                         upload_tools, debug):
    """Top level function that prepares the clients and environment.

    initial and other are paths to the binariy used initially, and a binary
    used later.  base_env is the name of the environment to base the
    environment on and environment_name is the new name for the environment.
    """
    environment = SimpleEnvironment.from_config(base_env)
    update_env(environment, environment_name, series='precise')
    environment.config.pop('tools-metadata-url', None)
    initial_client = EnvJujuClient.by_version(environment, initial,
                                              debug=debug)
    other_client = EnvJujuClient.by_version(environment, other, debug=debug)
    # System juju is assumed to be released and the best choice for tearing
    # down environments reliably.  (For example, 1.18.x cannot tear down
    # environments with alpha agent-versions.)
    released_client = EnvJujuClient.by_version(environment, debug=debug)
    test_control_heterogeneous(initial_client, other_client, released_client,
                               log_dir, upload_tools)


def test_control_heterogeneous(initial, other, released, log_dir,
                               upload_tools):
    """Test if one binary can control an environment set up by the other."""
    released.destroy_environment()
    host = bootstrap_client(initial, upload_tools)
    with dumping_env(released, host, log_dir):
        token = prepare_dummy_env(initial)
        initial.wait_for_started()
        check_token(initial, token)
        check_series(other)
        other.juju('run', ('--all', 'uname -a'))
        other.get_juju_output('get', 'dummy-source')
        other.get_juju_output('get-env')
        other.juju('remove-relation', ('dummy-source', 'dummy-sink'))
        status = other.get_status()
        other.juju('unexpose', ('dummy-sink',))
        status = other.get_status()
        juju_with_fallback(other, released, 'deploy',
                           ('local:dummy-sink', 'sink2'))
        other.wait_for_started()
        other.juju('add-relation', ('dummy-source', 'sink2'))
        status = other.get_status()
        other.juju('expose', ('sink2',))
        status = other.get_status()
        if 'sink2' not in status.status['services']:
            raise AssertionError('Sink2 missing')
        other.juju('destroy-service', ('sink2',))
        for ignored in until_timeout(30):
            status = other.get_status()
            if 'sink2' not in status.status['services']:
                break
        else:
            raise AssertionError('Sink2 not destroyed')
        other.juju('add-relation', ('dummy-source', 'dummy-sink'))
        status = other.get_status()
        relations = status.status['services']['dummy-sink']['relations']
        if not relations['source'] == ['dummy-source']:
            raise AssertionError('source is not dummy-source.')
        other.juju('expose', ('dummy-sink',))
        status = other.get_status()
        if not status.status['services']['dummy-sink']['exposed']:
            raise AssertionError('dummy-sink is not exposed')
        other.juju('add-unit', ('dummy-sink',))
        if not has_agent(other, 'dummy-sink/1'):
            raise AssertionError('dummy-sink/1 was not added.')
        other.juju('remove-unit', ('dummy-sink/1',))
        status = other.get_status()
        if has_agent(other, 'dummy-sink/1'):
            raise AssertionError('dummy-sink/1 was not removed.')
        other.juju('add-machine', ('lxc',))
        status = other.get_status()
        lxc_machine, = set(k for k, v in status.agent_items() if
                           k.endswith('/lxc/0'))
        lxc_holder = lxc_machine.split('/')[0]
        other.juju('remove-machine', (lxc_machine,))
        wait_until_removed(other, lxc_machine)
        other.juju('remove-machine', (lxc_holder,))
        wait_until_removed(other, lxc_holder)
    # Test clean shutdown of an environment.
    juju_with_fallback(other, released, 'destroy-environment',
                       (other.env.environment, '-y'), include_e=False)


def juju_with_fallback(other, released, command, args, include_e=True):
    """Fallback to released juju when 1.18 fails.

    Get as much test coverage of 1.18 as we can, by falling back to a released
    juju for commands that we expect to fail (due to unsupported agent version
    format).
    """
    for client in [other, released]:
        try:
            client.juju(command, args, include_e=include_e)
        except CalledProcessError:
            if not client.version.startswith('1.18.'):
                raise
        else:
            break


def has_agent(client, agent_id):
    return bool(agent_id in dict(client.get_status().agent_items()))


def wait_until_removed(client, agent_id):
    """Wait for an agent to be removed from the environment."""
    for ignored in until_timeout(240):
        if not has_agent(client, agent_id):
            return
    else:
        raise AssertionError('Machine not destroyed: {}.'.format(agent_id))


def check_series(client):
    """Use 'juju ssh' to check that the deployed series meets expectations."""
    result = client.get_juju_output('ssh', '0', 'lsb_release', '-c')
    label, codename = result.rstrip().split('\t')
    if label != 'Codename:':
        raise AssertionError()
    expected_codename = client.env.config['default-series']
    if codename != expected_codename:
        raise AssertionError(
            'Series is {}, not {}'.format(codename, expected_codename))


def main():
    parser = ArgumentParser(description=dedent("""\
        Determine whether one juju version can control an environment created
        by another version.
    """))
    parser.add_argument('initial', help='The initial juju binary.')
    parser.add_argument('other', help='A different juju binary.')
    parser.add_argument('base_environment', help='The environment to base on.')
    parser.add_argument('environment_name', help='The new environment name.')
    parser.add_argument('log_dir', help='The directory to dump logs to.')
    parser.add_argument(
        '--upload-tools', action='store_true', default=False,
        help='Upload local version of tools before bootstrapping.')
    parser.add_argument('--debug', help='Run juju with --debug',
                        action='store_true', default=False)
    args = parser.parse_args()
    assess_heterogeneous(args.initial, args.other, args.base_environment,
                         args.environment_name, args.log_dir,
                         args.upload_tools, args.debug)


if __name__ == '__main__':
    main()
