#!/usr/bin/env python

from argparse import ArgumentParser
from contextlib import contextmanager
import logging
from textwrap import dedent
from subprocess import CalledProcessError
import sys

from jujupy import (
    EnvJujuClient,
    SimpleEnvironment,
    until_timeout,
    )
from deploy_stack import (
    BootstrapManager,
    check_token,
    get_random_string,
    )
from jujuci import add_credential_args
from utility import configure_logging


def prepare_dummy_env(client):
    """Use a client to prepare a dummy environment."""
    client.deploy('local:dummy-source')
    client.deploy('local:dummy-sink')
    token = get_random_string()
    client.juju('set', ('dummy-source', 'token=%s' % token))
    client.juju('add-relation', ('dummy-source', 'dummy-sink'))
    client.juju('expose', ('dummy-sink',))
    return token


def get_clients(initial, other, base_env, debug, agent_url):
    """Return the clients to use for testing."""
    environment = SimpleEnvironment.from_config(base_env)
    if agent_url is None:
        environment.config.pop('tools-metadata-url', None)
    initial_client = EnvJujuClient.by_version(environment, initial,
                                              debug=debug)
    other_client = EnvJujuClient.by_version(environment, other, debug=debug)
    # System juju is assumed to be released and the best choice for tearing
    # down environments reliably.  (For example, 1.18.x cannot tear down
    # environments with alpha agent-versions.)
    released_client = EnvJujuClient.by_version(environment, debug=debug)
    return initial_client, other_client, released_client


def assess_heterogeneous(initial, other, base_env, environment_name, log_dir,
                         upload_tools, debug, agent_url, agent_stream, series):
    """Top level function that prepares the clients and environment.

    initial and other are paths to the binary used initially, and a binary
    used later.  base_env is the name of the environment to base the
    environment on and environment_name is the new name for the environment.
    """
    initial_client, other_client, released_client = get_clients(
        initial, other, base_env, debug, agent_url)
    jes_enabled = initial_client.is_jes_enabled()
    bs_manager = BootstrapManager(
        environment_name, initial_client, released_client,
        bootstrap_host=None, machines=[], series=series, agent_url=agent_url,
        agent_stream=agent_stream, region=None, log_dir=log_dir,
        keep_env=False, permanent=jes_enabled, jes_enabled=jes_enabled)
    test_control_heterogeneous(bs_manager, other_client, upload_tools)


@contextmanager
def run_context(bs_manager, other, upload_tools):
    try:
        bs_manager.keep_env = True
        with bs_manager.booted_context(upload_tools):
            other.juju_home = bs_manager.client.juju_home
            yield
        # Test clean shutdown of an environment.
        juju_with_fallback(other, bs_manager.tear_down_client,
                           'destroy-environment',
                           (other.env.environment, '-y'), include_e=False)
    except:
        bs_manager.tear_down()
        raise


def test_control_heterogeneous(bs_manager, other, upload_tools):
    """Test if one binary can control an environment set up by the other."""
    initial = bs_manager.client
    released = bs_manager.tear_down_client
    # Work around bug #1524398.  Force all clients to use the same env
    # instance.
    other.env = initial.env
    released.env = initial.env
    with run_context(bs_manager, other, upload_tools):
        token = prepare_dummy_env(initial)
        initial.wait_for_started()
        if sys.platform != "win32":
            # Currently, juju ssh is not working on Windows.
            check_token(initial, token)
            check_series(other)
            other.juju('run', ('--all', 'uname -a'))
        other.get_juju_output('get', 'dummy-source')
        other.get_juju_output('get-env')
        other.juju('remove-relation', ('dummy-source', 'dummy-sink'))
        status = other.get_status()
        other.juju('unexpose', ('dummy-sink',))
        status = other.get_status()
        if status.status['services']['dummy-sink']['exposed']:
            raise AssertionError('dummy-sink is still exposed')
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

# suppress nosetests
test_control_heterogeneous.__test__ = False


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


def parse_args(argv=None):
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
    parser.add_argument('--agent-url', default=None)
    parser.add_argument('--agent-stream', action='store',
                        help='URL for retrieving agent binaries.')
    parser.add_argument('--series', action='store',
                        help='Name of the Ubuntu series to use.')
    add_credential_args(parser)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    configure_logging(logging.INFO)
    assess_heterogeneous(args.initial, args.other, args.base_environment,
                         args.environment_name, args.log_dir,
                         args.upload_tools, args.debug, args.agent_url,
                         args.agent_stream, args.series)


if __name__ == '__main__':
    main()
