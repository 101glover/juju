from contextlib import contextmanager
import copy
from datetime import (
    datetime,
    timedelta,
)
import errno
from itertools import count
import logging
import os
import socket
import StringIO
import subprocess
import sys
from tempfile import NamedTemporaryFile
from textwrap import dedent
import types

from mock import (
    call,
    MagicMock,
    Mock,
    patch,
)
import yaml

from deploy_stack import GET_TOKEN_SCRIPT
from jujuconfig import (
    get_environments_path,
    get_jenv_path,
    NoSuchEnvironment,
)
from jujupy import (
    BootstrapMismatch,
    CannotConnectEnv,
    CONTROLLER,
    EnvJujuClient,
    EnvJujuClient1X,
    EnvJujuClient22,
    EnvJujuClient24,
    EnvJujuClient25,
    EnvJujuClient26,
    EnvJujuClient2A1,
    EnvJujuClient2A2,
    ErroredUnit,
    GroupReporter,
    get_cache_path,
    get_local_root,
    get_machine_dns_name,
    get_timeout_path,
    jes_home_path,
    JESByDefault,
    JESNotSupported,
    JujuData,
    JUJU_DEV_FEATURE_FLAGS,
    KILL_CONTROLLER,
    make_client,
    make_safe_config,
    parse_new_state_server_from_error,
    SimpleEnvironment,
    Status,
    SYSTEM,
    tear_down,
    temp_bootstrap_env,
    _temp_env as temp_env,
    temp_yaml_file,
    uniquify_local,
    UpgradeMongoNotSupported,
)
from tests import (
    TestCase,
    FakeHomeTestCase,
)
from utility import (
    scoped_environ,
    temp_dir,
)


__metaclass__ = type


def assert_juju_call(test_case, mock_method, client, expected_args,
                     call_index=None):
    if call_index is None:
        test_case.assertEqual(len(mock_method.mock_calls), 1)
        call_index = 0
    empty, args, kwargs = mock_method.mock_calls[call_index]
    test_case.assertEqual(args, (expected_args,))


class FakeEnvironmentState:
    """A Fake environment state that can be used by multiple FakeClients."""

    def __init__(self):
        self._clear()

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
        self.state = 'not-bootstrapped'
        self.current_bundle = None
        self.models = {}
        self.model_config = None

    def add_machine(self):
        machine_id = str(self.machine_id_iter.next())
        self.machines.add(machine_id)
        self.machine_host_names[machine_id] = '{}.example.com'.format(
            machine_id)
        return machine_id

    def add_ssh_machines(self, machines):
        for machine in machines:
            self.add_machine()

    def add_container(self, container_type):
        host = self.add_machine()
        container_name = '{}/{}/{}'.format(host, container_type, '0')
        self.containers[host] = {container_name}

    def remove_container(self, container_id):
        for containers in self.containers.values():
            containers.discard(container_id)

    def remove_machine(self, machine_id):
        self.machines.remove(machine_id)
        self.containers.pop(machine_id, None)

    def bootstrap(self, env, commandline_config):
        self.name = env.environment
        self.state_servers.append(self.add_machine())
        self.state = 'bootstrapped'
        self.model_config = copy.deepcopy(env.config)
        self.model_config.update(commandline_config)

    def destroy_environment(self):
        self._clear()
        self.state = 'destroyed'
        return 0

    def kill_controller(self):
        self._clear()
        self.state = 'controller-killed'

    def create_model(self, name):
        state = FakeEnvironmentState()
        self.models[name] = state
        state.state = 'created'
        return state

    def destroy_model(self):
        self._clear()
        self.state = 'model-destroyed'

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
            machine_dict = {}
            hostname = self.machine_host_names.get(machine_id)
            if hostname is not None:
                machine_dict['dns-name'] = hostname
            machines[machine_id] = machine_dict
        for host, containers in self.containers.items():
            machines[host]['containers'] = dict((c, {}) for c in containers)
        services = {}
        for service, units in self.services.items():
            unit_map = {}
            for unit_id, machine_id in units:
                unit_map[unit_id] = {'machine': machine_id}
            services[service] = {
                'units': unit_map,
                'relations': self.relations.get(service, {}),
                'exposed': service in self.exposed,
                }
        return {'machines': machines, 'services': services}


class FakeJujuClient:
    """A fake juju client for tests.

    This is a partial implementation, but should be suitable for many uses,
    and can be extended.

    The state is provided by _backing_state, so that multiple clients can
    manipulate the same state.
    """
    def __init__(self, env=None, full_path=None, debug=False,
                 jes_enabled=False):
        self._backing_state = FakeEnvironmentState()
        if env is None:
            env = SimpleEnvironment('name', {
                'type': 'foo',
                'default-series': 'angsty',
                }, juju_home='foo')
        self.env = env
        self.full_path = full_path
        self.debug = debug
        self.bootstrap_replaces = {}
        self._jes_enabled = jes_enabled

    def clone(self, env, full_path=None, debug=None):
        if full_path is None:
            full_path = self.full_path
        if debug is None:
            debug = self.debug
        client = self.__class__(env, full_path, debug,
                                jes_enabled=self._jes_enabled)
        client._backing_state = self._backing_state
        return client

    def by_version(self, env, path, debug):
        return FakeJujuClient(env, path, debug)

    def get_matching_agent_version(self):
        return '1.2-alpha3'

    def is_jes_enabled(self):
        return self._jes_enabled

    def get_cache_path(self):
        return get_cache_path(self.env.juju_home, models=True)

    def get_juju_output(self, command, *args, **kwargs):
        if (command, args) == ('ssh', ('dummy-sink/0', GET_TOKEN_SCRIPT)):
            return self._backing_state.token
        if (command, args) == ('ssh', ('0', 'lsb_release', '-c')):
            return 'Codename:\t{}\n'.format(self.env.config['default-series'])

    def juju(self, cmd, args, include_e=True):
        # TODO: Use argparse or change all call sites to use functions.
        if (cmd, args[:1]) == ('set', ('dummy-source',)):
            name, value = args[1].split('=')
            if name == 'token':
                self._backing_state.token = value
        if cmd == 'deploy':
            self.deploy(*args)
        if cmd == 'destroy-service':
            self._backing_state.destroy_service(*args)
        if cmd == 'add-relation':
            if args[0] == 'dummy-source':
                self._backing_state.relations[args[1]] = {'source': [args[0]]}
        if cmd == 'expose':
            (service,) = args
            self._backing_state.exposed.add(service)
        if cmd == 'unexpose':
            (service,) = args
            self._backing_state.exposed.remove(service)
        if cmd == 'add-unit':
            (service,) = args
            self._backing_state.add_unit(service)
        if cmd == 'remove-unit':
            (unit_id,) = args
            self._backing_state.remove_unit(unit_id)
        if cmd == 'add-machine':
            (container_type,) = args
            self._backing_state.add_container(container_type)
        if cmd == 'remove-machine':
            (machine_id,) = args
            if '/' in machine_id:
                self._backing_state.remove_container(machine_id)
            else:
                self._backing_state.remove_machine(machine_id)

    def bootstrap(self, upload_tools=False, bootstrap_series=None):
        commandline_config = {}
        if bootstrap_series is not None:
            commandline_config['default-series'] = bootstrap_series
        self._backing_state.bootstrap(self.env, commandline_config)

    @contextmanager
    def bootstrap_async(self, upload_tools=False):
        yield

    def quickstart(self, bundle):
        self._backing_state.bootstrap(self.env, {})
        self._backing_state.deploy_bundle(bundle)

    def create_environment(self, controller_client, config_file):
        model_state = controller_client._backing_state.create_model(
            self.env.environment)
        self._backing_state = model_state

    def destroy_model(self):
        self._backing_state.destroy_model()

    def destroy_environment(self, force=True, delete_jenv=False):
        self._backing_state.destroy_environment()
        return 0

    def kill_controller(self):
        self._backing_state.kill_controller()

    def add_ssh_machines(self, machines):
        self._backing_state.add_ssh_machines(machines)

    def deploy(self, charm_name, service_name=None):
        if service_name is None:
            service_name = charm_name.split(':')[-1]
        self._backing_state.deploy(charm_name, service_name)

    def remove_service(self, service):
        self._backing_state.destroy_service(service)

    def wait_for_started(self, timeout=1200, start=None):
        pass

    def wait_for_deploy_started(self):
        pass

    def show_status(self):
        pass

    def get_status(self):
        status_dict = self._backing_state.get_status_dict()
        return Status(status_dict, yaml.safe_dump(status_dict))

    def status_until(self, timeout):
        yield self.get_status()

    def set_config(self, service, options):
        option_strings = ['{}={}'.format(*item) for item in options.items()]
        self.juju('set', (service,) + tuple(option_strings))

    def get_config(self, service):
        pass

    def get_model_config(self):
        return copy.deepcopy(self._backing_state.model_config)

    def deployer(self, bundle, name=None):
        pass

    def wait_for_workloads(self):
        pass

    def get_juju_timings(self):
        pass


class TestErroredUnit(TestCase):

    def test_output(self):
        e = ErroredUnit('bar', 'baz')
        self.assertEqual('bar is in state baz', str(e))


class ClientTest(FakeHomeTestCase):

    def setUp(self):
        super(ClientTest, self).setUp()
        patcher = patch('jujupy.pause')
        self.addCleanup(patcher.stop)
        self.pause_mock = patcher.start()


class CloudSigmaTest:

    def test__shell_environ_no_flags(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.25-foobar', 'path')
        env = client._shell_environ()
        self.assertEqual(env.get(JUJU_DEV_FEATURE_FLAGS, ''), '')

    def test__shell_environ_cloudsigma(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'cloudsigma'}),
            '1.25-foobar', 'path')
        env = client._shell_environ()
        self.assertTrue('cloudsigma' in env[JUJU_DEV_FEATURE_FLAGS].split(","))

    def test__shell_environ_juju_home(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.25-foobar', 'path',
            'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_HOME'], 'asdf')


class TestTempYamlFile(TestCase):

    def test_temp_yaml_file(self):
        with temp_yaml_file({'foo': 'bar'}) as yaml_file:
            with open(yaml_file) as f:
                self.assertEqual({'foo': 'bar'}, yaml.safe_load(f))


