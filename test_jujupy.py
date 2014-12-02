__metaclass__ = type

from contextlib import contextmanager
from datetime import (
    datetime,
    timedelta,
    )
import os
import shutil
import subprocess
import tempfile
from textwrap import dedent
from unittest import TestCase

from mock import (
    call,
    MagicMock,
    patch,
)
import yaml

from jujuconfig import (
    get_environments_path,
    get_jenv_path,
    )
from jujupy import (
    CannotConnectEnv,
    Environment,
    EnvJujuClient,
    ErroredUnit,
    format_listing,
    get_local_root,
    JujuClientDevel,
    SimpleEnvironment,
    Status,
    temp_bootstrap_env,
    uniquify_local,
)
from utility import (
    scoped_environ,
    temp_dir,
    )


def assert_juju_call(test_case, mock_method, client, expected_args,
                     call_index=None, assign_stderr=False):
    if call_index is None:
        test_case.assertEqual(len(mock_method.mock_calls), 1)
        call_index = 0
    empty, args, kwargs = mock_method.mock_calls[call_index]
    test_case.assertEqual(args, (expected_args,))
    kwarg_keys = ['env']
    if assign_stderr:
        kwarg_keys = ['stderr'] + kwarg_keys
        test_case.assertEqual(type(kwargs['stderr']), file)
    test_case.assertEqual(kwargs.keys(), kwarg_keys)
    bin_dir = os.path.dirname(client.full_path)
    test_case.assertRegexpMatches(kwargs['env']['PATH'],
                                  r'^{}\:'.format(bin_dir))


class TestErroredUnit(TestCase):

    def test_output(self):
        e = ErroredUnit('bar', 'baz')
        self.assertEqual('bar is in state baz', str(e))


class TestEnvJujuClient(TestCase):

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
        client = EnvJujuClient(SimpleEnvironment(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        self.assertEqual('1.23.1', client.get_matching_agent_version())
        self.assertEqual('1.23', client.get_matching_agent_version(
                         no_build=True))
        client.version = '1.20-beta1-series-arch'
        self.assertEqual('1.20-beta1.1', client.get_matching_agent_version())

    def test_upgrade_juju_nonlocal(self):
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'nonlocal'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234'))

    def test_upgrade_juju_local(self):
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234', '--upload-tools',))

    def test_upgrade_juju_no_force_version(self):
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju(force_version=False)
        juju_mock.assert_called_with(
            'upgrade-juju', ('--upload-tools',))

    def test_by_version(self):
        def juju_cmd_iterator():
            yield '1.17'
            yield '1.16'
            yield '1.16.1'
            yield '1.15'

        context = patch.object(
            EnvJujuClient, 'get_version',
            side_effect=juju_cmd_iterator().send)
        with context:
            self.assertIs(EnvJujuClient,
                          type(EnvJujuClient.by_version(None)))
            with self.assertRaisesRegexp(Exception, 'Unsupported juju: 1.16'):
                EnvJujuClient.by_version(None)
            with self.assertRaisesRegexp(Exception,
                                         'Unsupported juju: 1.16.1'):
                EnvJujuClient.by_version(None)
            client = EnvJujuClient.by_version(None)
            self.assertIs(EnvJujuClient, type(client))
            self.assertEqual('1.15', client.version)

    def test_by_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            client = EnvJujuClient.by_version(None, 'foo/bar/qux')
        vsn.assert_called_once_with(('foo/bar/qux', '--version'))
        self.assertNotEqual(client.full_path, 'foo/bar/qux')
        self.assertEqual(client.full_path, os.path.abspath('foo/bar/qux'))

    def test_full_args(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, 'my/juju/bin')
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
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        client.debug = True
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual((
            'juju', '--debug', 'bar', '-e', 'foo', 'baz', 'qux'), full)

    def test_bootstrap_hpcloud(self):
        env = Environment('hp', '')
        with patch.object(env, 'hpcloud', lambda: True):
            with patch.object(EnvJujuClient, 'juju') as mock:
                EnvJujuClient(env, None, None).bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_maas(self):
        env = Environment('maas', '')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client = EnvJujuClient(env, None, None)
            with patch.object(client.env, 'maas', lambda: True):
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G arch=amd64'), False)

    def test_bootstrap_non_sudo(self):
        env = Environment('foo', '')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client = EnvJujuClient(env, None, None)
            with patch.object(client.env, 'needs_sudo', lambda: False):
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_sudo(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), True)

    def test_bootstrap_upload_tools(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.bootstrap(upload_tools=True)
            mock.assert_called_with(
                'bootstrap', ('--upload-tools', '--constraints', 'mem=2G'),
                True)

    def test_destroy_environment_non_sudo(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: False):
            with patch.object(client, 'juju') as mock:
                client.destroy_environment()
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                False, check=False, include_e=False)

    def test_destroy_environment_sudo(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.destroy_environment()
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                True, check=False, include_e=False)

    def test_get_juju_output(self):
        env = Environment('foo', '')
        asdf = lambda x, stderr, env: 'asdf'
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_output', side_effect=asdf) as mock:
            result = client.get_juju_output('bar')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo'),),
                         mock.call_args[0])

    def test_get_juju_output_accepts_varargs(self):
        env = Environment('foo', '')
        asdf = lambda x, stderr, env: 'asdf'
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_output', side_effect=asdf) as mock:
            result = client.get_juju_output('bar', 'baz', '--qux')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo', 'baz',
                           '--qux'),), mock.call_args[0])

    def test_get_juju_output_stderr(self):
        def raise_without_stderr(args, stderr, env):
            stderr.write('Hello!')
            raise subprocess.CalledProcessError('a', 'b')
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.check_output', raise_without_stderr):
                client.get_juju_output('bar')
        self.assertEqual(exc.exception.stderr, 'Hello!')

    def test_get_juju_output_accepts_timeout(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_output') as sco_mock:
            client.get_juju_output('bar', timeout=5)
        self.assertEqual(
            sco_mock.call_args[0][0],
            ('timeout', '5.00s', 'juju', '--show-log', 'bar', '-e', 'foo'))

    def test_juju_output_supplies_path(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, '/foobar/bar')
        with patch('subprocess.check_output') as sco_mock:
            client.get_juju_output(env, 'baz')
        self.assertRegexpMatches(sco_mock.call_args[1]['env']['PATH'],
                                 r'/foobar\:')

    def test_get_status(self):
        output_text = yield dedent("""\
                - a
                - b
                - c
                """)
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=output_text):
            result = client.get_status()
        self.assertEqual(Status, type(result))
        self.assertEqual(['a', 'b', 'c'], result.status)

    def test_get_status_retries_on_error(self):
        env = Environment('foo', '')
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
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)

        def get_juju_output(command):
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output',
                          side_effect=get_juju_output):
            with patch('jujupy.until_timeout', lambda x: iter([None, None])):
                with self.assertRaisesRegexp(
                        Exception, 'Timed out waiting for juju status'):
                    client.get_status()

    def test_get_status_raises_on_timeout_2(self):
        env = SimpleEnvironment('foo')
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
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_joyent(self):
        env = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_wait_for_started(self):
        value = self.make_status_yaml('agent-state', 'started', 'started')
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_started()

    def test_wait_for_started_timeout(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x, start=None: range(1)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    with patch('logging.error'):
                        client.wait_for_started()

    def test_wait_for_started_start(self):
        value = self.make_status_yaml('agent-state', 'started', 'pending')
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    with patch('logging.error'):
                        client.wait_for_started(start=now - timedelta(1200))

    def test_wait_for_started_logs_status(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            with self.assertRaisesRegexp(
                    Exception,
                    'Timed out waiting for agents to start in local'):
                with patch('logging.error') as le_mock:
                    client.wait_for_started(0)
        le_mock.assert_called_once_with(value)

    def test_wait_for_ha(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
                '2': {'state-server-member-status': 'has-vote'},
                },
            'services': {},
            })
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
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
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch('sys.stdout'):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for voting to be enabled.'):
                    client.wait_for_ha(0.01)

    def test_wait_for_ha_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
                },
            'services': {},
            })
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for voting to be enabled.'):
                    client.wait_for_ha()

    def test_wait_for_version(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_timeout(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.1')
        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception, 'Some versions did not update'):
                    client.wait_for_version('1.17.2')

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

        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        output_real = 'test_jujupy.EnvJujuClient.get_juju_output'
        devnull = open(os.devnull, 'w')
        with patch('sys.stdout', devnull):
            with patch(output_real, get_juju_output_fake):
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

        client = EnvJujuClient(SimpleEnvironment('local'), None, None)
        output_real = 'test_jujupy.EnvJujuClient.get_juju_output'
        devnull = open(os.devnull, 'w')
        with patch('sys.stdout', devnull):
            with patch(output_real, get_juju_output_fake):
                with self.assertRaisesRegexp(Exception, 'foo'):
                    client.wait_for_version('1.17.2')

    def test_get_env_option(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_output') as mock:
            mock.return_value = 'https://example.org/juju/tools'
            result = client.get_env_option('tools-metadata-url')
        self.assertEqual(
            mock.call_args[0][0],
            ('juju', '--show-log', 'get-env', '-e', 'foo',
             'tools-metadata-url'))
        self.assertEqual('https://example.org/juju/tools', result)

    def test_set_env_option(self):
        env = Environment('foo', '')
        client = EnvJujuClient(env, None, None)
        with patch('subprocess.check_call') as mock:
            client.set_env_option(
                'tools-metadata-url', 'https://example.org/juju/tools')
        mock.assert_called_with(
            ('juju', '--show-log', 'set-env', '-e', 'foo',
             'tools-metadata-url=https://example.org/juju/tools'),
            env=os.environ)

    def test_juju(self):
        env = Environment('qux', '')
        client = EnvJujuClient(env, None, None)
        with patch('sys.stdout') as stdout_mock:
            with patch('subprocess.check_call') as mock:
                client.juju('foo', ('bar', 'baz'))
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'), env=os.environ)
        stdout_mock.flush.assert_called_with()

    def test_juju_env(self):
        env = Environment('qux', '')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_call') as cc_mock:
            client.juju('foo', ('bar', 'baz'))
        self.assertRegexpMatches(cc_mock.call_args[1]['env']['PATH'],
                                 r'/foobar\:')

    def test_juju_no_check(self):
        env = Environment('qux', '')
        client = EnvJujuClient(env, None, None)
        with patch('sys.stdout') as stdout_mock:
            with patch('subprocess.call') as mock:
                client.juju('foo', ('bar', 'baz'), check=False)
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'), env=os.environ)
        stdout_mock.flush.assert_called_with()

    def test_juju_no_check_env(self):
        env = Environment('qux', '')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.call') as call_mock:
            client.juju('foo', ('bar', 'baz'), check=False)
        self.assertRegexpMatches(call_mock.call_args[1]['env']['PATH'],
                                 r'/foobar\:')

    def test_juju_backup(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='foojuju-backup-24.tgzz') as co_mock:
            with patch('sys.stdout'):
                backup_file = client.backup()
        self.assertEqual(backup_file, os.path.abspath('juju-backup-24.tgz'))
        assert_juju_call(self, co_mock, client, ['juju', 'backup'])
        self.assertEqual(co_mock.mock_calls[0][2]['env']['JUJU_ENV'], 'qux')

    def test_juju_backup_no_file(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_output', return_value=''):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                with patch('sys.stdout'):
                    client.backup()

    def test_juju_backup_wrong_file(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='mumu-backup-24.tgz'):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                with patch('sys.stdout'):
                    client.backup()


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
def bootstrap_context(client):
    # Avoid unnecessary syscalls.
    with patch('jujupy.check_free_disk_space'):
        with scoped_environ():
            with temp_dir() as fake_home:
                os.environ['JUJU_HOME'] = fake_home
                yield fake_home


def stub_bootstrap(upload_tools=False):
    jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
    os.mkdir(os.path.dirname(jenv_path))
    with open(jenv_path, 'w') as f:
        f.write('Bogus jenv')


class TestTempJujuEnv(TestCase):

    def test_no_config_mangling_side_effect(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = EnvJujuClient.by_version(env)
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                stub_bootstrap()
        self.assertEqual(env.config, {'type': 'local'})

    def test_temp_bootstrap_env_environment(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = EnvJujuClient.by_version(env)
        agent_version = client.get_matching_agent_version()
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                temp_home = os.environ['JUJU_HOME']
                self.assertNotEqual(temp_home, fake_home)
                symlink_path = get_jenv_path(fake_home, 'qux')
                symlink_target = os.path.realpath(symlink_path)
                expected_target = get_jenv_path(temp_home, 'qux')
                self.assertEqual(symlink_target, expected_target)
                config = yaml.safe_load(
                    open(get_environments_path(temp_home)))
                self.assertEqual(config, {'environments': {'qux': {
                    'type': 'local',
                    'root-dir': get_local_root(fake_home, client.env),
                    'agent-version': agent_version,
                    'test-mode': True,
                    }}})
                stub_bootstrap()

    def test_output(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = EnvJujuClient.by_version(env)
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                stub_bootstrap()
            jenv_path = get_jenv_path(fake_home, 'qux')
            self.assertFalse(os.path.islink(jenv_path))
            self.assertEqual(open(jenv_path).read(), 'Bogus jenv')

    def test_rename_on_exception(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = EnvJujuClient.by_version(env)
        with bootstrap_context(client) as fake_home:
            with self.assertRaisesRegexp(Exception, 'test-rename'):
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap()
                    raise Exception('test-rename')
            jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
            self.assertFalse(os.path.islink(jenv_path))
            self.assertEqual(open(jenv_path).read(), 'Bogus jenv')

    def test_exception_no_jenv(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = EnvJujuClient.by_version(env)
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
        client = EnvJujuClient.by_version(env)
        with bootstrap_context(client) as fake_home:
            with patch('jujupy.check_free_disk_space') as mock_cfds:
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap()
        self.assertEqual(mock_cfds.mock_calls, [
            call(os.path.join(fake_home, 'qux'), 8000000, 'MongoDB files'),
            call('/var/lib/lxc', 2000000, 'LXC containers'),
            ])

    def test_check_space_local_kvm(self):
        env = SimpleEnvironment('qux', {'type': 'local', 'container': 'kvm'})
        client = EnvJujuClient.by_version(env)
        with bootstrap_context(client) as fake_home:
            with patch('jujupy.check_free_disk_space') as mock_cfds:
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap()
        self.assertEqual(mock_cfds.mock_calls, [
            call(os.path.join(fake_home, 'qux'), 8000000, 'MongoDB files'),
            call('/var/lib/uvtool/libvirt/images', 2000000, 'KVM disk files'),
            ])

    def test_error_on_jenv(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = EnvJujuClient.by_version(env)
        with bootstrap_context(client) as fake_home:
            jenv_path = get_jenv_path(fake_home, 'qux')
            os.mkdir(os.path.dirname(jenv_path))
            with open(jenv_path, 'w') as f:
                f.write('In the way')
            with self.assertRaisesRegexp(Exception, '.* already exists!'):
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap()


class TestJujuClientDevel(TestCase):

    def test_get_version(self):
        value = ' 5.6 \n'
        with patch('subprocess.check_output', return_value=value) as vsn:
            version = JujuClientDevel.get_version()
        self.assertEqual('5.6', version)
        vsn.assert_called_with(('juju', '--version'))

    def test_by_version(self):
        def juju_cmd_iterator():
            yield '1.17'
            yield '1.16'
            yield '1.16.1'
            yield '1.15'

        context = patch.object(
            JujuClientDevel, 'get_version',
            side_effect=juju_cmd_iterator().next)
        with context:
            self.assertIs(JujuClientDevel,
                          type(JujuClientDevel.by_version()))
            with self.assertRaisesRegexp(Exception, 'Unsupported juju: 1.16'):
                JujuClientDevel.by_version()
            with self.assertRaisesRegexp(Exception,
                                         'Unsupported juju: 1.16.1'):
                JujuClientDevel.by_version()
            client = JujuClientDevel.by_version()
            self.assertIs(JujuClientDevel, type(client))
            self.assertEqual('1.15', client.version)

    def test_bootstrap_hpcloud(self):
        env = Environment('hp', '')
        with patch.object(env, 'hpcloud', lambda: True):
            with patch.object(EnvJujuClient, 'juju') as mock:
                JujuClientDevel(None, None).bootstrap(env)
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_non_sudo(self):
        env = Environment('foo', '')
        with patch.object(SimpleEnvironment, 'needs_sudo', return_value=False):
            with patch.object(EnvJujuClient, 'juju') as mock:
                JujuClientDevel(None, None).bootstrap(env)
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_sudo(self):
        env = Environment('foo', '')
        client = JujuClientDevel(None, None)
        with patch.object(SimpleEnvironment, 'needs_sudo', return_value=True):
            with patch.object(EnvJujuClient, 'juju') as mock:
                client.bootstrap(env)
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), True)

    def test_destroy_environment_non_sudo(self):
        env = Environment('foo', '')
        client = JujuClientDevel(None, None)
        with patch.object(SimpleEnvironment, 'needs_sudo', return_value=False):
            with patch.object(EnvJujuClient, 'juju') as mock:
                client.destroy_environment(env)
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                False, check=False, include_e=False)

    def test_destroy_environment_sudo(self):
        env = Environment('foo', '')
        client = JujuClientDevel(None, None)
        with patch.object(SimpleEnvironment, 'needs_sudo', return_value=True):
            with patch.object(EnvJujuClient, 'juju') as mock:
                client.destroy_environment(env)
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                True, check=False, include_e=False)

    def test_get_juju_output(self):
        env = Environment('foo', '')
        asdf = lambda x, stderr, env: 'asdf'
        client = JujuClientDevel(None, None)
        with patch('subprocess.check_output', side_effect=asdf) as mock:
            result = client.get_juju_output(env, 'bar')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo'),),
                         mock.call_args[0])

    def test_get_juju_output_accepts_varargs(self):
        env = Environment('foo', '')
        asdf = lambda x, stderr, env: 'asdf'
        client = JujuClientDevel(None, None)
        with patch('subprocess.check_output', side_effect=asdf) as mock:
            result = client.get_juju_output(env, 'bar', 'baz', '--qux')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo', 'baz',
                           '--qux'),), mock.call_args[0])

    def test_get_juju_output_stderr(self):
        def raise_without_stderr(args, stderr, env):
            stderr.write('Hello!')
            raise subprocess.CalledProcessError('a', 'b')
        env = Environment('foo', '')
        client = JujuClientDevel(None, None)
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.check_output', raise_without_stderr):
                client.get_juju_output(env, 'bar')
        self.assertEqual(exc.exception.stderr, 'Hello!')

    def test_get_juju_output_accepts_timeout(self):
        env = Environment('foo', '')
        client = JujuClientDevel(None, None)
        with patch('subprocess.check_output') as sco_mock:
            client.get_juju_output(env, 'bar', timeout=5)
        self.assertEqual(
            sco_mock.call_args[0][0],
            ('timeout', '5.00s', 'juju', '--show-log', 'bar', '-e', 'foo'))

    def test_juju_output_supplies_path(self):
        env = Environment('foo', '')
        client = JujuClientDevel(None, '/foobar/bar')
        with patch('subprocess.check_output') as sco_mock:
            client.get_juju_output(env, 'baz')
        self.assertRegexpMatches(sco_mock.call_args[1]['env']['PATH'],
                                 r'/foobar\:')

    def test_get_status(self):
        output_text = yield dedent("""\
                - a
                - b
                - c
                """)
        client = JujuClientDevel(None, None)
        env = Environment('foo', '')
        with patch.object(EnvJujuClient, 'get_juju_output',
                          return_value=output_text):
            result = client.get_status(env)
        self.assertEqual(Status, type(result))
        self.assertEqual(['a', 'b', 'c'], result.status)

    def test_get_status_retries_on_error(self):
        client = JujuClientDevel(None, None)
        client.attempt = 0

        def get_juju_output(command, *args):
            if client.attempt == 1:
                return '"hello"'
            client.attempt += 1
            raise subprocess.CalledProcessError(1, command)

        env = Environment('foo', '')
        with patch.object(EnvJujuClient, 'get_juju_output', get_juju_output):
            client.get_status(env)

    def test_get_status_raises_on_timeout_1(self):
        client = JujuClientDevel(None, None)

        def get_juju_output(command):
            raise subprocess.CalledProcessError(1, command)

        env = Environment('foo', '')
        with patch.object(EnvJujuClient, 'get_juju_output',
                          side_effect=get_juju_output):
            with patch('jujupy.until_timeout', lambda x: iter([None, None])):
                with self.assertRaisesRegexp(
                        Exception, 'Timed out waiting for juju status'):
                    client.get_status(env)

    def test_get_status_raises_on_timeout_2(self):
        client = JujuClientDevel(None, None)
        env = Environment('foo', '')
        with patch('jujupy.until_timeout', return_value=iter([1])) as mock_ut:
            with patch.object(EnvJujuClient, 'get_juju_output',
                              side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    client.get_status(env, 500)
        mock_ut.assert_called_with(500)

    def test_get_env_option(self):
        client = JujuClientDevel(None, None)
        env = Environment('foo', '')
        with patch('subprocess.check_output') as mock:
            mock.return_value = 'https://example.org/juju/tools'
            result = client.get_env_option(env, 'tools-metadata-url')
        self.assertEqual(
            mock.call_args[0][0],
            ('juju', '--show-log', 'get-env', '-e', 'foo',
             'tools-metadata-url'))
        self.assertEqual('https://example.org/juju/tools', result)

    def test_set_env_option(self):
        client = JujuClientDevel(None, None)
        env = Environment('foo', '')
        with patch('subprocess.check_call') as mock:
            client.set_env_option(
                env, 'tools-metadata-url', 'https://example.org/juju/tools')
        mock.assert_called_with(
            ('juju', '--show-log', 'set-env', '-e', 'foo',
             'tools-metadata-url=https://example.org/juju/tools'),
            env=os.environ)

    def test_juju(self):
        env = Environment('qux', '')
        client = JujuClientDevel(None, None)
        with patch('sys.stdout') as stdout_mock:
            with patch('subprocess.check_call') as mock:
                client.juju(env, 'foo', ('bar', 'baz'))
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'), env=os.environ)
        stdout_mock.flush.assert_called_with()

    def test_juju_env(self):
        env = Environment('qux', '')
        client = JujuClientDevel(None, '/foobar/baz')
        with patch('subprocess.check_call') as cc_mock:
            client.juju(env, 'foo', ('bar', 'baz'))
        self.assertRegexpMatches(cc_mock.call_args[1]['env']['PATH'],
                                 r'/foobar\:')

    def test_juju_no_check(self):
        env = Environment('qux', '')
        client = JujuClientDevel(None, None)
        with patch('sys.stdout') as stdout_mock:
            with patch('subprocess.call') as mock:
                client.juju(env, 'foo', ('bar', 'baz'), check=False)
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'), env=os.environ)
        stdout_mock.flush.assert_called_with()

    def test_juju_no_check_env(self):
        env = Environment('qux', '')
        client = JujuClientDevel(None, '/foobar/baz')
        with patch('subprocess.call') as call_mock:
            client.juju(env, 'foo', ('bar', 'baz'), check=False)
        self.assertRegexpMatches(call_mock.call_args[1]['env']['PATH'],
                                 r'/foobar\:')


class TestStatus(TestCase):

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
                        'jenkins/1': {'baz': 'qux'}
                    }
                }
            }
        }, '')
        expected = [
            ('1', {'foo': 'bar'}), ('jenkins/1', {'baz': 'qux'})]
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

    def test_agent_states(self):
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

    def test_check_agents_started_all_started(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
                '2': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'started'},
                        'jenkins/2': {'agent-state': 'started'},
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