class TestEnvJujuClient26(ClientTest, CloudSigmaTest):

    client_class = EnvJujuClient26

    def test_enable_jes_already_supported(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.26-foobar', 'path')
        fake_popen = FakePopen(CONTROLLER, '', 0)
        with patch('subprocess.Popen', autospec=True,
                   return_value=fake_popen) as po_mock:
            with self.assertRaises(JESByDefault):
                client.enable_jes()
        self.assertNotIn('jes', client.feature_flags)
        assert_juju_call(
            self, po_mock, client, ('juju', '--show-log', 'help', 'commands'))

    def test_enable_jes_unsupported(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.24-foobar', 'path')
        with patch('subprocess.Popen', autospec=True,
                   return_value=FakePopen('', '', 0)) as po_mock:
            with self.assertRaises(JESNotSupported):
                client.enable_jes()
        self.assertNotIn('jes', client.feature_flags)
        assert_juju_call(
            self, po_mock, client, ('juju', '--show-log', 'help', 'commands'),
            0)
        assert_juju_call(
            self, po_mock, client, ('juju', '--show-log', 'help', 'commands'),
            1)
        self.assertEqual(po_mock.call_count, 2)

    def test_enable_jes_requires_flag(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.25-foobar', 'path')
        # The help output will change when the jes feature flag is set.
        with patch('subprocess.Popen', autospec=True, side_effect=[
                FakePopen('', '', 0),
                FakePopen(SYSTEM, '', 0)]) as po_mock:
            client.enable_jes()
        self.assertIn('jes', client.feature_flags)
        assert_juju_call(
            self, po_mock, client, ('juju', '--show-log', 'help', 'commands'),
            0)
        # GZ 2015-10-26: Should assert that env has feature flag at call time.
        assert_juju_call(
            self, po_mock, client, ('juju', '--show-log', 'help', 'commands'),
            1)
        self.assertEqual(po_mock.call_count, 2)

    def test_disable_jes(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.25-foobar', 'path')
        client.feature_flags.add('jes')
        client.disable_jes()
        self.assertNotIn('jes', client.feature_flags)

    def test__shell_environ_jes(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.25-foobar', 'path')
        client.feature_flags.add('jes')
        env = client._shell_environ()
        self.assertIn('jes', env[JUJU_DEV_FEATURE_FLAGS].split(","))

    def test__shell_environ_jes_cloudsigma(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'cloudsigma'}),
            '1.25-foobar', 'path')
        client.feature_flags.add('jes')
        env = client._shell_environ()
        flags = env[JUJU_DEV_FEATURE_FLAGS].split(",")
        self.assertItemsEqual(['cloudsigma', 'jes'], flags)

    def test_clone_unchanged(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(type(client1), type(client2))
        self.assertIs(client1.env, client2.env)
        self.assertEqual(client1.version, client2.version)
        self.assertEqual(client1.full_path, client2.full_path)
        self.assertIs(client1.debug, client2.debug)

    def test_clone_changed(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        env2 = SimpleEnvironment('bar')
        client2 = client1.clone(env2, '1.28', 'other/path', debug=False,
                                cls=EnvJujuClient1X)
        self.assertIs(EnvJujuClient1X, type(client2))
        self.assertIs(env2, client2.env)
        self.assertEqual('1.28', client2.version)
        self.assertEqual('other/path', client2.full_path)
        self.assertIs(False, client2.debug)

    def test_clone_defaults(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(self.client_class, type(client2))
        self.assertEqual(set(), client2.feature_flags)

    def test_clone_enabled(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        client1.enable_feature('jes')
        client1.enable_feature('address-allocation')
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(self.client_class, type(client2))
        self.assertEqual(
            set(['jes', 'address-allocation']),
            client2.feature_flags)

    def test_clone_old_feature(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        client1.enable_feature('actions')
        client1.enable_feature('address-allocation')
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(self.client_class, type(client2))
        self.assertEqual(set(['address-allocation']), client2.feature_flags)


class TestEnvJujuClient25(TestEnvJujuClient26):

    client_class = EnvJujuClient25


class TestEnvJujuClient22(ClientTest):

    client_class = EnvJujuClient22

    def test__shell_environ(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.22-foobar', 'path')
        env = client._shell_environ()
        self.assertEqual(env.get(JUJU_DEV_FEATURE_FLAGS), 'actions')

    def test__shell_environ_juju_home(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.22-foobar', 'path',
            'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_HOME'], 'asdf')


class TestEnvJujuClient24(ClientTest, CloudSigmaTest):

    client_class = EnvJujuClient24

    def test_no_jes(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'cloudsigma'}),
            '1.25-foobar', 'path')
        with self.assertRaises(JESNotSupported):
            client.enable_jes()
        client._use_jes = True
        env = client._shell_environ()
        self.assertNotIn('jes', env[JUJU_DEV_FEATURE_FLAGS].split(","))

    def test_add_ssh_machines(self):
        client = self.client_class(SimpleEnvironment('foo', {}), None, '')
        with patch('subprocess.check_call', autospec=True) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-baz'), 2)
        self.assertEqual(cc_mock.call_count, 3)

    def test_add_ssh_machines_no_retry(self):
        client = self.client_class(SimpleEnvironment('foo', {}), None, '')
        with patch('subprocess.check_call', autospec=True,
                   side_effect=[subprocess.CalledProcessError(None, None),
                                None, None, None]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'))


class TestTearDown(TestCase):

    def test_tear_down_no_jes(self):
        client = MagicMock()
        client.destroy_environment.return_value = 0
        tear_down(client, False)
        client.destroy_environment.assert_called_once_with(force=False)
        self.assertEqual(0, client.kill_controller.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_no_jes_exception(self):
        client = MagicMock()
        client.destroy_environment.side_effect = [1, 0]
        tear_down(client, False)
        self.assertEqual(
            client.destroy_environment.mock_calls,
            [call(force=False), call(force=True)])
        self.assertEqual(0, client.kill_controller.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_jes(self):
        client = MagicMock()
        tear_down(client, True)
        client.kill_controller.assert_called_once_with()
        self.assertEqual(0, client.destroy_environment.call_count)
        self.assertEqual(0, client.enable_jes.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_try_jes(self):

        def check_jes():
            client.enable_jes.assert_called_once_with()
            self.assertEqual(0, client.disable_jes.call_count)

        client = MagicMock()
        client.kill_controller.side_effect = check_jes

        tear_down(client, jes_enabled=False, try_jes=True)
        client.kill_controller.assert_called_once_with()
        client.disable_jes.assert_called_once_with()

    def test_tear_down_jes_try_jes(self):
        client = MagicMock()
        tear_down(client, jes_enabled=True, try_jes=True)
        client.kill_controller.assert_called_once_with()
        self.assertEqual(0, client.destroy_environment.call_count)
        self.assertEqual(0, client.enable_jes.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_try_jes_not_supported(self):

        def check_jes(force=True):
            client.enable_jes.assert_called_once_with()
            return 0

        client = MagicMock()
        client.enable_jes.side_effect = JESNotSupported
        client.destroy_environment.side_effect = check_jes

        tear_down(client, jes_enabled=False, try_jes=True)
        client.destroy_environment.assert_called_once_with(force=False)
        self.assertEqual(0, client.disable_jes.call_count)


class FakePopen(object):

    def __init__(self, out, err, returncode):
        self._out = out
        self._err = err
        self._code = returncode

    def communicate(self):
        self.returncode = self._code
        return self._out, self._err

    def poll(self):
        return self._code


@contextmanager
def observable_temp_file():
    temporary_file = NamedTemporaryFile(delete=False)
    try:
        with temporary_file as temp_file:
            with patch('jujupy.NamedTemporaryFile',
                       return_value=temp_file):
                with patch.object(temp_file, '__exit__'):
                    yield temp_file
    finally:
        try:
            os.unlink(temporary_file.name)
        except OSError as e:
            # File may have already been deleted, e.g. by temp_yaml_file.
            if e.errno != errno.ENOENT:
                raise


class TestEnvJujuClient(ClientTest):

    def test_no_duplicate_env(self):
        env = JujuData('foo', {})
        client = EnvJujuClient(env, '1.25', 'full_path')
        self.assertIs(env, client.env)

    def test_convert_to_juju_data(self):
        env = SimpleEnvironment('foo', {'type': 'bar'}, 'baz')
        with patch.object(JujuData, 'load_yaml'):
            client = EnvJujuClient(env, '1.25', 'full_path')
            client.env.load_yaml.assert_called_once_with()
        self.assertIsInstance(client.env, JujuData)
        self.assertEqual(client.env.environment, 'foo')
        self.assertEqual(client.env.config, {'type': 'bar'})
        self.assertEqual(client.env.juju_home, 'baz')

    def test_get_version(self):
        value = ' 5.6 \n'
        with patch('subprocess.check_output', return_value=value) as vsn:
            version = EnvJujuClient.get_version()
        self.assertEqual('5.6', version)
        vsn.assert_called_with(('juju', '--version'))

    def test_get_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            EnvJujuClient.get_version('foo/bar/baz')
        vsn.assert_called_once_with(('foo/bar/baz', '--version'))

    def test_get_matching_agent_version(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        self.assertEqual('1.23.1', client.get_matching_agent_version())
        self.assertEqual('1.23', client.get_matching_agent_version(
                         no_build=True))
        client.version = '1.20-beta1-series-arch'
        self.assertEqual('1.20-beta1.1', client.get_matching_agent_version())

    def test_upgrade_juju_nonlocal(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'nonlocal'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234'))

    def test_upgrade_juju_local(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234', '--upload-tools',))

    def test_upgrade_juju_no_force_version(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju(force_version=False)
        juju_mock.assert_called_with(
            'upgrade-juju', ('--upload-tools',))

    @patch.object(EnvJujuClient, 'get_full_path', return_value='fake-path')
    def test_by_version(self, gfp_mock):
        def juju_cmd_iterator():
            yield '1.17'
            yield '1.16'
            yield '1.16.1'
            yield '1.15'
            yield '1.22.1'
            yield '1.24-alpha1'
            yield '1.24.7'
            yield '1.25.1'
            yield '1.26.1'
            yield '1.27.1'
            yield '2.0-alpha1'
            yield '2.0-alpha2'
            yield '2.0-alpha2-fake-wrapper'
            yield '2.0-alpha3'
            yield '2.0-beta1'
            yield '2.0-delta1'

        context = patch.object(
            EnvJujuClient, 'get_version',
            side_effect=juju_cmd_iterator().send)
        with context:
            self.assertIs(EnvJujuClient1X,
                          type(EnvJujuClient.by_version(None)))
            with self.assertRaisesRegexp(Exception, 'Unsupported juju: 1.16'):
                EnvJujuClient.by_version(None)
            with self.assertRaisesRegexp(Exception,
                                         'Unsupported juju: 1.16.1'):
                EnvJujuClient.by_version(None)
            client = EnvJujuClient.by_version(None)
            self.assertIs(EnvJujuClient1X, type(client))
            self.assertEqual('1.15', client.version)
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient22)
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient24)
            self.assertEqual(client.version, '1.24-alpha1')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient24)
            self.assertEqual(client.version, '1.24.7')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient25)
            self.assertEqual(client.version, '1.25.1')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient26)
            self.assertEqual(client.version, '1.26.1')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient1X)
            self.assertEqual(client.version, '1.27.1')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient2A1)
            self.assertEqual(client.version, '2.0-alpha1')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient2A2)
            self.assertEqual(client.version, '2.0-alpha2')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-alpha1-juju-wrapped')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-alpha3')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-beta1')
            client = EnvJujuClient.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-delta1')
            with self.assertRaises(StopIteration):
                EnvJujuClient.by_version(None)

    def test_by_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            client = EnvJujuClient.by_version(None, 'foo/bar/qux')
        vsn.assert_called_once_with(('foo/bar/qux', '--version'))
        self.assertNotEqual(client.full_path, 'foo/bar/qux')
        self.assertEqual(client.full_path, os.path.abspath('foo/bar/qux'))

    def test_by_version_keep_home(self):
        env = JujuData({}, juju_home='/foo/bar')
        with patch('subprocess.check_output', return_value='2.0-alpha3-a-b'):
            EnvJujuClient.by_version(env, 'foo/bar/qux')
        self.assertEqual('/foo/bar', env.juju_home)

    def test_clone_unchanged(self):
        client1 = EnvJujuClient(JujuData('foo'), '1.27', 'full/path',
                                debug=True)
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(type(client1), type(client2))
        self.assertIs(client1.env, client2.env)
        self.assertEqual(client1.version, client2.version)
        self.assertEqual(client1.full_path, client2.full_path)
        self.assertIs(client1.debug, client2.debug)
        self.assertEqual(client1.feature_flags, client2.feature_flags)

    def test_clone_changed(self):
        client1 = EnvJujuClient(JujuData('foo'), '1.27', 'full/path',
                                debug=True)
        env2 = SimpleEnvironment('bar')
        client2 = client1.clone(env2, '1.28', 'other/path', debug=False,
                                cls=EnvJujuClient1X)
        self.assertIs(EnvJujuClient1X, type(client2))
        self.assertIs(env2, client2.env)
        self.assertEqual('1.28', client2.version)
        self.assertEqual('other/path', client2.full_path)
        self.assertIs(False, client2.debug)
        self.assertEqual(client1.feature_flags, client2.feature_flags)

    def test_get_cache_path(self):
        client = EnvJujuClient(JujuData('foo', juju_home='/foo/'),
                               '1.27', 'full/path', debug=True)
        self.assertEqual('/foo/models/cache.yaml',
                         client.get_cache_path())

    def test_full_args(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('juju', '--show-log', 'bar', '-m', 'foo', 'baz',
                          'qux'), full)
        full = client._full_args('bar', True, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--show-log', 'bar', '-m', 'foo',
            'baz', 'qux'), full)
        client.env = None
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('juju', '--show-log', 'bar', 'baz', 'qux'), full)

    def test_full_args_debug(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        client.debug = True
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--debug', 'bar', '-m', 'foo', 'baz', 'qux'), full)

    def test_full_args_action(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        full = client._full_args('action bar', False, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--show-log', 'action', 'bar', '-m', 'foo', 'baz', 'qux'),
            full)

    def test__bootstrap_config(self):
        env = JujuData('foo', {
            'access-key': 'foo',
            'admin-secret': 'foo',
            'agent-metadata-url': 'frank',
            'agent-stream': 'foo',
            'application-id': 'foo',
            'application-password': 'foo',
            'auth-url': 'foo',
            'authorized-keys': 'foo',
            'availability-sets-enabled': 'foo',
            'bootstrap-host': 'foo',
            'bootstrap-timeout': 'foo',
            'bootstrap-user': 'foo',
            'client-email': 'foo',
            'client-id': 'foo',
            'container': 'foo',
            'control-bucket': 'foo',
            'default-series': 'foo',
            'development': False,
            'enable-os-upgrade': 'foo',
            'image-metadata-url': 'foo',
            'location': 'foo',
            'maas-oauth': 'foo',
            'maas-server': 'foo',
            'manta-key-id': 'foo',
            'manta-user': 'foo',
            'name': 'foo',
            'password': 'foo',
            'prefer-ipv6': 'foo',
            'private-key': 'foo',
            'region': 'foo',
            'sdc-key-id': 'foo',
            'sdc-url': 'foo',
            'sdc-user': 'foo',
            'secret-key': 'foo',
            'storage-account-name': 'foo',
            'subscription-id': 'foo',
            'tenant-id': 'foo',
            'tenant-name': 'foo',
            'test-mode': False,
            'tools-metadata-url': 'steve',
            'type': 'foo',
            'username': 'foo',
            }, 'home')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        with client._bootstrap_config() as config_filename:
            with open(config_filename) as f:
                self.assertEqual({
                    'admin-secret': 'foo',
                    'agent-metadata-url': 'frank',
                    'agent-stream': 'foo',
                    'authorized-keys': 'foo',
                    'availability-sets-enabled': 'foo',
                    'bootstrap-timeout': 'foo',
                    'bootstrap-user': 'foo',
                    'container': 'foo',
                    'default-series': 'foo',
                    'development': False,
                    'enable-os-upgrade': 'foo',
                    'image-metadata-url': 'foo',
                    'prefer-ipv6': 'foo',
                    'test-mode': True,
                    'tools-metadata-url': 'steve',
                    }, yaml.safe_load(f))

    def test_get_cloud_region(self):
        self.assertEqual(
            'foo/bar', EnvJujuClient.get_cloud_region('foo', 'bar'))
        self.assertEqual(
            'foo', EnvJujuClient.get_cloud_region('foo', None))

    def test_bootstrap_maas(self):
        env = JujuData('maas', {'type': 'foo', 'region': 'asdf'})
        with patch.object(EnvJujuClient, 'juju') as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with patch.object(client.env, 'maas', lambda: True):
                with observable_temp_file() as config_file:
                    client.bootstrap()
            mock.assert_called_with(
                'bootstrap', (
                    '--constraints', 'mem=2G arch=amd64', 'maas', 'foo/asdf',
                    '--config', config_file.name, '--agent-version', '2.0'),
                include_e=False)

    def test_bootstrap_joyent(self):
        env = JujuData('joyent', {
            'type': 'joyent', 'sdc-url': 'https://foo.api.joyentcloud.com'})
        with patch.object(EnvJujuClient, 'juju', autospec=True) as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with patch.object(client.env, 'joyent', lambda: True):
                with observable_temp_file() as config_file:
                    client.bootstrap()
            mock.assert_called_once_with(
                client, 'bootstrap', (
                    '--constraints', 'mem=2G cpu-cores=1', 'joyent',
                    'joyent/foo', '--config', config_file.name,
                    '--agent-version', '2.0'), include_e=False)

    def test_bootstrap(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with observable_temp_file() as config_file:
            with patch.object(EnvJujuClient, 'juju') as mock:
                client = EnvJujuClient(env, '2.0-zeta1', None)
                client.bootstrap()
                mock.assert_called_with(
                    'bootstrap', ('--constraints', 'mem=2G',
                                  'foo', 'bar/baz',
                                  '--config', config_file.name,
                                  '--agent-version', '2.0'), include_e=False)
                config_file.seek(0)
                config = yaml.safe_load(config_file)
        self.assertEqual({'test-mode': True}, config)

    def test_bootstrap_upload_tools(self):
        env = JujuData('foo', {'type': 'foo', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with observable_temp_file() as config_file:
                with patch.object(client, 'juju') as mock:
                    client.bootstrap(upload_tools=True)
            mock.assert_called_with(
                'bootstrap', (
                    '--upload-tools', '--constraints', 'mem=2G', 'foo',
                    'foo/baz', '--config', config_file.name), include_e=False)

    def test_bootstrap_args(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client, 'juju') as mock:
            with observable_temp_file() as config_file:
                client.bootstrap(bootstrap_series='angsty')
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G', 'foo', 'bar/baz',
                '--config', config_file.name,
                '--agent-version', '2.0',
                '--bootstrap-series', 'angsty'), include_e=False)

    def test_bootstrap_async(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            client.env.juju_home = 'foo'
            with observable_temp_file() as config_file:
                with client.bootstrap_async():
                    mock.assert_called_once_with(
                        client, 'bootstrap', (
                            '--constraints', 'mem=2G', 'foo', 'bar/baz',
                            '--config', config_file.name,
                            '--agent-version', '2.0'), include_e=False)

    def test_bootstrap_async_upload_tools(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with observable_temp_file() as config_file:
                with client.bootstrap_async(upload_tools=True):
                    mock.assert_called_with(
                        client, 'bootstrap', (
                            '--upload-tools', '--constraints', 'mem=2G',
                            'foo', 'bar/baz', '--config', config_file.name),
                        include_e=False)

    def test_get_bootstrap_args_bootstrap_series(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        args = client.get_bootstrap_args(upload_tools=True,
                                         config_filename='config',
                                         bootstrap_series='angsty')
        self.assertEqual(args, (
            '--upload-tools', '--constraints', 'mem=2G', 'foo', 'bar/baz',
            '--config', 'config', '--bootstrap-series', 'angsty'))

    def test_create_environment_hypenated_controller(self):
        self.do_create_environment(
            'kill-controller', 'create-model', ('-c', 'foo'))

    def do_create_environment(self, jes_command, create_cmd,
                              controller_option):
        controller_client = EnvJujuClient(JujuData('foo'), None, None)
        client = EnvJujuClient(JujuData('bar'), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(client, 'juju') as juju_mock:
                client.create_environment(controller_client, 'temp')
        juju_mock.assert_called_once_with(
            create_cmd, controller_option + ('bar', '--config', 'temp'),
            include_e=False)

    def test_destroy_environment(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        self.assertIs(False, hasattr(client, 'destroy_environment'))

    def test_destroy_model(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_model()
        mock.assert_called_with(
            'destroy-model', ('foo', '-y'),
            include_e=False, timeout=600.0)

    def test_kill_controller_system(self):
        self.do_kill_controller('system', 'system kill')

    def test_kill_controller_controller(self):
        self.do_kill_controller('controller', 'controller kill')

    def test_kill_controller_hyphenated(self):
        self.do_kill_controller('kill-controller', 'kill-controller')

    def do_kill_controller(self, jes_command, kill_command):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(client, 'juju') as juju_mock:
                client.kill_controller()
        juju_mock.assert_called_once_with(
            kill_command, ('foo', '-y'), check=False, include_e=False,
            timeout=600)

    def test_get_juju_output(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        fake_popen = FakePopen('asdf', None, 0)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-m', 'foo'),),
                         mock.call_args[0])

    def test_get_juju_output_accepts_varargs(self):
        env = JujuData('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar', 'baz', '--qux')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-m', 'foo', 'baz',
                           '--qux'),), mock.call_args[0])

    def test_get_juju_output_stderr(self):
        env = JujuData('foo')
        fake_popen = FakePopen(None, 'Hello!', 1)
        client = EnvJujuClient(env, None, None)
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.Popen', return_value=fake_popen):
                client.get_juju_output('bar')
        self.assertEqual(exc.exception.stderr, 'Hello!')

    def test_get_juju_output_accepts_timeout(self):
        env = JujuData('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            client.get_juju_output('bar', timeout=5)
        self.assertEqual(
            po_mock.call_args[0][0],
            (sys.executable, get_timeout_path(), '5.00', '--', 'juju',
             '--show-log', 'bar', '-m', 'foo'))

    def test__shell_environ_juju_data(self):
        client = EnvJujuClient(
            JujuData('baz', {'type': 'ec2'}), '1.25-foobar', 'path', 'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_DATA'], 'asdf')
        self.assertNotIn('JUJU_HOME', env)

    def test__shell_environ_cloudsigma(self):
        client = EnvJujuClient(
            JujuData('baz', {'type': 'cloudsigma'}), '1.24-foobar', 'path')
        env = client._shell_environ()
        self.assertEqual(env.get(JUJU_DEV_FEATURE_FLAGS, ''), '')

    def test_juju_output_supplies_path(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, '/foobar/bar')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
            return FakePopen(None, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   side_effect=check_path):
            client.get_juju_output('cmd', 'baz')

    def test_get_status(self):
        output_text = dedent("""\
                - a
                - b
                - c
                """)
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=output_text) as gjo_mock:
            result = client.get_status()
        gjo_mock.assert_called_once_with('show-status', '--format', 'yaml')
        self.assertEqual(Status, type(result))
        self.assertEqual(['a', 'b', 'c'], result.status)

    def test_get_status_retries_on_error(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        client.attempt = 0

        def get_juju_output(command, *args):
            if client.attempt == 1:
                return '"hello"'
            client.attempt += 1
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output', get_juju_output):
            client.get_status()

    def test_get_status_raises_on_timeout_1(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)

        def get_juju_output(command, *args, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output',
                          side_effect=get_juju_output):
            with patch('jujupy.until_timeout', lambda x: iter([None, None])):
                with self.assertRaisesRegexp(
                        Exception, 'Timed out waiting for juju status'):
                    client.get_status()

    def test_get_status_raises_on_timeout_2(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch('jujupy.until_timeout', return_value=iter([1])) as mock_ut:
            with patch.object(client, 'get_juju_output',
                              side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    client.get_status(500)
        mock_ut.assert_called_with(500)

    @staticmethod
    def make_status_yaml(key, machine_value, unit_value):
        return dedent("""\
            machines:
              "0":
                {0}: {1}
            services:
              jenkins:
                units:
                  jenkins/0:
                    {0}: {2}
        """.format(key, machine_value, unit_value))

    def test_deploy_non_joyent(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_joyent(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_repository(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', '/home/jrandom/repo')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--repository', '/home/jrandom/repo'))

    def test_deploy_to(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', to='0')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--to', '0'))

    def test_deploy_service(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:mondogb', service='my-mondogb')
        mock_juju.assert_called_with(
            'deploy', ('local:mondogb', 'my-mondogb',))

    def test_deploy_bundle_2X(self):
        client = EnvJujuClient(JujuData('an_env', None),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju') as mock_juju:
            client.deploy_bundle('bundle:~juju-qa/some-bundle')
        mock_juju.assert_called_with(
            'deploy', ('bundle:~juju-qa/some-bundle'), timeout=3600)

    def test_remove_service(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.remove_service('mondogb')
        mock_juju.assert_called_with('remove-service', ('mondogb',))

    def test_status_until_always_runs_once(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        with patch.object(client, 'get_juju_output', return_value=status_txt):
            result = list(client.status_until(-1))
        self.assertEqual(
            [r.status for r in result], [Status.from_text(status_txt).status])

    def test_status_until_timeout(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        status_yaml = yaml.safe_load(status_txt)

        def until_timeout_stub(timeout, start=None):
            return iter([None, None])

        with patch.object(client, 'get_juju_output', return_value=status_txt):
            with patch('jujupy.until_timeout',
                       side_effect=until_timeout_stub) as ut_mock:
                result = list(client.status_until(30, 70))
        self.assertEqual(
            [r.status for r in result], [status_yaml] * 3)
        # until_timeout is called by status as well as status_until.
        self.assertEqual(ut_mock.mock_calls,
                         [call(60), call(30, start=70), call(60), call(60)])

    def test_add_ssh_machines(self):
        client = EnvJujuClient(JujuData('foo'), None, '')
        with patch('subprocess.check_call', autospec=True) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-bar'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-baz'), 2)
        self.assertEqual(cc_mock.call_count, 3)

    def test_add_ssh_machines_retry(self):
        client = EnvJujuClient(JujuData('foo'), None, '')
        with patch('subprocess.check_call', autospec=True,
                   side_effect=[subprocess.CalledProcessError(None, None),
                                None, None, None]) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-foo'), 0)
        self.pause_mock.assert_called_once_with(30)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-foo'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-bar'), 2)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-baz'), 3)
        self.assertEqual(cc_mock.call_count, 4)

    def test_add_ssh_machines_fail_on_second_machine(self):
        client = EnvJujuClient(JujuData('foo'), None, '')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                None, subprocess.CalledProcessError(None, None), None, None
                ]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-bar'), 1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_add_ssh_machines_fail_on_second_attempt(self):
        client = EnvJujuClient(JujuData('foo'), None, '')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                subprocess.CalledProcessError(None, None),
                subprocess.CalledProcessError(None, None)]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-m', 'foo', 'ssh:m-foo'), 1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_wait_for_started(self):
        value = self.make_status_yaml('agent-state', 'started', 'started')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_started()

    def test_wait_for_started_timeout(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', lambda x, start=None: range(1)):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started()
                self.assertEqual(writes, ['pending: 0', ' .', '\n'])

    def test_wait_for_started_start(self):
        value = self.make_status_yaml('agent-state', 'started', 'pending')
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started(start=now - timedelta(1200))
                self.assertEqual(writes, ['pending: jenkins/0', '\n'])

    def test_wait_for_started_logs_status(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch.object(GroupReporter, '_write', autospec=True,
                              side_effect=lambda _, s: writes.append(s)):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_started(0)
            self.assertEqual(writes, ['pending: 0', '\n'])
        self.assertEqual(self.log_stream.getvalue(), 'ERROR %s\n' % value)

    def test_wait_for_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1/0:
                        agent-state: started
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub2/0:
                        agent-state: started
                      sub3/0:
                        agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'jenkins', 'sub1', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_subordinate_units_with_agent_status(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1/0:
                        agent-status:
                          current: idle
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub2/0:
                        agent-status:
                          current: idle
                      sub3/0:
                        agent-status:
                          current: idle
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'jenkins', 'sub1', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_multiple_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub/0:
                        agent-state: started
                  ubuntu/1:
                    subordinates:
                      sub/1:
                        agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'ubuntu', 'sub', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_subordinate_units_checks_slash_in_unit_name(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1:
                        agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_subordinate_units_no_subordinate(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_workload(self):
        initial_status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: waiting
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        final_status = Status(copy.deepcopy(initial_status.status), None)
        final_status.status['services']['jenkins']['units']['jenkins/0'][
            'workload-status']['current'] = 'active'
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[1]):
            with patch.object(client, 'get_status', autospec=True,
                              side_effect=[initial_status, final_status]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads()
        self.assertEqual(writes, ['waiting: jenkins/0', '\n'])

    def test_wait_for_workload_all_unknown(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: unknown
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_workload_no_workload_status(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: active
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_ha(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'controller-member-status': 'has-vote'},
                '1': {'controller-member-status': 'has-vote'},
                '2': {'controller-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_ha()

    def test_wait_for_ha_no_has_vote(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'controller-member-status': 'no-vote'},
                '1': {'controller-member-status': 'no-vote'},
                '2': {'controller-member-status': 'no-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch('jujupy.until_timeout', autospec=True,
                       return_value=[2, 1]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for voting to be enabled.'):
                        client.wait_for_ha()
            self.assertEqual(writes[:2], ['no-vote: 0, 1, 2', ' .'])
            self.assertEqual(writes[2:-1], ['.'] * (len(writes) - 3))
            self.assertEqual(writes[-1:], ['\n'])

    def test_wait_for_ha_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'controller-member-status': 'has-vote'},
                '1': {'controller-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for voting to be enabled.'):
                    client.wait_for_ha()

    def test_wait_for_deploy_started(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'baz': 'qux'}
                    }
                }
            }
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_deploy_started()

    def test_wait_for_deploy_started_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for services to start.'):
                    client.wait_for_deploy_started()

    def test_wait_for_version(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_timeout(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.1')
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('jujupy.until_timeout', lambda x, start=None: [x]):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception, 'Some versions did not update'):
                        client.wait_for_version('1.17.2')
        self.assertEqual(writes, ['1.17.1: jenkins/0', ' .', '\n'])

    def test_wait_for_version_handles_connection_error(self):
        err = subprocess.CalledProcessError(2, 'foo')
        err.stderr = 'Unable to connect to environment'
        err = CannotConnectEnv(err)
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_raises_non_connection_error(self):
        err = Exception('foo')
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            with self.assertRaisesRegexp(Exception, 'foo'):
                client.wait_for_version('1.17.2')

    def test_wait_for_just_machine_0(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for('machines-not-0', 'none')

    def test_wait_for_just_machine_0_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
                '1': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value), \
            patch('jujupy.until_timeout', lambda x: range(0)), \
            self.assertRaisesRegexp(
                Exception,
                'Timed out waiting for machines-not-0'):
            client.wait_for('machines-not-0', 'none')

    def test_set_model_constraints(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_model_constraints({'bar': 'baz'})
        juju_mock.assert_called_once_with('set-model-constraints',
                                          ('bar=baz',))

    def test_get_model_config(self):
        env = JujuData('foo', None)
        fake_popen = FakePopen(yaml.safe_dump({'bar': 'baz'}), None, 0)
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            result = client.get_model_config()
        assert_juju_call(
            self, po_mock, client, (
                'juju', '--show-log', 'get-model-config', '-m', 'foo'))
        self.assertEqual({'bar': 'baz'}, result)

    def test_get_env_option(self):
        env = JujuData('foo', None)
        fake_popen = FakePopen('https://example.org/juju/tools', None, 0)
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_env_option('tools-metadata-url')
        self.assertEqual(
            mock.call_args[0][0],
            ('juju', '--show-log', 'get-model-config', '-m', 'foo',
             'tools-metadata-url'))
        self.assertEqual('https://example.org/juju/tools', result)

    def test_set_env_option(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_call') as mock:
            client.set_env_option(
                'tools-metadata-url', 'https://example.org/juju/tools')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(
            ('juju', '--show-log', 'set-model-config', '-m', 'foo',
             'tools-metadata-url=https://example.org/juju/tools'))

    def test_set_testing_tools_metadata_url(self):
        env = JujuData(None, {'type': 'foo'})
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_tools_metadata_url()
        mock_get.assert_called_with('tools-metadata-url')
        mock_set.assert_called_with(
            'tools-metadata-url',
            'https://example.org/juju/testing/tools')

    def test_set_testing_tools_metadata_url_noop(self):
        env = JujuData(None, {'type': 'foo'})
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/testing/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_tools_metadata_url()
        mock_get.assert_called_with('tools-metadata-url')
        self.assertEqual(0, mock_set.call_count)

    def test_juju(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_call') as mock:
            client.juju('foo', ('bar', 'baz'))
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(('juju', '--show-log', 'foo', '-m', 'qux',
                                 'bar', 'baz'))

    def test_juju_env(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.check_call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'))

    def test_juju_no_check(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, None)
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        with patch('subprocess.call') as mock:
            client.juju('foo', ('bar', 'baz'), check=False)
        mock.assert_called_with(('juju', '--show-log', 'foo', '-m', 'qux',
                                 'bar', 'baz'))

    def test_juju_no_check_env(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'), check=False)

    def test_juju_timeout(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_call') as cc_mock:
            client.juju('foo', ('bar', 'baz'), timeout=58)
        self.assertEqual(cc_mock.call_args[0][0], (
            sys.executable, get_timeout_path(), '58.00', '--', 'juju',
            '--show-log', 'foo', '-m', 'qux', 'bar', 'baz'))

    def test_juju_juju_home(self):
        env = JujuData('qux')
        os.environ['JUJU_HOME'] = 'foo'
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_home(*args, **kwargs):
            self.assertEqual(os.environ['JUJU_HOME'], 'foo')
            yield
            self.assertEqual(os.environ['JUJU_HOME'], 'asdf')
            yield

        with patch('subprocess.check_call', side_effect=check_home):
            client.juju('foo', ('bar', 'baz'))
            client.env.juju_home = 'asdf'
            client.juju('foo', ('bar', 'baz'))

    def test_juju_extra_env(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, None)
        extra_env = {'JUJU': '/juju', 'JUJU_HOME': client.env.juju_home}

        def check_env(*args, **kwargs):
            self.assertEqual('/juju', os.environ['JUJU'])

        with patch('subprocess.check_call', side_effect=check_env) as mock:
            client.juju('quickstart', ('bar', 'baz'), extra_env=extra_env)
        mock.assert_called_with(
            ('juju', '--show-log', 'quickstart', '-m', 'qux', 'bar', 'baz'))

    def test_juju_backup_with_tgz(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_env(*args, **kwargs):
            return 'foojuju-backup-24.tgzz'
        with patch('subprocess.check_output',
                   side_effect=check_env) as co_mock:
            backup_file = client.backup()
        self.assertEqual(backup_file, os.path.abspath('juju-backup-24.tgz'))
        assert_juju_call(self, co_mock, client, ('juju', '--show-log',
                         'create-backup', '-m', 'qux'))

    def test_juju_backup_with_tar_gz(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='foojuju-backup-123-456.tar.gzbar'):
            backup_file = client.backup()
        self.assertEqual(
            backup_file, os.path.abspath('juju-backup-123-456.tar.gz'))

    def test_juju_backup_no_file(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_output', return_value=''):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_wrong_file(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='mumu-backup-24.tgz'):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_environ(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        environ = client._shell_environ()

        def side_effect(*args, **kwargs):
            self.assertEqual(environ, os.environ)
            return 'foojuju-backup-123-456.tar.gzbar'
        with patch('subprocess.check_output', side_effect=side_effect):
            client.backup()
            self.assertNotEqual(environ, os.environ)

    def test_restore_backup(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch.object(client, 'get_juju_output') as gjo_mock:
            result = client.restore_backup('quxx')
        gjo_mock.assert_called_once_with('restore-backup', '-b',
                                         '--constraints', 'mem=2G',
                                         '--file', 'quxx')
        self.assertIs(gjo_mock.return_value, result)

    def test_restore_backup_async(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch.object(client, 'juju_async') as gjo_mock:
            result = client.restore_backup_async('quxx')
        gjo_mock.assert_called_once_with('restore-backup', (
            '-b', '--constraints', 'mem=2G', '--file', 'quxx'))
        self.assertIs(gjo_mock.return_value, result)

    def test_enable_ha(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch.object(client, 'juju', autospec=True) as eha_mock:
            client.enable_ha()
        eha_mock.assert_called_once_with('enable-ha', ('-n', '3'))

    def test_juju_async(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with client.juju_async('foo', ('bar', 'baz')) as proc:
                assert_juju_call(self, popen_class_mock, client, (
                    'juju', '--show-log', 'foo', '-m', 'qux', 'bar', 'baz'))
                self.assertIs(proc, popen_class_mock.return_value)
                self.assertEqual(proc.wait.call_count, 0)
                proc.wait.return_value = 0
        proc.wait.assert_called_once_with()

    def test_juju_async_failure(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with self.assertRaises(subprocess.CalledProcessError) as err_cxt:
                with client.juju_async('foo', ('bar', 'baz')):
                    proc_mock = popen_class_mock.return_value
                    proc_mock.wait.return_value = 23
        self.assertEqual(err_cxt.exception.returncode, 23)
        self.assertEqual(err_cxt.exception.cmd, (
            'juju', '--show-log', 'foo', '-m', 'qux', 'bar', 'baz'))

    def test_juju_async_environ(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        environ = client._shell_environ()
        proc_mock = Mock()
        with patch('subprocess.Popen') as popen_class_mock:

            def check_environ(*args, **kwargs):
                self.assertEqual(environ, os.environ)
                return proc_mock
            popen_class_mock.side_effect = check_environ
            proc_mock.wait.return_value = 0
            with client.juju_async('foo', ('bar', 'baz')):
                pass
            self.assertNotEqual(environ, os.environ)

    def test_is_jes_enabled(self):
        # EnvJujuClient knows that JES is always enabled, and doesn't need to
        # shell out.
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            self.assertTrue(client.is_jes_enabled())
        self.assertEqual(0, po_mock.call_count)

    def test_get_jes_command(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        # Juju 1.24 and older do not have a JES command. It is an error
        # to call get_jes_command when is_jes_enabled is False
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            self.assertEqual(KILL_CONTROLLER, client.get_jes_command())
        self.assertEqual(0, po_mock.call_count)

    def test_get_juju_timings(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        client.juju_timings = {("juju", "op1"): [1], ("juju", "op2"): [2]}
        flattened_timings = client.get_juju_timings()
        expected = {"juju op1": [1], "juju op2": [2]}
        self.assertEqual(flattened_timings, expected)

    def test_deployer(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle'),
            True
        )

    def test_deployer_with_bundle_name(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle', 'name')
        mock.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle',
                         'name'),
            True
        )

    def test_quickstart_maas(self):
        client = EnvJujuClient(JujuData(None, {'type': 'maas'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G arch=amd64', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_local(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), True, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_nonlocal(self):
        client = EnvJujuClient(JujuData(None, {'type': 'nonlocal'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_action_do(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            mock.return_value = \
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9"
            id = client.action_do("foo/0", "myaction", "param=5")
            self.assertEqual(id, "5a92ec93-d4be-4399-82dc-7431dbfd08f9")
        mock.assert_called_once_with(
            'run-action', 'foo/0', 'myaction', "param=5"
        )

    def test_action_do_error(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            mock.return_value = "some bad text"
            with self.assertRaisesRegexp(Exception,
                                         "Action id not found in output"):
                client.action_do("foo/0", "myaction", "param=5")

    def test_action_fetch(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            mock.return_value = ret
            out = client.action_fetch("123")
            self.assertEqual(out, ret)
        mock.assert_called_once_with(
            'show-action-output', '123', "--wait", "1m"
        )

    def test_action_fetch_timeout(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        ret = "status: pending\nfoo: bar"
        with patch.object(EnvJujuClient,
                          'get_juju_output', return_value=ret):
            with self.assertRaisesRegexp(Exception,
                                         "timed out waiting for action"):
                client.action_fetch("123")

    def test_action_do_fetch(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            # setting side_effect to an iterable will return the next value
            # from the list each time the function is called.
            mock.side_effect = [
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9",
                ret]
            out = client.action_do_fetch("foo/0", "myaction", "param=5")
            self.assertEqual(out, ret)

    def test_list_space(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        yaml_dict = {'foo': 'bar'}
        output = yaml.safe_dump(yaml_dict)
        with patch.object(client, 'get_juju_output', return_value=output,
                          autospec=True) as gjo_mock:
            result = client.list_space()
        self.assertEqual(result, yaml_dict)
        gjo_mock.assert_called_once_with('list-space')

    def test_add_space(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_space('foo-space')
        juju_mock.assert_called_once_with('add-space', ('foo-space'))

    def test_add_subnet(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_subnet('bar-subnet', 'foo-space')
        juju_mock.assert_called_once_with('add-subnet',
                                          ('bar-subnet', 'foo-space'))

    def test__shell_environ_uses_pathsep(self):
        client = EnvJujuClient(JujuData('foo'), None, 'foo/bar/juju')
        with patch('os.pathsep', '!'):
            environ = client._shell_environ()
        self.assertRegexpMatches(environ['PATH'], r'foo/bar\!')

    def test_set_config(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_config('foo', {'bar': 'baz'})
        juju_mock.assert_called_once_with('set-config', ('foo', 'bar=baz'))

    def test_get_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output',
                          side_effect=output) as gjo_mock:
            results = client.get_config('foo')
        self.assertEqual(expected, results)
        gjo_mock.assert_called_once_with('get-config', 'foo')

    def test_get_service_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output', side_effect=output):
            results = client.get_service_config('foo')
        self.assertEqual(expected, results)

    def test_get_service_config_timesout(self):
        client = EnvJujuClient(JujuData('foo', {}), None, '/foo')
        with patch('jujupy.until_timeout', return_value=range(0)):
            with self.assertRaisesRegexp(
                    Exception, 'Timed out waiting for juju get'):
                client.get_service_config('foo')

    def test_upgrade_mongo(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_mongo()
        juju_mock.assert_called_once_with('upgrade-mongo', ())

    def test_enable_feature(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        self.assertEqual(set(), client.feature_flags)
        client.enable_feature('actions')
        self.assertEqual(set(['actions']), client.feature_flags)

    def test_enable_feature_invalid(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        self.assertEqual(set(), client.feature_flags)
        with self.assertRaises(ValueError) as ctx:
            client.enable_feature('nomongo')
        self.assertEqual(str(ctx.exception), "Unknown feature flag: 'nomongo'")


class TestEnvJujuClient2A2(TestCase):

    def test_raise_on_juju_data(self):
        env = JujuData('foo', {'type': 'bar'}, 'baz')
        with self.assertRaisesRegexp(
                ValueError, 'JujuData cannot be used with EnvJujuClient2A2'):
            EnvJujuClient2A2(env, '1.25', 'full_path')

    def test__shell_environ_juju_home(self):
        client = EnvJujuClient2A2(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.25-foobar', 'path',
            'asdf')
        with patch.dict(os.environ, {'PATH': ''}):
            env = client._shell_environ()
        # For transition, supply both.
        self.assertEqual(env['JUJU_HOME'], 'asdf')
        self.assertEqual(env['JUJU_DATA'], 'asdf')

    def test_get_bootstrap_args_bootstrap_series(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient2A2(env, '2.0-zeta1', 'path', 'home')
        args = client.get_bootstrap_args(upload_tools=True,
                                         bootstrap_series='angsty')
        self.assertEqual(args, (
            '--upload-tools', '--constraints', 'mem=2G',
            '--agent-version', '2.0', '--bootstrap-series', 'angsty'))


class TestEnvJujuClient1X(ClientTest):

    def test_no_duplicate_env(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient1X(env, '1.25', 'full_path')
        self.assertIs(env, client.env)

    def test_get_version(self):
        value = ' 5.6 \n'
        with patch('subprocess.check_output', return_value=value) as vsn:
            version = EnvJujuClient1X.get_version()
        self.assertEqual('5.6', version)
        vsn.assert_called_with(('juju', '--version'))

    def test_get_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            EnvJujuClient1X.get_version('foo/bar/baz')
        vsn.assert_called_once_with(('foo/bar/baz', '--version'))

    def test_get_matching_agent_version(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        self.assertEqual('1.23.1', client.get_matching_agent_version())
        self.assertEqual('1.23', client.get_matching_agent_version(
                         no_build=True))
        client.version = '1.20-beta1-series-arch'
        self.assertEqual('1.20-beta1.1', client.get_matching_agent_version())

    def test_upgrade_juju_nonlocal(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'nonlocal'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234'))

    def test_upgrade_juju_local(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234', '--upload-tools',))

    def test_upgrade_juju_no_force_version(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju(force_version=False)
        juju_mock.assert_called_with(
            'upgrade-juju', ('--upload-tools',))

    def test_upgrade_mongo_exception(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with self.assertRaises(UpgradeMongoNotSupported):
            client.upgrade_mongo()

    @patch.object(EnvJujuClient1X, 'get_full_path', return_value='fake-path')
    def test_by_version(self, gfp_mock):
        def juju_cmd_iterator():
            yield '1.17'
            yield '1.16'
            yield '1.16.1'
            yield '1.15'
            yield '1.22.1'
            yield '1.24-alpha1'
            yield '1.24.7'
            yield '1.25.1'
            yield '1.26.1'
            yield '1.27.1'
            yield '2.0-alpha1'
            yield '2.0-alpha2'
            yield '2.0-alpha2-fake-wrapper'
            yield '2.0-alpha3'
            yield '2.0-beta1'
            yield '2.0-delta1'

        context = patch.object(
            EnvJujuClient1X, 'get_version',
            side_effect=juju_cmd_iterator().send)
        with context:
            self.assertIs(EnvJujuClient1X,
                          type(EnvJujuClient1X.by_version(None)))
            with self.assertRaisesRegexp(Exception, 'Unsupported juju: 1.16'):
                EnvJujuClient1X.by_version(None)
            with self.assertRaisesRegexp(Exception,
                                         'Unsupported juju: 1.16.1'):
                EnvJujuClient1X.by_version(None)
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(EnvJujuClient1X, type(client))
            self.assertEqual('1.15', client.version)
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient22)
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient24)
            self.assertEqual(client.version, '1.24-alpha1')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient24)
            self.assertEqual(client.version, '1.24.7')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient25)
            self.assertEqual(client.version, '1.25.1')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient26)
            self.assertEqual(client.version, '1.26.1')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient1X)
            self.assertEqual(client.version, '1.27.1')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient2A1)
            self.assertEqual(client.version, '2.0-alpha1')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient2A2)
            self.assertEqual(client.version, '2.0-alpha2')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-alpha1-juju-wrapped')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-alpha3')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-beta1')
            client = EnvJujuClient1X.by_version(None)
            self.assertIs(type(client), EnvJujuClient)
            self.assertEqual(client.version, '2.0-delta1')
            with self.assertRaises(StopIteration):
                EnvJujuClient1X.by_version(None)

    def test_by_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            client = EnvJujuClient1X.by_version(None, 'foo/bar/qux')
        vsn.assert_called_once_with(('foo/bar/qux', '--version'))
        self.assertNotEqual(client.full_path, 'foo/bar/qux')
        self.assertEqual(client.full_path, os.path.abspath('foo/bar/qux'))

    def test_by_version_keep_home(self):
        env = SimpleEnvironment({}, juju_home='/foo/bar')
        with patch('subprocess.check_output', return_value=' 1.27'):
            EnvJujuClient1X.by_version(env, 'foo/bar/qux')
        self.assertEqual('/foo/bar', env.juju_home)

    def test_get_cache_path(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', juju_home='/foo/'),
                                 '1.27', 'full/path', debug=True)
        self.assertEqual('/foo/environments/cache.yaml',
                         client.get_cache_path())

    def test_full_args(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('juju', '--show-log', 'bar', '-e', 'foo', 'baz',
                          'qux'), full)
        full = client._full_args('bar', True, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--show-log', 'bar', '-e', 'foo',
            'baz', 'qux'), full)
        client.env = None
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('juju', '--show-log', 'bar', 'baz', 'qux'), full)

    def test_full_args_debug(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        client.debug = True
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--debug', 'bar', '-e', 'foo', 'baz', 'qux'), full)

    def test_full_args_action(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        full = client._full_args('action bar', False, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--show-log', 'action', 'bar', '-e', 'foo', 'baz', 'qux'),
            full)

    def test_bootstrap_maas(self):
        env = SimpleEnvironment('maas')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client = EnvJujuClient1X(env, None, None)
            with patch.object(client.env, 'maas', lambda: True):
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G arch=amd64'), False)

    def test_bootstrap_joyent(self):
        env = SimpleEnvironment('joyent')
        with patch.object(EnvJujuClient1X, 'juju', autospec=True) as mock:
            client = EnvJujuClient1X(env, None, None)
            with patch.object(client.env, 'joyent', lambda: True):
                client.bootstrap()
            mock.assert_called_once_with(
                client, 'bootstrap', ('--constraints', 'mem=2G cpu-cores=1'),
                False)

    def test_bootstrap_non_sudo(self):
        env = SimpleEnvironment('foo')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client = EnvJujuClient1X(env, None, None)
            with patch.object(client.env, 'needs_sudo', lambda: False):
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_sudo(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), True)

    def test_bootstrap_upload_tools(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.bootstrap(upload_tools=True)
            mock.assert_called_with(
                'bootstrap', ('--upload-tools', '--constraints', 'mem=2G'),
                True)

    def test_bootstrap_args(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient1X(env, None, None)
        with self.assertRaisesRegexp(
                BootstrapMismatch,
                '--bootstrap-series angsty does not match default-series:'
                ' None'):
            client.bootstrap(bootstrap_series='angsty')
        env.config.update({
            'default-series': 'angsty',
            })
        with patch.object(client, 'juju') as mock:
            client.bootstrap(bootstrap_series='angsty')
        mock.assert_called_with(
            'bootstrap', ('--constraints', 'mem=2G'),
            False)

    def test_bootstrap_async(self):
        env = SimpleEnvironment('foo')
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient1X(env, None, None)
            client.env.juju_home = 'foo'
            with client.bootstrap_async():
                mock.assert_called_once_with(
                    client, 'bootstrap', ('--constraints', 'mem=2G'))

    def test_bootstrap_async_upload_tools(self):
        env = SimpleEnvironment('foo')
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient1X(env, None, None)
            with client.bootstrap_async(upload_tools=True):
                mock.assert_called_with(
                    client, 'bootstrap', ('--upload-tools', '--constraints',
                                          'mem=2G'))

    def test_get_bootstrap_args_bootstrap_series(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient1X(env, None, None)
        with self.assertRaisesRegexp(
                BootstrapMismatch,
                '--bootstrap-series angsty does not match default-series:'
                ' None'):
            client.get_bootstrap_args(upload_tools=True,
                                      bootstrap_series='angsty')
        env.config['default-series'] = 'angsty'
        args = client.get_bootstrap_args(upload_tools=True,
                                         bootstrap_series='angsty')
        self.assertEqual(args, ('--upload-tools', '--constraints', 'mem=2G'))

    def test_create_environment_system(self):
        self.do_create_environment(
            'system', 'system create-environment', ('-s', 'foo'))

    def test_create_environment_controller(self):
        self.do_create_environment(
            'controller', 'controller create-environment', ('-c', 'foo'))

    def test_create_environment_hypenated_controller(self):
        self.do_create_environment(
            'kill-controller', 'create-environment', ('-c', 'foo'))

    def do_create_environment(self, jes_command, create_cmd,
                              controller_option):
        controller_client = EnvJujuClient1X(SimpleEnvironment('foo'), None,
                                            None)
        client = EnvJujuClient1X(SimpleEnvironment('bar'), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(client, 'juju') as juju_mock:
                client.create_environment(controller_client, 'temp')
        juju_mock.assert_called_once_with(
            create_cmd, controller_option + ('bar', '--config', 'temp'),
            include_e=False)

    def test_destroy_environment_non_sudo(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: False):
            with patch.object(client, 'juju') as mock:
                client.destroy_environment()
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                False, check=False, include_e=False, timeout=600.0)

    def test_destroy_environment_sudo(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.destroy_environment()
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                True, check=False, include_e=False, timeout=600.0)

    def test_destroy_environment_no_force(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_environment(force=False)
            mock.assert_called_with(
                'destroy-environment', ('foo', '-y'),
                False, check=False, include_e=False, timeout=600.0)

    def test_destroy_environment_delete_jenv(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju'):
            with temp_env({}) as juju_home:
                client.env.juju_home = juju_home
                jenv_path = get_jenv_path(juju_home, 'foo')
                os.makedirs(os.path.dirname(jenv_path))
                open(jenv_path, 'w')
                self.assertTrue(os.path.exists(jenv_path))
                client.destroy_environment(delete_jenv=True)
                self.assertFalse(os.path.exists(jenv_path))

    def test_destroy_model(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_model()
        mock.assert_called_with(
            'destroy-environment', ('foo', '-y'),
            False, check=False, include_e=False, timeout=600.0)

    def test_kill_controller_system(self):
        self.do_kill_controller('system', 'system kill')

    def test_kill_controller_controller(self):
        self.do_kill_controller('controller', 'controller kill')

    def test_kill_controller_hyphenated(self):
        self.do_kill_controller('kill-controller', 'kill-controller')

    def do_kill_controller(self, jes_command, kill_command):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(client, 'juju') as juju_mock:
                client.kill_controller()
        juju_mock.assert_called_once_with(
            kill_command, ('foo', '-y'), check=False, include_e=False,
            timeout=600)

    def test_get_juju_output(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        fake_popen = FakePopen('asdf', None, 0)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo'),),
                         mock.call_args[0])

    def test_get_juju_output_accepts_varargs(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient1X(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar', 'baz', '--qux')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo', 'baz',
                           '--qux'),), mock.call_args[0])

    def test_get_juju_output_stderr(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen('Hello', 'Error!', 1)
        client = EnvJujuClient1X(env, None, None)
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.Popen', return_value=fake_popen):
                client.get_juju_output('bar')
        self.assertEqual(exc.exception.output, 'Hello')
        self.assertEqual(exc.exception.stderr, 'Error!')

    def test_get_juju_output_accepts_timeout(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient1X(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            client.get_juju_output('bar', timeout=5)
        self.assertEqual(
            po_mock.call_args[0][0],
            (sys.executable, get_timeout_path(), '5.00', '--', 'juju',
             '--show-log', 'bar', '-e', 'foo'))

    def test__shell_environ_juju_home(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.25-foobar', 'path',
            'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_HOME'], 'asdf')
        self.assertNotIn('JUJU_DATA', env)

    def test__shell_environ_cloudsigma(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('baz', {'type': 'cloudsigma'}),
            '1.24-foobar', 'path')
        env = client._shell_environ()
        self.assertEqual(env.get(JUJU_DEV_FEATURE_FLAGS, ''), '')

    def test_juju_output_supplies_path(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, '/foobar/bar')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
            return FakePopen(None, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   side_effect=check_path):
            client.get_juju_output('cmd', 'baz')

    def test_get_status(self):
        output_text = dedent("""\
                - a
                - b
                - c
                """)
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=output_text) as gjo_mock:
            result = client.get_status()
        gjo_mock.assert_called_once_with('status', '--format', 'yaml')
        self.assertEqual(Status, type(result))
        self.assertEqual(['a', 'b', 'c'], result.status)

    def test_get_status_retries_on_error(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        client.attempt = 0

        def get_juju_output(command, *args):
            if client.attempt == 1:
                return '"hello"'
            client.attempt += 1
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output', get_juju_output):
            client.get_status()

    def test_get_status_raises_on_timeout_1(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)

        def get_juju_output(command, *args, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output',
                          side_effect=get_juju_output):
            with patch('jujupy.until_timeout', lambda x: iter([None, None])):
                with self.assertRaisesRegexp(
                        Exception, 'Timed out waiting for juju status'):
                    client.get_status()

    def test_get_status_raises_on_timeout_2(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch('jujupy.until_timeout', return_value=iter([1])) as mock_ut:
            with patch.object(client, 'get_juju_output',
                              side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    client.get_status(500)
        mock_ut.assert_called_with(500)

    @staticmethod
    def make_status_yaml(key, machine_value, unit_value):
        return dedent("""\
            machines:
              "0":
                {0}: {1}
            services:
              jenkins:
                units:
                  jenkins/0:
                    {0}: {2}
        """.format(key, machine_value, unit_value))

    def test_deploy_non_joyent(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_joyent(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_repository(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', '/home/jrandom/repo')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--repository', '/home/jrandom/repo'))

    def test_deploy_to(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', to='0')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--to', '0'))

    def test_deploy_service(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:mondogb', service='my-mondogb')
        mock_juju.assert_called_with(
            'deploy', ('local:mondogb', 'my-mondogb',))

    def test_remove_service(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.remove_service('mondogb')
        mock_juju.assert_called_with('destroy-service', ('mondogb',))

    def test_status_until_always_runs_once(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        with patch.object(client, 'get_juju_output', return_value=status_txt):
            result = list(client.status_until(-1))
        self.assertEqual(
            [r.status for r in result], [Status.from_text(status_txt).status])

    def test_status_until_timeout(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        status_yaml = yaml.safe_load(status_txt)

        def until_timeout_stub(timeout, start=None):
            return iter([None, None])

        with patch.object(client, 'get_juju_output', return_value=status_txt):
            with patch('jujupy.until_timeout',
                       side_effect=until_timeout_stub) as ut_mock:
                result = list(client.status_until(30, 70))
        self.assertEqual(
            [r.status for r in result], [status_yaml] * 3)
        # until_timeout is called by status as well as status_until.
        self.assertEqual(ut_mock.mock_calls,
                         [call(60), call(30, start=70), call(60), call(60)])

    def test_add_ssh_machines(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, '')
        with patch('subprocess.check_call', autospec=True) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-baz'), 2)
        self.assertEqual(cc_mock.call_count, 3)

    def test_add_ssh_machines_retry(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, '')
        with patch('subprocess.check_call', autospec=True,
                   side_effect=[subprocess.CalledProcessError(None, None),
                                None, None, None]) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        self.pause_mock.assert_called_once_with(30)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 2)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-baz'), 3)
        self.assertEqual(cc_mock.call_count, 4)

    def test_add_ssh_machines_fail_on_second_machine(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, '')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                None, subprocess.CalledProcessError(None, None), None, None
                ]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_add_ssh_machines_fail_on_second_attempt(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, '')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                subprocess.CalledProcessError(None, None),
                subprocess.CalledProcessError(None, None)]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_wait_for_started(self):
        value = self.make_status_yaml('agent-state', 'started', 'started')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_started()

    def test_wait_for_started_timeout(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x, start=None: range(1)):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started()
                self.assertEqual(writes, ['pending: 0', ' .', '\n'])

    def test_wait_for_started_start(self):
        value = self.make_status_yaml('agent-state', 'started', 'pending')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started(start=now - timedelta(1200))
                self.assertEqual(writes, ['pending: jenkins/0', '\n'])

    def test_wait_for_started_logs_status(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch.object(GroupReporter, '_write', autospec=True,
                              side_effect=lambda _, s: writes.append(s)):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_started(0)
            self.assertEqual(writes, ['pending: 0', '\n'])
        self.assertEqual(self.log_stream.getvalue(), 'ERROR %s\n' % value)

    def test_wait_for_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1/0:
                        agent-state: started
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub2/0:
                        agent-state: started
                      sub3/0:
                        agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'jenkins', 'sub1', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_multiple_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub/0:
                        agent-state: started
                  ubuntu/1:
                    subordinates:
                      sub/1:
                        agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'ubuntu', 'sub', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_subordinate_units_checks_slash_in_unit_name(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1:
                        agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_subordinate_units_no_subordinate(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_workload(self):
        initial_status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: waiting
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        final_status = Status(copy.deepcopy(initial_status.status), None)
        final_status.status['services']['jenkins']['units']['jenkins/0'][
            'workload-status']['current'] = 'active'
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[1]):
            with patch.object(client, 'get_status', autospec=True,
                              side_effect=[initial_status, final_status]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads()
        self.assertEqual(writes, ['waiting: jenkins/0', '\n'])

    def test_wait_for_workload_all_unknown(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: unknown
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_workload_no_workload_status(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: active
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_ha(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
                '2': {'state-server-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_ha()

    def test_wait_for_ha_no_has_vote(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'no-vote'},
                '1': {'state-server-member-status': 'no-vote'},
                '2': {'state-server-member-status': 'no-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch('jujupy.until_timeout', autospec=True,
                       return_value=[2, 1]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for voting to be enabled.'):
                        client.wait_for_ha()
            self.assertEqual(writes[:2], ['no-vote: 0, 1, 2', ' .'])
            self.assertEqual(writes[2:-1], ['.'] * (len(writes) - 3))
            self.assertEqual(writes[-1:], ['\n'])

    def test_wait_for_ha_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for voting to be enabled.'):
                    client.wait_for_ha()

    def test_wait_for_deploy_started(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'baz': 'qux'}
                    }
                }
            }
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_deploy_started()

    def test_wait_for_deploy_started_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for services to start.'):
                    client.wait_for_deploy_started()

    def test_wait_for_version(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_timeout(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.1')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('jujupy.until_timeout', lambda x, start=None: [x]):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception, 'Some versions did not update'):
                        client.wait_for_version('1.17.2')
        self.assertEqual(writes, ['1.17.1: jenkins/0', ' .', '\n'])

    def test_wait_for_version_handles_connection_error(self):
        err = subprocess.CalledProcessError(2, 'foo')
        err.stderr = 'Unable to connect to environment'
        err = CannotConnectEnv(err)
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_raises_non_connection_error(self):
        err = Exception('foo')
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            with self.assertRaisesRegexp(Exception, 'foo'):
                client.wait_for_version('1.17.2')

    def test_wait_for_just_machine_0(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for('machines-not-0', 'none')

    def test_wait_for_just_machine_0_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
                '1': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value), \
            patch('jujupy.until_timeout', lambda x: range(0)), \
            self.assertRaisesRegexp(
                Exception,
                'Timed out waiting for machines-not-0'):
            client.wait_for('machines-not-0', 'none')

    def test_set_model_constraints(self):
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_model_constraints({'bar': 'baz'})
        juju_mock.assert_called_once_with('set-constraints', ('bar=baz',))

    def test_get_model_config(self):
        env = SimpleEnvironment('foo', None)
        fake_popen = FakePopen(yaml.safe_dump({'bar': 'baz'}), None, 0)
        client = EnvJujuClient1X(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            result = client.get_model_config()
        assert_juju_call(
            self, po_mock, client, (
                'juju', '--show-log', 'get-env', '-e', 'foo'))
        self.assertEqual({'bar': 'baz'}, result)

    def test_get_env_option(self):
        env = SimpleEnvironment('foo', None)
        fake_popen = FakePopen('https://example.org/juju/tools', None, 0)
        client = EnvJujuClient1X(env, None, None)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_env_option('tools-metadata-url')
        self.assertEqual(
            mock.call_args[0][0],
            ('juju', '--show-log', 'get-env', '-e', 'foo',
             'tools-metadata-url'))
        self.assertEqual('https://example.org/juju/tools', result)

    def test_set_env_option(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch('subprocess.check_call') as mock:
            client.set_env_option(
                'tools-metadata-url', 'https://example.org/juju/tools')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(
            ('juju', '--show-log', 'set-env', '-e', 'foo',
             'tools-metadata-url=https://example.org/juju/tools'))

    def test_set_testing_tools_metadata_url(self):
        env = SimpleEnvironment(None, {'type': 'foo'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_tools_metadata_url()
        mock_get.assert_called_with('tools-metadata-url')
        mock_set.assert_called_with(
            'tools-metadata-url',
            'https://example.org/juju/testing/tools')

    def test_set_testing_tools_metadata_url_noop(self):
        env = SimpleEnvironment(None, {'type': 'foo'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/testing/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_tools_metadata_url()
        mock_get.assert_called_with('tools-metadata-url')
        self.assertEqual(0, mock_set.call_count)

    def test_juju(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, None)
        with patch('subprocess.check_call') as mock:
            client.juju('foo', ('bar', 'baz'))
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'))

    def test_juju_env(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.check_call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'))

    def test_juju_no_check(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, None)
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        with patch('subprocess.call') as mock:
            client.juju('foo', ('bar', 'baz'), check=False)
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'))

    def test_juju_no_check_env(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'), check=False)

    def test_juju_timeout(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_call') as cc_mock:
            client.juju('foo', ('bar', 'baz'), timeout=58)
        self.assertEqual(cc_mock.call_args[0][0], (
            sys.executable, get_timeout_path(), '58.00', '--', 'juju',
            '--show-log', 'foo', '-e', 'qux', 'bar', 'baz'))

    def test_juju_juju_home(self):
        env = SimpleEnvironment('qux')
        os.environ['JUJU_HOME'] = 'foo'
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_home(*args, **kwargs):
            self.assertEqual(os.environ['JUJU_HOME'], 'foo')
            yield
            self.assertEqual(os.environ['JUJU_HOME'], 'asdf')
            yield

        with patch('subprocess.check_call', side_effect=check_home):
            client.juju('foo', ('bar', 'baz'))
            client.env.juju_home = 'asdf'
            client.juju('foo', ('bar', 'baz'))

    def test_juju_extra_env(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, None)
        extra_env = {'JUJU': '/juju', 'JUJU_HOME': client.env.juju_home}

        def check_env(*args, **kwargs):
            self.assertEqual('/juju', os.environ['JUJU'])

        with patch('subprocess.check_call', side_effect=check_env) as mock:
            client.juju('quickstart', ('bar', 'baz'), extra_env=extra_env)
        mock.assert_called_with(
            ('juju', '--show-log', 'quickstart', '-e', 'qux', 'bar', 'baz'))

    def test_juju_backup_with_tgz(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_env(*args, **kwargs):
            self.assertEqual(os.environ['JUJU_ENV'], 'qux')
            return 'foojuju-backup-24.tgzz'
        with patch('subprocess.check_output',
                   side_effect=check_env) as co_mock:
            backup_file = client.backup()
        self.assertEqual(backup_file, os.path.abspath('juju-backup-24.tgz'))
        assert_juju_call(self, co_mock, client, ['juju', 'backup'])

    def test_juju_backup_with_tar_gz(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='foojuju-backup-123-456.tar.gzbar'):
            backup_file = client.backup()
        self.assertEqual(
            backup_file, os.path.abspath('juju-backup-123-456.tar.gz'))

    def test_juju_backup_no_file(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_output', return_value=''):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_wrong_file(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='mumu-backup-24.tgz'):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_environ(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        environ = client._shell_environ()
        environ['JUJU_ENV'] = client.env.environment

        def side_effect(*args, **kwargs):
            self.assertEqual(environ, os.environ)
            return 'foojuju-backup-123-456.tar.gzbar'
        with patch('subprocess.check_output', side_effect=side_effect):
            client.backup()
            self.assertNotEqual(environ, os.environ)

    def test_restore_backup(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch.object(client, 'get_juju_output') as gjo_mock:
            result = client.restore_backup('quxx')
        gjo_mock.assert_called_once_with('restore', '--constraints',
                                         'mem=2G', 'quxx')
        self.assertIs(gjo_mock.return_value, result)

    def test_restore_backup_async(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch.object(client, 'juju_async') as gjo_mock:
            result = client.restore_backup_async('quxx')
        gjo_mock.assert_called_once_with(
            'restore', ('--constraints', 'mem=2G', 'quxx'))
        self.assertIs(gjo_mock.return_value, result)

    def test_enable_ha(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch.object(client, 'juju', autospec=True) as eha_mock:
            client.enable_ha()
        eha_mock.assert_called_once_with('ensure-availability', ('-n', '3'))

    def test_juju_async(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with client.juju_async('foo', ('bar', 'baz')) as proc:
                assert_juju_call(self, popen_class_mock, client, (
                    'juju', '--show-log', 'foo', '-e', 'qux', 'bar', 'baz'))
                self.assertIs(proc, popen_class_mock.return_value)
                self.assertEqual(proc.wait.call_count, 0)
                proc.wait.return_value = 0
        proc.wait.assert_called_once_with()

    def test_juju_async_failure(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with self.assertRaises(subprocess.CalledProcessError) as err_cxt:
                with client.juju_async('foo', ('bar', 'baz')):
                    proc_mock = popen_class_mock.return_value
                    proc_mock.wait.return_value = 23
        self.assertEqual(err_cxt.exception.returncode, 23)
        self.assertEqual(err_cxt.exception.cmd, (
            'juju', '--show-log', 'foo', '-e', 'qux', 'bar', 'baz'))

    def test_juju_async_environ(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        environ = client._shell_environ()
        proc_mock = Mock()
        with patch('subprocess.Popen') as popen_class_mock:

            def check_environ(*args, **kwargs):
                self.assertEqual(environ, os.environ)
                return proc_mock
            popen_class_mock.side_effect = check_environ
            proc_mock.wait.return_value = 0
            with client.juju_async('foo', ('bar', 'baz')):
                pass
            self.assertNotEqual(environ, os.environ)

    def test_is_jes_enabled(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            self.assertFalse(client.is_jes_enabled())
        assert_juju_call(self, po_mock, client, (
            'juju', '--show-log', 'help', 'commands'))
        # Juju 1.25 uses the system command.
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(SYSTEM, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   return_value=fake_popen):
            self.assertTrue(client.is_jes_enabled())
        # Juju 1.26 uses the controller command.
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(CONTROLLER, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   return_value=fake_popen):
            self.assertTrue(client.is_jes_enabled())

    def test_get_jes_command(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        # Juju 1.24 and older do not have a JES command. It is an error
        # to call get_jes_command when is_jes_enabled is False
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            with self.assertRaises(JESNotSupported):
                client.get_jes_command()
        assert_juju_call(self, po_mock, client, (
            'juju', '--show-log', 'help', 'commands'))
        # Juju 2.x uses the 'controller kill' command.
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(CONTROLLER, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   return_value=fake_popen):
            self.assertEqual(CONTROLLER, client.get_jes_command())
        # Juju 1.26 uses the destroy-controller command.
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(KILL_CONTROLLER, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   return_value=fake_popen):
            self.assertEqual(KILL_CONTROLLER, client.get_jes_command())
        # Juju 1.25 uses the 'system kill' command.
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(SYSTEM, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   return_value=fake_popen):
            self.assertEqual(
                SYSTEM, client.get_jes_command())

    def test_get_juju_timings(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        client.juju_timings = {("juju", "op1"): [1], ("juju", "op2"): [2]}
        flattened_timings = client.get_juju_timings()
        expected = {"juju op1": [1], "juju op2": [2]}
        self.assertEqual(flattened_timings, expected)

    def test_deploy_bundle_1X(self):
        client = EnvJujuClient1X(SimpleEnvironment('an_env', None),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju') as mock_juju:
            client.deploy_bundle('bundle:~juju-qa/some-bundle')
        mock_juju.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle'),
            False
        )

    def test_deployer(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle'),
            True
        )

    def test_deployer_with_bundle_name(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle', 'name')
        mock.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle',
                         'name'),
            True
        )

    def test_quickstart_maas(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'maas'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G arch=amd64', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_local(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), True, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_nonlocal(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'nonlocal'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_action_do(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            mock.return_value = \
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9"
            id = client.action_do("foo/0", "myaction", "param=5")
            self.assertEqual(id, "5a92ec93-d4be-4399-82dc-7431dbfd08f9")
        mock.assert_called_once_with(
            'action do', 'foo/0', 'myaction', "param=5"
        )

    def test_action_do_error(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            mock.return_value = "some bad text"
            with self.assertRaisesRegexp(Exception,
                                         "Action id not found in output"):
                client.action_do("foo/0", "myaction", "param=5")

    def test_action_fetch(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            mock.return_value = ret
            out = client.action_fetch("123")
            self.assertEqual(out, ret)
        mock.assert_called_once_with(
            'action fetch', '123', "--wait", "1m"
        )

    def test_action_fetch_timeout(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        ret = "status: pending\nfoo: bar"
        with patch.object(EnvJujuClient1X,
                          'get_juju_output', return_value=ret):
            with self.assertRaisesRegexp(Exception,
                                         "timed out waiting for action"):
                client.action_fetch("123")

    def test_action_do_fetch(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            # setting side_effect to an iterable will return the next value
            # from the list each time the function is called.
            mock.side_effect = [
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9",
                ret]
            out = client.action_do_fetch("foo/0", "myaction", "param=5")
            self.assertEqual(out, ret)

    def test_list_space(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        yaml_dict = {'foo': 'bar'}
        output = yaml.safe_dump(yaml_dict)
        with patch.object(client, 'get_juju_output', return_value=output,
                          autospec=True) as gjo_mock:
            result = client.list_space()
        self.assertEqual(result, yaml_dict)
        gjo_mock.assert_called_once_with('space list')

    def test_add_space(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_space('foo-space')
        juju_mock.assert_called_once_with('space create', ('foo-space'))

    def test_add_subnet(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_subnet('bar-subnet', 'foo-space')
        juju_mock.assert_called_once_with('subnet add',
                                          ('bar-subnet', 'foo-space'))

    def test__shell_environ_uses_pathsep(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None,
                                 'foo/bar/juju')
        with patch('os.pathsep', '!'):
            environ = client._shell_environ()
        self.assertRegexpMatches(environ['PATH'], r'foo/bar\!')

    def test_set_config(self):
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_config('foo', {'bar': 'baz'})
        juju_mock.assert_called_once_with('set', ('foo', 'bar=baz'))

    def test_get_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output',
                          side_effect=output) as gjo_mock:
            results = client.get_config('foo')
        self.assertEqual(expected, results)
        gjo_mock.assert_called_once_with('get', 'foo')

    def test_get_service_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output', side_effect=output):
            results = client.get_service_config('foo')
        self.assertEqual(expected, results)

    def test_get_service_config_timesout(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, '/foo')
        with patch('jujupy.until_timeout', return_value=range(0)):
            with self.assertRaisesRegexp(
                    Exception, 'Timed out waiting for juju get'):
                client.get_service_config('foo')


class TestUniquifyLocal(TestCase):

    def test_uniquify_local_empty(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        uniquify_local(env)
        self.assertEqual(env.config, {
            'type': 'local',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })

    def test_uniquify_local_preset(self):
        env = SimpleEnvironment('foo', {
            'type': 'local',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })
        uniquify_local(env)
        self.assertEqual(env.config, {
            'type': 'local',
            'api-port': 17072,
            'state-port': 37019,
            'storage-port': 8042,
            'syslog-port': 6516,
        })

    def test_uniquify_nonlocal(self):
        env = SimpleEnvironment('foo', {
            'type': 'nonlocal',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })
        uniquify_local(env)
        self.assertEqual(env.config, {
            'type': 'nonlocal',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })


@contextmanager
def bootstrap_context(client=None):
    # Avoid unnecessary syscalls.
    with patch('jujupy.check_free_disk_space'):
        with scoped_environ():
            with temp_dir() as fake_home:
                os.environ['JUJU_HOME'] = fake_home
                yield fake_home


class TestJesHomePath(TestCase):

    def test_jes_home_path(self):
        path = jes_home_path('/home/jrandom/foo', 'bar')
        self.assertEqual(path, '/home/jrandom/foo/jes-homes/bar')


class TestGetCachePath(TestCase):

    def test_get_cache_path(self):
        path = get_cache_path('/home/jrandom/foo')
        self.assertEqual(path, '/home/jrandom/foo/environments/cache.yaml')

    def test_get_cache_path_models(self):
        path = get_cache_path('/home/jrandom/foo', models=True)
        self.assertEqual(path, '/home/jrandom/foo/models/cache.yaml')


def stub_bootstrap(client):
    jenv_path = get_jenv_path(client.env.juju_home, 'qux')
    os.mkdir(os.path.dirname(jenv_path))
    with open(jenv_path, 'w') as f:
        f.write('Bogus jenv')


class TestMakeSafeConfig(TestCase):

    def test_default(self):
        client = FakeJujuClient(SimpleEnvironment('foo', {'type': 'bar'}))
        config = make_safe_config(client)
        self.assertEqual({
            'name': 'foo',
            'type': 'bar',
            'test-mode': True,
            'agent-version': '1.2-alpha3',
            }, config)

    def test_local(self):
        with temp_dir() as juju_home:
            env = SimpleEnvironment('foo', {'type': 'local'},
                                    juju_home=juju_home)
            client = FakeJujuClient(env)
            with patch('jujupy.check_free_disk_space'):
                config = make_safe_config(client)
        self.assertEqual(get_local_root(client.env.juju_home, client.env),
                         config['root-dir'])

    def test_bootstrap_replaces_agent_version(self):
        client = FakeJujuClient(SimpleEnvironment('foo', {'type': 'bar'}))
        client.bootstrap_replaces = {'agent-version'}
        self.assertNotIn('agent-version', make_safe_config(client))
        client.env.config['agent-version'] = '1.23'
        self.assertNotIn('agent-version', make_safe_config(client))


class TestTempBootstrapEnv(FakeHomeTestCase):

    @staticmethod
    def get_client(env):
        return EnvJujuClient24(env, '1.24-fake', 'fake-juju-path')

    def test_no_config_mangling_side_effect(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                stub_bootstrap(client)
        self.assertEqual(env.config, {'type': 'local'})

    def test_temp_bootstrap_env_environment(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        with bootstrap_context() as fake_home:
            client = self.get_client(env)
            agent_version = client.get_matching_agent_version()
            with temp_bootstrap_env(fake_home, client):
                temp_home = os.environ['JUJU_HOME']
                self.assertEqual(temp_home, os.environ['JUJU_DATA'])
                self.assertNotEqual(temp_home, fake_home)
                symlink_path = get_jenv_path(fake_home, 'qux')
                symlink_target = os.path.realpath(symlink_path)
                expected_target = os.path.realpath(
                    get_jenv_path(temp_home, 'qux'))
                self.assertEqual(symlink_target, expected_target)
                config = yaml.safe_load(
                    open(get_environments_path(temp_home)))
                self.assertEqual(config, {'environments': {'qux': {
                    'type': 'local',
                    'root-dir': get_local_root(fake_home, client.env),
                    'agent-version': agent_version,
                    'test-mode': True,
                    'name': 'qux',
                }}})
                stub_bootstrap(client)

    def test_temp_bootstrap_env_provides_dir(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        juju_home = os.path.join(self.home_dir, 'asdf')

        def side_effect(*args, **kwargs):
            os.mkdir(juju_home)
            return juju_home

        with patch('utility.mkdtemp', side_effect=side_effect):
            with patch('jujupy.check_free_disk_space', autospec=True):
                with temp_bootstrap_env(self.home_dir, client) as temp_home:
                    pass
        self.assertEqual(temp_home, juju_home)

    def test_temp_bootstrap_env_no_set_home(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        os.environ['JUJU_HOME'] = 'foo'
        os.environ['JUJU_DATA'] = 'bar'
        with patch('jujupy.check_free_disk_space', autospec=True):
            with temp_bootstrap_env(self.home_dir, client, set_home=False):
                self.assertEqual(os.environ['JUJU_HOME'], 'foo')
                self.assertEqual(os.environ['JUJU_DATA'], 'bar')

    def test_output(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                stub_bootstrap(client)
            jenv_path = get_jenv_path(fake_home, 'qux')
            self.assertFalse(os.path.islink(jenv_path))
            self.assertEqual(open(jenv_path).read(), 'Bogus jenv')

    def test_rename_on_exception(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with self.assertRaisesRegexp(Exception, 'test-rename'):
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)
                    raise Exception('test-rename')
            jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
            self.assertFalse(os.path.islink(jenv_path))
            self.assertEqual(open(jenv_path).read(), 'Bogus jenv')

    def test_exception_no_jenv(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with self.assertRaisesRegexp(Exception, 'test-rename'):
                with temp_bootstrap_env(fake_home, client):
                    jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
                    os.mkdir(os.path.dirname(jenv_path))
                    raise Exception('test-rename')
            jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
            self.assertFalse(os.path.lexists(jenv_path))

    def test_check_space_local_lxc(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        with bootstrap_context() as fake_home:
            client = self.get_client(env)
            with patch('jujupy.check_free_disk_space') as mock_cfds:
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)
        self.assertEqual(mock_cfds.mock_calls, [
            call(os.path.join(fake_home, 'qux'), 8000000, 'MongoDB files'),
            call('/var/lib/lxc', 2000000, 'LXC containers'),
        ])

    def test_check_space_local_kvm(self):
        env = SimpleEnvironment('qux', {'type': 'local', 'container': 'kvm'})
        with bootstrap_context() as fake_home:
            client = self.get_client(env)
            with patch('jujupy.check_free_disk_space') as mock_cfds:
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)
        self.assertEqual(mock_cfds.mock_calls, [
            call(os.path.join(fake_home, 'qux'), 8000000, 'MongoDB files'),
            call('/var/lib/uvtool/libvirt/images', 2000000, 'KVM disk files'),
        ])

    def test_error_on_jenv(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            jenv_path = get_jenv_path(fake_home, 'qux')
            os.mkdir(os.path.dirname(jenv_path))
            with open(jenv_path, 'w') as f:
                f.write('In the way')
            with self.assertRaisesRegexp(Exception, '.* already exists!'):
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)

    def test_not_permanent(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            client.env.juju_home = fake_home
            with temp_bootstrap_env(fake_home, client,
                                    permanent=False) as tb_home:
                stub_bootstrap(client)
            self.assertFalse(os.path.exists(tb_home))
            self.assertTrue(os.path.exists(get_jenv_path(fake_home,
                            client.env.environment)))
            self.assertFalse(os.path.exists(get_jenv_path(tb_home,
                             client.env.environment)))
        self.assertFalse(os.path.exists(tb_home))
        self.assertEqual(client.env.juju_home, fake_home)
        self.assertNotEqual(tb_home,
                            jes_home_path(fake_home, client.env.environment))

    def test_permanent(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            client.env.juju_home = fake_home
            with temp_bootstrap_env(fake_home, client,
                                    permanent=True) as tb_home:
                stub_bootstrap(client)
            self.assertTrue(os.path.exists(tb_home))
            self.assertFalse(os.path.exists(get_jenv_path(fake_home,
                             client.env.environment)))
            self.assertTrue(os.path.exists(get_jenv_path(tb_home,
                            client.env.environment)))
        self.assertFalse(os.path.exists(tb_home))
        self.assertEqual(client.env.juju_home, tb_home)


class TestStatus(FakeHomeTestCase):

    def test_iter_machines_no_containers(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar', 'containers': {'1/lxc/0': {'baz': 'qux'}}}
            },
            'services': {}}, '')
        self.assertEqual(list(status.iter_machines()),
                         [('1', status.status['machines']['1'])])

    def test_iter_machines_containers(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar', 'containers': {'1/lxc/0': {'baz': 'qux'}}}
            },
            'services': {}}, '')
        self.assertEqual(list(status.iter_machines(containers=True)), [
            ('1', status.status['machines']['1']),
            ('1/lxc/0', {'baz': 'qux'}),
        ])

    def test_agent_items_empty(self):
        status = Status({'machines': {}, 'services': {}}, '')
        self.assertItemsEqual([], status.agent_items())

    def test_agent_items(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar'}
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'subordinates': {
                                'sub': {'baz': 'qux'}
                            }
                        }
                    }
                }
            }
        }, '')
        expected = [
            ('1', {'foo': 'bar'}),
            ('jenkins/1', {'subordinates': {'sub': {'baz': 'qux'}}}),
            ('sub', {'baz': 'qux'})]
        self.assertItemsEqual(expected, status.agent_items())

    def test_agent_items_containers(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar', 'containers': {
                    '2': {'qux': 'baz'},
                }}
            },
            'services': {}
        }, '')
        expected = [
            ('1', {'foo': 'bar', 'containers': {'2': {'qux': 'baz'}}}),
            ('2', {'qux': 'baz'})
        ]
        self.assertItemsEqual(expected, status.agent_items())

    def test_get_service_count_zero(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
        }, '')
        self.assertEqual(0, status.get_service_count())

    def test_get_service_count(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                    }
                },
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {'agent-state': 'started'},
                    }
                },
                'juju-reports': {
                    'units': {
                        'juju-reports/0': {'agent-state': 'pending'},
                    }
                }
            }
        }, '')
        self.assertEqual(3, status.get_service_count())

    def test_get_service_unit_count_zero(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
        }, '')
        self.assertEqual(0, status.get_service_unit_count('jenkins'))

    def test_get_service_unit_count(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                        'jenkins/2': {'agent-state': 'bad'},
                        'jenkins/3': {'agent-state': 'bad'},
                    }
                }
            }
        }, '')
        self.assertEqual(3, status.get_service_unit_count('jenkins'))

    def test_get_unit(self):
        status = Status({
            'machines': {
                '1': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                    }
                },
                'dummy-sink': {
                    'units': {
                        'jenkins/2': {'agent-state': 'started'},
                    }
                },
            }
        }, '')
        self.assertEqual(
            status.get_unit('jenkins/1'), {'agent-state': 'bad'})
        self.assertEqual(
            status.get_unit('jenkins/2'), {'agent-state': 'started'})
        with self.assertRaisesRegexp(KeyError, 'jenkins/3'):
            status.get_unit('jenkins/3')

    def test_service_subordinate_units(self):
        status = Status({
            'machines': {
                '1': {},
            },
            'services': {
                'ubuntu': {},
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'subordinates': {
                                'chaos-monkey/0': {'agent-state': 'started'},
                            }
                        }
                    }
                },
                'dummy-sink': {
                    'units': {
                        'jenkins/2': {
                            'subordinates': {
                                'chaos-monkey/1': {'agent-state': 'started'}
                            }
                        },
                        'jenkins/3': {
                            'subordinates': {
                                'chaos-monkey/2': {'agent-state': 'started'}
                            }
                        }
                    }
                }
            }
        }, '')
        self.assertItemsEqual(
            status.service_subordinate_units('ubuntu'),
            [])
        self.assertItemsEqual(
            status.service_subordinate_units('jenkins'),
            [('chaos-monkey/0', {'agent-state': 'started'},)])
        self.assertItemsEqual(
            status.service_subordinate_units('dummy-sink'), [
                ('chaos-monkey/1', {'agent-state': 'started'}),
                ('chaos-monkey/2', {'agent-state': 'started'})]
            )

    def test_get_open_ports(self):
        status = Status({
            'machines': {
                '1': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                    }
                },
                'dummy-sink': {
                    'units': {
                        'jenkins/2': {'open-ports': ['42/tcp']},
                    }
                },
            }
        }, '')
        self.assertEqual(status.get_open_ports('jenkins/1'), [])
        self.assertEqual(status.get_open_ports('jenkins/2'), ['42/tcp'])

    def test_agent_states_with_agent_state(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                        'jenkins/2': {'agent-state': 'good'},
                    }
                }
            }
        }, '')
        expected = {
            'good': ['1', 'jenkins/2'],
            'bad': ['jenkins/1'],
            'no-agent': ['2'],
        }
        self.assertEqual(expected, status.agent_states())

    def test_agent_states_with_agent_status(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-status': {'current': 'bad'}},
                        'jenkins/2': {'agent-status': {'current': 'good'}},
                        'jenkins/3': {},
                    }
                }
            }
        }, '')
        expected = {
            'good': ['1', 'jenkins/2'],
            'bad': ['jenkins/1'],
            'no-agent': ['2', 'jenkins/3'],
        }
        self.assertEqual(expected, status.agent_states())

    def test_agent_states_with_juju_status(self):
        status = Status({
            'machines': {
                '1': {'juju-status': {'current': 'good'}},
                '2': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'juju-status': {'current': 'bad'}},
                        'jenkins/2': {'juju-status': {'current': 'good'}},
                        'jenkins/3': {},
                    }
                }
            }
        }, '')
        expected = {
            'good': ['1', 'jenkins/2'],
            'bad': ['jenkins/1'],
            'no-agent': ['2', 'jenkins/3'],
        }
        self.assertEqual(expected, status.agent_states())

    def test_check_agents_started_not_started(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                        'jenkins/2': {'agent-state': 'good'},
                    }
                }
            }
        }, '')
        self.assertEqual(status.agent_states(),
                         status.check_agents_started('env1'))

    def test_check_agents_started_all_started_with_agent_state(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
                '2': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'agent-state': 'started',
                            'subordinates': {
                                'sub1': {
                                    'agent-state': 'started'
                                }
                            }
                        },
                        'jenkins/2': {'agent-state': 'started'},
                    }
                }
            }
        }, '')
        self.assertIs(None, status.check_agents_started('env1'))

    def test_check_agents_started_all_started_with_agent_status(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
                '2': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'agent-status': {'current': 'idle'},
                            'subordinates': {
                                'sub1': {
                                    'agent-status': {'current': 'idle'}
                                }
                            }
                        },
                        'jenkins/2': {'agent-status': {'current': 'idle'}},
                    }
                }
            }
        }, '')
        self.assertIs(None, status.check_agents_started('env1'))

    def test_check_agents_started_agent_error(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'any-error'},
            },
            'services': {}
        }, '')
        with self.assertRaisesRegexp(ErroredUnit,
                                     '1 is in state any-error'):
            status.check_agents_started('env1')

    def do_check_agents_started_failure(self, failure):
        status = Status({
            'machines': {'0': {
                'agent-state-info': failure}},
            'services': {},
        }, '')
        with self.assertRaises(ErroredUnit) as e_cxt:
            status.check_agents_started()
        e = e_cxt.exception
        self.assertEqual(
            str(e), '0 is in state {}'.format(failure))
        self.assertEqual(e.unit_name, '0')
        self.assertEqual(e.state, failure)

    def test_check_agents_cannot_set_up_groups(self):
        self.do_check_agents_started_failure('cannot set up groups foobar')

    def test_check_agents_error(self):
        self.do_check_agents_started_failure('error executing "lxc-start"')

    def test_check_agents_cannot_run_instances(self):
        self.do_check_agents_started_failure('cannot run instances')

    def test_check_agents_cannot_run_instance(self):
        self.do_check_agents_started_failure('cannot run instance')

    def test_check_agents_started_agent_info_error(self):
        # Sometimes the error is indicated in a special 'agent-state-info'
        # field.
        status = Status({
            'machines': {
                '1': {'agent-state-info': 'any-error'},
            },
            'services': {}
        }, '')
        with self.assertRaisesRegexp(ErroredUnit,
                                     '1 is in state any-error'):
            status.check_agents_started('env1')

    def test_get_agent_versions(self):
        status = Status({
            'machines': {
                '1': {'agent-version': '1.6.2'},
                '2': {'agent-version': '1.6.1'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/0': {
                            'agent-version': '1.6.1'},
                        'jenkins/1': {},
                    },
                }
            }
        }, '')
        self.assertEqual({
            '1.6.2': {'1'},
            '1.6.1': {'jenkins/0', '2'},
            'unknown': {'jenkins/1'},
        }, status.get_agent_versions())

    def test_iter_new_machines(self):
        old_status = Status({
            'machines': {
                'bar': 'bar_info',
            }
        }, '')
        new_status = Status({
            'machines': {
                'foo': 'foo_info',
                'bar': 'bar_info',
            }
        }, '')
        self.assertItemsEqual(new_status.iter_new_machines(old_status),
                              [('foo', 'foo_info')])

    def test_get_instance_id(self):
        status = Status({
            'machines': {
                '0': {'instance-id': 'foo-bar'},
                '1': {},
            }
        }, '')
        self.assertEqual(status.get_instance_id('0'), 'foo-bar')
        with self.assertRaises(KeyError):
            status.get_instance_id('1')
        with self.assertRaises(KeyError):
            status.get_instance_id('2')

    def test_from_text(self):
        text = TestEnvJujuClient.make_status_yaml(
            'agent-state', 'pending', 'horsefeathers')
        status = Status.from_text(text)
        self.assertEqual(status.status_text, text)
        self.assertEqual(status.status, {
            'machines': {'0': {'agent-state': 'pending'}},
            'services': {'jenkins': {'units': {'jenkins/0': {
                'agent-state': 'horsefeathers'}}}}
        })

    def test_iter_units(self):
        started_unit = {'agent-state': 'started'}
        unit_with_subordinates = {
            'agent-state': 'started',
            'subordinates': {
                'ntp/0': started_unit,
                'nrpe/0': started_unit,
            },
        }
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/0': unit_with_subordinates,
                    }
                },
                'application': {
                    'units': {
                        'application/0': started_unit,
                        'application/1': started_unit,
                    }
                },
            }
        }, '')
        expected = [
            ('application/0', started_unit),
            ('application/1', started_unit),
            ('jenkins/0', unit_with_subordinates),
            ('nrpe/0', started_unit),
            ('ntp/0', started_unit),
        ]
        gen = status.iter_units()
        self.assertIsInstance(gen, types.GeneratorType)
        self.assertEqual(expected, list(gen))


def fast_timeout(count):
    if False:
        yield


@contextmanager
def temp_config():
    with temp_dir() as home:
        os.environ['JUJU_HOME'] = home
        environments_path = os.path.join(home, 'environments.yaml')
        with open(environments_path, 'w') as environments:
            yaml.dump({'environments': {
                'foo': {'type': 'local'}
            }}, environments)
        yield


class TestSimpleEnvironment(TestCase):

    def test_local_from_config(self):
        env = SimpleEnvironment('local', {'type': 'openstack'})
        self.assertFalse(env.local, 'Does not respect config type.')
        env = SimpleEnvironment('local', {'type': 'local'})
        self.assertTrue(env.local, 'Does not respect config type.')

    def test_kvm_from_config(self):
        env = SimpleEnvironment('local', {'type': 'local'})
        self.assertFalse(env.kvm, 'Does not respect config type.')
        env = SimpleEnvironment('local',
                                {'type': 'local', 'container': 'kvm'})
        self.assertTrue(env.kvm, 'Does not respect config type.')

    def test_from_config(self):
        with temp_config():
            env = SimpleEnvironment.from_config('foo')
            self.assertIs(SimpleEnvironment, type(env))
            self.assertEqual({'type': 'local'}, env.config)

    def test_from_bogus_config(self):
        with temp_config():
            with self.assertRaises(NoSuchEnvironment):
                SimpleEnvironment.from_config('bar')

    def test_from_config_none(self):
        with temp_config():
            os.environ['JUJU_ENV'] = 'foo'
            # GZ 2015-10-15: Currently default_env calls the juju on path here.
            with patch('jujuconfig.default_env', autospec=True,
                       return_value='foo') as cde_mock:
                env = SimpleEnvironment.from_config(None)
            self.assertEqual(env.environment, 'foo')
            cde_mock.assert_called_once_with()

    def test_juju_home(self):
        env = SimpleEnvironment('foo')
        self.assertIs(None, env.juju_home)
        env = SimpleEnvironment('foo', juju_home='baz')
        self.assertEqual('baz', env.juju_home)

    def test_make_jes_home(self):
        with temp_dir() as juju_home:
            with SimpleEnvironment('foo').make_jes_home(
                    juju_home, 'bar', {'baz': 'qux'}) as jes_home:
                pass
            with open(get_environments_path(jes_home)) as env_file:
                env = yaml.safe_load(env_file)
        self.assertEqual(env, {'baz': 'qux'})
        self.assertEqual(jes_home, jes_home_path(juju_home, 'bar'))

    def test_make_jes_home_clean_existing(self):
        env = SimpleEnvironment('foo')
        with temp_dir() as juju_home:
            with env.make_jes_home(juju_home, 'bar',
                                   {'baz': 'qux'}) as jes_home:
                foo_path = os.path.join(jes_home, 'foo')
                with open(foo_path, 'w') as foo:
                    foo.write('foo')
                self.assertTrue(os.path.isfile(foo_path))
            with env.make_jes_home(juju_home, 'bar',
                                   {'baz': 'qux'}) as jes_home:
                self.assertFalse(os.path.exists(foo_path))

    def test_dump_yaml(self):
        env = SimpleEnvironment('baz', {'type': 'qux'}, 'home')
        with temp_dir() as path:
            env.dump_yaml(path, {'foo': 'bar'})
            self.assertItemsEqual(
                ['environments.yaml'], os.listdir(path))
            with open(os.path.join(path, 'environments.yaml')) as f:
                self.assertEqual({'foo': 'bar'}, yaml.safe_load(f))


class TestJujuData(TestCase):

    def test_get_cloud_random_provider(self):
        self.assertEqual(
            'bar', JujuData('foo', {'type': 'bar'}, 'home').get_cloud())

    def test_get_cloud_ec2(self):
        self.assertEqual(
            'aws', JujuData('foo', {'type': 'ec2', 'region': 'bar'},
                            'home').get_cloud())
        self.assertEqual(
            'aws-china', JujuData('foo', {
                'type': 'ec2', 'region': 'cn-north-1'
                }, 'home').get_cloud())

    def test_get_cloud_gce(self):
        self.assertEqual(
            'google', JujuData('foo', {'type': 'gce', 'region': 'bar'},
                               'home').get_cloud())

    def test_get_cloud_maas(self):
        data = JujuData('foo', {'type': 'maas', 'maas-server': 'bar'}, 'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'maas', 'endpoint': 'bar'},
            'qux': {'type': 'maas', 'endpoint': 'qux'},
            }}
        self.assertEqual('baz', data.get_cloud())

    def test_get_cloud_maas_wrong_type(self):
        data = JujuData('foo', {'type': 'maas', 'maas-server': 'bar'}, 'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'foo', 'endpoint': 'bar'},
            }}
        with self.assertRaisesRegexp(LookupError, 'No such endpoint: bar'):
            self.assertEqual(data.get_cloud())

    def test_get_cloud_openstack(self):
        data = JujuData('foo', {'type': 'openstack', 'auth-url': 'bar'},
                        'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'openstack', 'endpoint': 'bar'},
            'qux': {'type': 'openstack', 'endpoint': 'qux'},
            }}
        self.assertEqual('baz', data.get_cloud())

    def test_get_cloud_openstack_wrong_type(self):
        data = JujuData('foo', {'type': 'openstack', 'auth-url': 'bar'},
                        'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'maas', 'endpoint': 'bar'},
            }}
        with self.assertRaisesRegexp(LookupError, 'No such endpoint: bar'):
            data.get_cloud()

    def test_get_region(self):
        self.assertEqual(
            'bar', JujuData('foo', {'type': 'foo', 'region': 'bar'},
                            'home').get_region())

    def test_get_region_old_azure(self):
        with self.assertRaisesRegexp(
                ValueError, 'Non-ARM Azure not supported.'):
            JujuData('foo', {'type': 'azure', 'location': 'bar'},
                     'home').get_region()

    def test_get_region_azure_arm(self):
        self.assertEqual('bar', JujuData('foo', {
            'type': 'azure', 'location': 'bar', 'tenant-id': 'baz'},
            'home').get_region())

    def test_get_region_joyent(self):
        self.assertEqual('bar', JujuData('foo', {
            'type': 'joyent', 'sdc-url': 'https://bar.api.joyentcloud.com'},
            'home').get_region())

    def test_get_region_lxd(self):
        self.assertEqual('localhost', JujuData('foo', {'type': 'lxd'},
                                               'home').get_region())

    def test_get_region_maas(self):
        self.assertIs(None, JujuData('foo', {'type': 'maas', 'region': 'bar'},
                                     'home').get_region())

    def test_get_region_manual(self):
        self.assertEqual('baz', JujuData('foo', {
            'type': 'manual', 'region': 'bar',
            'bootstrap-host': 'baz'}, 'home').get_region())

    def test_dump_yaml(self):
        cloud_dict = {'clouds': {'foo': {}}}
        credential_dict = {'credential': {'bar': {}}}
        data = JujuData('baz', {'type': 'qux'}, 'home')
        data.clouds = dict(cloud_dict)
        data.credentials = dict(credential_dict)
        with temp_dir() as path:
            data.dump_yaml(path, {})
            self.assertItemsEqual(
                ['clouds.yaml', 'credentials.yaml'], os.listdir(path))
            with open(os.path.join(path, 'clouds.yaml')) as f:
                self.assertEqual(cloud_dict, yaml.safe_load(f))
            with open(os.path.join(path, 'credentials.yaml')) as f:
                self.assertEqual(credential_dict, yaml.safe_load(f))

    def test_load_yaml(self):
        cloud_dict = {'clouds': {'foo': {}}}
        credential_dict = {'credential': {'bar': {}}}
        with temp_dir() as path:
            with open(os.path.join(path, 'clouds.yaml'), 'w') as f:
                yaml.safe_dump(cloud_dict, f)
            with open(os.path.join(path, 'credentials.yaml'), 'w') as f:
                yaml.safe_dump(credential_dict, f)
            data = JujuData('baz', {'type': 'qux'}, path)
            data.load_yaml()


class TestGroupReporter(TestCase):

    def test_single(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1")
        reporter.update({"done": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1\n")

    def test_single_ticks(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1 .")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1 ..")
        reporter.update({"done": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1 ..\n")

    def test_multiple_values(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.update({"working": ["1", "2"]})
        self.assertEqual(sio.getvalue(), "working: 1, 2")
        reporter.update({"working": ["1"], "done": ["2"]})
        self.assertEqual(sio.getvalue(), "working: 1, 2\nworking: 1")
        reporter.update({"done": ["1", "2"]})
        self.assertEqual(sio.getvalue(), "working: 1, 2\nworking: 1\n")

    def test_multiple_groups(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.update({"working": ["1", "2"], "starting": ["3"]})
        first = "starting: 3 | working: 1, 2"
        self.assertEqual(sio.getvalue(), first)
        reporter.update({"working": ["1", "3"], "done": ["2"]})
        second = "working: 1, 3"
        self.assertEqual(sio.getvalue(), "\n".join([first, second]))
        reporter.update({"done": ["1", "2", "3"]})
        self.assertEqual(sio.getvalue(), "\n".join([first, second, ""]))

    def test_finish(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1")
        reporter.finish()
        self.assertEqual(sio.getvalue(), "working: 1\n")

    def test_finish_unchanged(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        reporter.finish()
        self.assertEqual(sio.getvalue(), "")

    def test_wrap_to_width(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        for _ in range(150):
            reporter.update({"working": ["1"]})
        reporter.finish()
        self.assertEqual(sio.getvalue(), """\
working: 1 ....................................................................
...............................................................................
..
""")

    def test_wrap_to_width_exact(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.wrap_width = 12
        self.assertEqual(sio.getvalue(), "")
        changes = []
        for _ in range(20):
            reporter.update({"working": ["1"]})
            changes.append(sio.getvalue())
        self.assertEqual(changes[::4], [
            "working: 1",
            "working: 1 .\n...",
            "working: 1 .\n.......",
            "working: 1 .\n...........",
            "working: 1 .\n............\n...",
        ])
        reporter.finish()
        self.assertEqual(sio.getvalue(), changes[-1] + "\n")

    def test_wrap_to_width_overflow(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.wrap_width = 8
        self.assertEqual(sio.getvalue(), "")
        changes = []
        for _ in range(16):
            reporter.update({"working": ["1"]})
            changes.append(sio.getvalue())
        self.assertEqual(changes[::4], [
            "working: 1",
            "working: 1\n....",
            "working: 1\n........",
            "working: 1\n........\n....",
        ])
        reporter.finish()
        self.assertEqual(sio.getvalue(), changes[-1] + "\n")

    def test_wrap_to_width_multiple_groups(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.wrap_width = 16
        self.assertEqual(sio.getvalue(), "")
        changes = []
        for _ in range(6):
            reporter.update({"working": ["1", "2"]})
            changes.append(sio.getvalue())
        for _ in range(10):
            reporter.update({"working": ["1"], "done": ["2"]})
            changes.append(sio.getvalue())
        self.assertEqual(changes[::4], [
            "working: 1, 2",
            "working: 1, 2 ..\n..",
            "working: 1, 2 ..\n...\n"
            "working: 1 ..",
            "working: 1, 2 ..\n...\n"
            "working: 1 .....\n.",
        ])
        reporter.finish()
        self.assertEqual(sio.getvalue(), changes[-1] + "\n")


class TestMakeClient(TestCase):

    @contextmanager
    def make_client_cxt(self):
        td = temp_dir()
        te = temp_env({'environments': {'foo': {
            'orig-name': 'foo', 'name': 'foo'}}})
        with td as juju_path, te, patch('subprocess.Popen',
                                        side_effect=ValueError):
            with patch('subprocess.check_output') as co_mock:
                co_mock.return_value = '1.18'
                juju_path = os.path.join(juju_path, 'juju')
                yield juju_path

    def test_make_client(self):
        with self.make_client_cxt() as juju_path:
            client = make_client(juju_path, False, 'foo', 'bar')
        self.assertEqual(client.full_path, juju_path)
        self.assertEqual(client.debug, False)
        self.assertEqual(client.env.config['orig-name'], 'foo')
        self.assertEqual(client.env.config['name'], 'bar')
        self.assertEqual(client.env.environment, 'bar')

    def test_make_client_debug(self):
        with self.make_client_cxt() as juju_path:
            client = make_client(juju_path, True, 'foo', 'bar')
        self.assertEqual(client.debug, True)

    def test_make_client_no_temp_env_name(self):
        with self.make_client_cxt() as juju_path:
            client = make_client(juju_path, False, 'foo', None)
        self.assertEqual(client.full_path, juju_path)
        self.assertEqual(client.env.config['orig-name'], 'foo')
        self.assertEqual(client.env.config['name'], 'foo')
        self.assertEqual(client.env.environment, 'foo')


class AssessParseStateServerFromErrorTestCase(TestCase):

    def test_parse_new_state_server_from_error(self):
        output = dedent("""
            Waiting for address
            Attempting to connect to 10.0.0.202:22
            Attempting to connect to 1.2.3.4:22
            The fingerprint for the ECDSA key sent by the remote host is
            """)
        error = subprocess.CalledProcessError(1, ['foo'], output)
        address = parse_new_state_server_from_error(error)
        self.assertEqual('1.2.3.4', address)

    def test_parse_new_state_server_from_error_output_None(self):
        error = subprocess.CalledProcessError(1, ['foo'], None)
        address = parse_new_state_server_from_error(error)
        self.assertIs(None, address)

    def test_parse_new_state_server_from_error_no_output(self):
        address = parse_new_state_server_from_error(Exception())
        self.assertIs(None, address)


class TestGetMachineDNSName(TestCase):

    log_level = logging.DEBUG

    machine_0_no_addr = """\
        machines:
            "0":
                instance-id: pending
        """

    machine_0_hostname = """\
        machines:
            "0":
                dns-name: a-host
        """

    machine_0_ipv6 = """\
        machines:
            "0":
                dns-name: 2001:db8::3
        """

    def test_gets_host(self):
        status = Status.from_text(self.machine_0_hostname)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status]
        host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "a-host")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(self.log_stream.getvalue(), "")

    def test_retries_for_dns_name(self):
        status_pending = Status.from_text(self.machine_0_no_addr)
        status_host = Status.from_text(self.machine_0_hostname)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status_pending, status_host]
        host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "a-host")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(
            self.log_stream.getvalue(),
            "DEBUG No dns-name yet for machine 0\n")

    def test_retries_gives_up(self):
        status = Status.from_text(self.machine_0_no_addr)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status] * 3
        host = get_machine_dns_name(fake_client, '0', timeout=10)
        self.assertEqual(host, None)
        fake_client.status_until.assert_called_once_with(timeout=10)
        self.assertEqual(
            self.log_stream.getvalue(),
            "DEBUG No dns-name yet for machine 0\n" * 3)

    def test_gets_ipv6(self):
        status = Status.from_text(self.machine_0_ipv6)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status]
        host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "2001:db8::3")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(
            self.log_stream.getvalue(),
            "WARNING Selected IPv6 address for machine 0: '2001:db8::3'\n")

    def test_gets_ipv6_unsupported(self):
        status = Status.from_text(self.machine_0_ipv6)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status]
        with patch('utility.socket', wraps=socket) as wrapped_socket:
            del wrapped_socket.inet_pton
            host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "2001:db8::3")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(self.log_stream.getvalue(), "")