def fast_timeout(count):
    if False:
        yield


@contextmanager
def temp_config():
    home = tempfile.mkdtemp()
    try:
        environments_path = os.path.join(home, 'environments.yaml')
        old_home = os.environ.get('JUJU_HOME')
        os.environ['JUJU_HOME'] = home
        try:
            with open(environments_path, 'w') as environments:
                yaml.dump({'environments': {
                    'foo': {'type': 'local'}
                }}, environments)
            yield
        finally:
            if old_home is None:
                del os.environ['JUJU_HOME']
            else:
                os.environ['JUJU_HOME'] = old_home
    finally:
        shutil.rmtree(home)


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

    def test_hpcloud_from_config(self):
        env = SimpleEnvironment('cloud', {'auth-url': 'before.keystone.after'})
        self.assertFalse(env.hpcloud, 'Does not respect config type.')
        env = SimpleEnvironment('hp', {'auth-url': 'before.hpcloudsvc.after/'})
        self.assertTrue(env.hpcloud, 'Does not respect config type.')

    def test_from_config(self):
        with temp_config():
            env = SimpleEnvironment.from_config('foo')
            self.assertIs(SimpleEnvironment, type(env))
            self.assertEqual({'type': 'local'}, env.config)


class TestEnvironment(TestCase):

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

    def test_wait_for_started(self):
        value = self.make_status_yaml('agent-state', 'started', 'started')
        env = Environment('local', JujuClientDevel(None, None))
        with patch.object(EnvJujuClient, 'get_juju_output',
                          return_value=value):
            env.wait_for_started()

    def test_wait_for_started_timeout(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        env = Environment('local', JujuClientDevel(None, None))
        with patch('jujupy.until_timeout', lambda x, start=None: range(1)):
            with patch.object(EnvJujuClient, 'get_juju_output',
                              return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    with patch('logging.error'):
                        env.wait_for_started()

    def test_wait_for_version(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        env = Environment('local', JujuClientDevel(None, None))
        with patch.object(
                EnvJujuClient, 'get_juju_output', return_value=value):
            env.wait_for_version('1.17.2')

    def test_wait_for_version_timeout(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.1')
        env = Environment('local', JujuClientDevel(None, None))
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(EnvJujuClient, 'get_juju_output',
                              return_value=value):
                with self.assertRaisesRegexp(
                        Exception, 'Some versions did not update'):
                    env.wait_for_version('1.17.2')

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

        env = Environment('local', JujuClientDevel(None, None))
        output_real = 'test_jujupy.EnvJujuClient.get_juju_output'
        devnull = open(os.devnull, 'w')
        with patch('sys.stdout', devnull):
            with patch(output_real, get_juju_output_fake):
                env.wait_for_version('1.17.2')

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

        env = Environment('local', JujuClientDevel(None, None))
        output_real = 'test_jujupy.EnvJujuClient.get_juju_output'
        devnull = open(os.devnull, 'w')
        with patch('sys.stdout', devnull):
            with patch(output_real, get_juju_output_fake):
                with self.assertRaisesRegexp(Exception, 'foo'):
                    env.wait_for_version('1.17.2')

    def test_from_config(self):
        with temp_config():
            env = Environment.from_config('foo')
            self.assertIs(Environment, type(env))
            self.assertEqual({'type': 'local'}, env.config)

    def test_upgrade_juju_nonlocal(self):
        client = JujuClientDevel('1.234-76', None)
        env = Environment('foo', client, {'type': 'nonlocal'})
        env_client = client.get_env_client(env)
        with patch.object(client, 'get_env_client', return_value=env_client):
            with patch.object(env_client, 'juju') as juju_mock:
                env.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234'))

    def test_get_matching_agent_version(self):
        client = JujuClientDevel('1.23-series-arch', None)
        env = Environment('foo', client, {'type': 'local'})
        self.assertEqual('1.23.1', env.get_matching_agent_version())
        self.assertEqual('1.23', env.get_matching_agent_version(
                         no_build=True))
        env.client.version = '1.20-beta1-series-arch'
        self.assertEqual('1.20-beta1.1', env.get_matching_agent_version())

    def test_upgrade_juju_local(self):
        client = JujuClientDevel(None, None)
        env = Environment('foo', client, {'type': 'local'})
        env.client.version = '1.234-76'
        env_client = client.get_env_client(env)
        with patch.object(client, 'get_env_client', return_value=env_client):
            with patch.object(env_client, 'juju') as juju_mock:
                env.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234', '--upload-tools',))

    def test_deploy_non_joyent(self):
        env = Environment('foo', MagicMock(), {'type': 'local'})
        env.client.version = '1.234-76'
        env.deploy('mondogb')
        env.client.juju.assert_called_with(env, 'deploy', ('mondogb',))

    def test_deploy_joyent(self):
        env = Environment('foo', MagicMock(), {'type': 'joyent'})
        env.client.version = '1.234-76'
        env.deploy('mondogb')
        env.client.juju.assert_called_with(
            env, 'deploy', ('mondogb',))

    def test_set_testing_tools_metadata_url(self):
        client = JujuClientDevel(None, None)
        env = Environment('foo', client)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                env.set_testing_tools_metadata_url()
        mock_get.assert_called_with(env, 'tools-metadata-url')
        mock_set.assert_called_with(
            env, 'tools-metadata-url',
            'https://example.org/juju/testing/tools')

    def test_set_testing_tools_metadata_url_noop(self):
        client = JujuClientDevel(None, None)
        env = Environment('foo', client)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/testing/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                env.set_testing_tools_metadata_url()
        mock_get.assert_called_with(env, 'tools-metadata-url')
        self.assertEqual(0, mock_set.call_count)


class TestFormatListing(TestCase):

    def test_format_listing(self):
        result = format_listing(
            {'1': ['a', 'b'], '2': ['c'], 'expected': ['d']}, 'expected')
        self.assertEqual('1: a, b | 2: c', result)
