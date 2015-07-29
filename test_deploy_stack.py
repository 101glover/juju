from argparse import (
    Namespace,
)
from contextlib import contextmanager
import json
import logging
import os
import subprocess
import sys
from unittest import (
    skipIf,
    TestCase
)

from mock import (
    call,
    patch,
)
import yaml

from deploy_stack import (
    assess_juju_run,
    boot_context,
    copy_local_logs,
    copy_remote_logs,
    deploy_dummy_stack,
    _deploy_job,
    deploy_job_parse_args,
    destroy_environment,
    dump_env_logs,
    dump_juju_timings,
    dump_logs,
    iter_remote_machines,
    get_remote_machines,
    GET_TOKEN_SCRIPT,
    assess_upgrade,
    safe_print_status,
    retain_jenv,
    update_env,
)
from jujuconfig import (
    get_juju_home,
)
from jujupy import (
    EnvJujuClient,
    SimpleEnvironment,
    Status,
)
from remote import (
    remote_from_address,
)
from test_jujupy import (
    assert_juju_call,
)
from utility import (
    setup_test_logging,
    temp_dir,
)


def make_logs(log_dir):
    def write_dumped_files(*args):
        with open(os.path.join(log_dir, 'cloud.log'), 'w') as l:
            l.write('fake log')
        with open(os.path.join(log_dir, 'extra'), 'w') as l:
            l.write('not compressed')
    return write_dumped_files


class DeployStackTestCase(TestCase):

    def setUp(self):
        setup_test_logging(self)

    def test_destroy_environment(self):
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client,
                          'destroy_environment', autospec=True) as de_mock:
            with patch('deploy_stack.destroy_job_instances',
                       autospec=True) as dji_mock:
                destroy_environment(client, 'foo')
        self.assertEqual(1, de_mock.call_count)
        self.assertEqual(0, dji_mock.call_count)

    def test_destroy_environment_with_manual_type_aws(self):
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'manual'}), '1.234-76', None)
        with patch.object(client,
                          'destroy_environment', autospec=True) as de_mock:
            with patch('deploy_stack.destroy_job_instances',
                       autospec=True) as dji_mock:
                with patch.dict(os.environ, {'AWS_ACCESS_KEY': 'bar'}):
                    destroy_environment(client, 'foo')
        self.assertEqual(1, de_mock.call_count)
        dji_mock.assert_called_with('foo')

    def test_destroy_environment_with_manual_type_non_aws(self):
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'manual'}), '1.234-76', None)
        with patch.object(client,
                          'destroy_environment', autospec=True) as de_mock:
            with patch('deploy_stack.destroy_job_instances',
                       autospec=True) as dji_mock:
                destroy_environment(client, 'foo')
        self.assertEqual(1, de_mock.call_count)
        self.assertEqual(0, dji_mock.call_count)

    def test_assess_juju_run(self):
        env = SimpleEnvironment('foo', {'type': 'nonlocal'})
        client = EnvJujuClient(env, None, None)
        response_ok = json.dumps(
            [{"MachineId": "1", "Stdout": "Linux\n"},
             {"MachineId": "2", "Stdout": "Linux\n"}])
        response_err = json.dumps([
            {"MachineId": "1", "Stdout": "Linux\n"},
            {"MachineId": "2",
             "Stdout": "Linux\n",
             "ReturnCode": 255,
             "Stderr": "Permission denied (publickey,password)"}])
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=response_ok):
            responses = assess_juju_run(client)
            for machine in responses:
                self.assertFalse(machine.get('ReturnCode', False))
                self.assertIn(machine.get('MachineId'), ["1", "2"])
            self.assertEqual(len(responses), 2)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=response_err):
            with self.assertRaises(ValueError):
                responses = assess_juju_run(client)

    def test_safe_print_status(self):
        env = SimpleEnvironment('foo', {'type': 'nonlocal'})
        client = EnvJujuClient(env, None, None)
        with patch.object(
                client, 'juju', autospec=True,
                side_effect=subprocess.CalledProcessError(
                    1, 'status', 'status error')
        ) as mock:
            safe_print_status(client)
        mock.assert_called_once_with('status', ())

    def test_update_env(self):
        env = SimpleEnvironment('foo', {'type': 'paas'})
        update_env(
            env, 'bar', series='wacky', bootstrap_host='baz',
            agent_url='url', agent_stream='devel')
        self.assertEqual('bar', env.environment)
        self.assertEqual('bar', env.config['name'])
        self.assertEqual('wacky', env.config['default-series'])
        self.assertEqual('baz', env.config['bootstrap-host'])
        self.assertEqual('url', env.config['tools-metadata-url'])
        self.assertEqual('devel', env.config['agent-stream'])

    def test_dump_juju_timings(self):
        env = SimpleEnvironment('foo', {'type': 'bar'})
        client = EnvJujuClient(env, None, None)
        client.juju_timings = {("juju", "op1"): [1], ("juju", "op2"): [2]}
        expected = {"juju op1": [1], "juju op2": [2]}
        with temp_dir() as fake_dir:
            dump_juju_timings(client, fake_dir)
            with open(os.path.join(fake_dir,
                      'juju_command_times.json')) as out_file:
                file_data = json.load(out_file)
        self.assertEqual(file_data, expected)


class DumpEnvLogsTestCase(TestCase):

    def setUp(self):
        setup_test_logging(self, level=logging.DEBUG)

    def assert_machines(self, expected, got):
        self.assertEqual(expected, dict((k, got[k].address) for k in got))

    r0 = remote_from_address('10.10.0.1')
    r1 = remote_from_address('10.10.0.11')
    r2 = remote_from_address('10.10.0.22')

    @classmethod
    def fake_remote_machines(cls):
        return {'0': cls.r0, '1': cls.r1, '2': cls.r2}

    def test_dump_env_logs_non_local_env(self):
        with temp_dir() as artifacts_dir:
            with patch('deploy_stack.get_remote_machines', autospec=True,
                       return_value=self.fake_remote_machines()) as gm_mock:
                with patch('deploy_stack.dump_logs', autospec=True) as dl_mock:
                    env = SimpleEnvironment('foo', {'type': 'nonlocal'})
                    client = EnvJujuClient(env, '1.234-76', None)
                    dump_env_logs(client, '10.10.0.1', artifacts_dir)
            self.assertEqual(
                ['machine-0', 'machine-1', 'machine-2'],
                sorted(os.listdir(artifacts_dir)))
        self.assertEqual(
            (client, '10.10.0.1'), gm_mock.call_args[0])
        call_list = sorted((cal[0], cal[1]) for cal in dl_mock.call_args_list)
        self.assertEqual(
            [((env, self.r0, '%s/machine-0' % artifacts_dir),
              {'local_state_server': False}),
             ((env, self.r1, '%s/machine-1' % artifacts_dir),
              {'local_state_server': False}),
             ((env, self.r2, '%s/machine-2' % artifacts_dir),
              {'local_state_server': False})],
            call_list)
        self.assertEqual(
            ['INFO Retrieving logs for machine-0 using ' + repr(self.r0),
             'INFO Retrieving logs for machine-1 using ' + repr(self.r1),
             'INFO Retrieving logs for machine-2 using ' + repr(self.r2)],
            sorted(self.log_stream.getvalue().splitlines()))

    def test_dump_env_logs_local_env(self):
        with temp_dir() as artifacts_dir:
            with patch('deploy_stack.get_remote_machines', autospec=True,
                       return_value=self.fake_remote_machines()):
                with patch('deploy_stack.dump_logs', autospec=True) as dl_mock:
                    env = SimpleEnvironment('foo', {'type': 'local'})
                    client = EnvJujuClient(env, '1.234-76', None)
                    dump_env_logs(client, '10.10.0.1', artifacts_dir)
        call_list = sorted((cal[0], cal[1]) for cal in dl_mock.call_args_list)
        self.assertEqual(
            [((env, self.r0, '%s/machine-0' % artifacts_dir),
              {'local_state_server': True}),
             ((env, self.r1, '%s/machine-1' % artifacts_dir),
              {'local_state_server': False}),
             ((env, self.r2, '%s/machine-2' % artifacts_dir),
              {'local_state_server': False})],
            call_list)

    def test_dump_logs_with_local_state_server_false(self):
        # copy_remote_logs is called for non-local envs.
        env = SimpleEnvironment('foo', {'type': 'nonlocal'})
        remote = object()
        with temp_dir() as log_dir:
            with patch('deploy_stack.copy_local_logs',
                       autospec=True) as cll_mock:
                with patch('deploy_stack.copy_remote_logs', autospec=True,
                           side_effect=make_logs(log_dir)) as crl_mock:
                    dump_logs(env, remote, log_dir,
                              local_state_server=False)
            self.assertEqual(['cloud.log.gz', 'extra'],
                             sorted(os.listdir(log_dir)))
        self.assertEqual(0, cll_mock.call_count)
        self.assertEqual((remote, log_dir), crl_mock.call_args[0])

    def test_dump_logs_with_local_state_server_true(self):
        # copy_local_logs is called for machine 0 in a local env.
        env = SimpleEnvironment('foo', {'type': 'local'})
        remote = object()
        with temp_dir() as log_dir:
            with patch('deploy_stack.copy_local_logs', autospec=True,
                       side_effect=make_logs(log_dir)) as cll_mock:
                with patch('deploy_stack.copy_remote_logs',
                           autospec=True) as crl_mock:
                    dump_logs(env, remote, log_dir,
                              local_state_server=True)
            self.assertEqual(['cloud.log.gz', 'extra'],
                             sorted(os.listdir(log_dir)))
        self.assertEqual((env, log_dir), cll_mock.call_args[0])
        self.assertEqual(0, crl_mock.call_count)

    def test_copy_local_logs(self):
        # Relevent local log files are copied, after changing their permissions
        # to allow access by non-root user.
        env = SimpleEnvironment('a-local', {'type': 'local'})
        with temp_dir() as juju_home_dir:
            log_dir = os.path.join(juju_home_dir, "a-local", "log")
            os.makedirs(log_dir)
            open(os.path.join(log_dir, "all-machines.log"), "w").close()
            template_dir = os.path.join(juju_home_dir, "templates")
            os.mkdir(template_dir)
            open(os.path.join(template_dir, "container.log"), "w").close()
            with patch('deploy_stack.get_juju_home', autospec=True,
                       return_value=juju_home_dir):
                with patch('deploy_stack.lxc_template_glob',
                           os.path.join(template_dir, "*.log")):
                    with patch('subprocess.check_call') as cc_mock:
                        copy_local_logs(env, '/destination/dir')
        expected_files = [os.path.join(juju_home_dir, *p) for p in (
            ('a-local', 'cloud-init-output.log'),
            ('a-local', 'log', 'all-machines.log'),
            ('templates', 'container.log'),
        )]
        self.assertEqual(cc_mock.call_args_list, [
            call(['sudo', 'chmod', 'go+r'] + expected_files),
            call(['cp'] + expected_files + ['/destination/dir']),
        ])

    def test_copy_remote_logs(self):
        # To get the logs, their permissions must be updated first,
        # then downloaded in the order that they will be created
        # to ensure errors do not prevent some logs from being retrieved.
        with patch('deploy_stack.wait_for_port', autospec=True):
            with patch('subprocess.check_output') as cc_mock:
                copy_remote_logs(remote_from_address('10.10.0.1'), '/foo')
        self.assertEqual(
            (['timeout', '5m', 'ssh',
              '-o', 'User ubuntu',
              '-o', 'UserKnownHostsFile /dev/null',
              '-o', 'StrictHostKeyChecking no',
              '10.10.0.1',
              'sudo chmod go+r /var/log/juju/*'], ),
            cc_mock.call_args_list[0][0])
        self.assertEqual(
            (['timeout', '5m', 'scp', '-C',
              '-o', 'User ubuntu',
              '-o', 'UserKnownHostsFile /dev/null',
              '-o', 'StrictHostKeyChecking no',
              '10.10.0.1:/var/log/cloud-init*.log',
              '10.10.0.1:/var/log/juju/*.log',
              '/foo'],),
            cc_mock.call_args_list[1][0])

    def test_copy_remote_logs_windows(self):
        remote = remote_from_address('10.10.0.1', series="win2012hvr2")
        with patch.object(remote, "copy", autospec=True) as copy_mock:
            copy_remote_logs(remote, '/foo')
        paths = [
            "%ProgramFiles(x86)%\\Cloudbase Solutions\\Cloudbase-Init\\log\\*",
            "C:\\Juju\\log\\juju\\*.log",
        ]
        copy_mock.assert_called_once_with("/foo", paths)

    def test_copy_remote_logs_with_errors(self):
        # Ssh and scp errors will happen when /var/log/juju doesn't exist yet,
        # but we log the case anc continue to retrieve as much as we can.
        def remote_op(*args, **kwargs):
            if 'ssh' in args:
                raise subprocess.CalledProcessError('ssh error', 'output')
            else:
                raise subprocess.CalledProcessError('scp error', 'output')

        with patch('subprocess.check_output', side_effect=remote_op) as co:
            with patch('deploy_stack.wait_for_port', autospec=True):
                copy_remote_logs(remote_from_address('10.10.0.1'), '/foo')
        self.assertEqual(2, co.call_count)
        self.assertEqual(
            ['WARNING Could not allow access to the juju logs:',
             'WARNING None',
             'WARNING Could not retrieve some or all logs:',
             'WARNING None'],
            self.log_stream.getvalue().splitlines())

    def test_get_machines_for_logs(self):
        client = EnvJujuClient(
            SimpleEnvironment('cloud', {'type': 'ec2'}), '1.23.4', None)
        status = Status.from_text("""\
            machines:
              "0":
                dns-name: 10.11.12.13
              "1":
                dns-name: 10.11.12.14
            """)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            machines = get_remote_machines(client, None)
        self.assert_machines(
            {'0': '10.11.12.13', '1': '10.11.12.14'}, machines)

    def test_get_machines_for_logs_with_boostrap_host(self):
        client = EnvJujuClient(
            SimpleEnvironment('cloud', {'type': 'ec2'}), '1.23.4', None)
        status = Status.from_text("""\
            machines:
              "0":
                instance-id: pending
            """)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            machines = get_remote_machines(client, '10.11.111.222')
        self.assert_machines({'0': '10.11.111.222'}, machines)

    def test_get_machines_for_logs_with_no_addresses(self):
        client = EnvJujuClient(
            SimpleEnvironment('cloud', {'type': 'ec2'}), '1.23.4', None)
        with patch.object(client, 'get_status', autospec=True,
                          side_effect=Exception):
            machines = get_remote_machines(client, '10.11.111.222')
        self.assert_machines({'0': '10.11.111.222'}, machines)

    @patch('subprocess.check_call')
    def test_get_remote_machines_with_maas(self, cc_mock):
        config = {
            'type': 'maas',
            'name': 'foo',
            'maas-server': 'http://bar/MASS/',
            'maas-oauth': 'baz'}
        client = EnvJujuClient(
            SimpleEnvironment('cloud', config), '1.23.4', None)
        status = Status.from_text("""\
            machines:
              "0":
                dns-name: node1.maas
              "1":
                dns-name: node2.maas
            """)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            allocated_ips = {
                'node1.maas': '10.11.12.13',
                'node2.maas': '10.11.12.14',
            }
            with patch('substrate.MAASAccount.get_allocated_ips',
                       autospec=True, return_value=allocated_ips):
                machines = get_remote_machines(client, 'node1.maas')
        self.assert_machines(
            {'0': '10.11.12.13', '1': '10.11.12.14'}, machines)

    def test_iter_remote_machines(self):
        client = EnvJujuClient(
            SimpleEnvironment('cloud', {'type': 'ec2'}), '1.23.4', None)
        status = Status.from_text("""\
            machines:
              "0":
                dns-name: 10.11.12.13
              "1":
                dns-name: 10.11.12.14
            """)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            machines = [(m, r.address)
                        for m, r in iter_remote_machines(client)]
        self.assertEqual(
            [('0', '10.11.12.13'), ('1', '10.11.12.14')], machines)

    def test_iter_remote_machines_with_series(self):
        client = EnvJujuClient(
            SimpleEnvironment('cloud', {'type': 'ec2'}), '1.23.4', None)
        status = Status.from_text("""\
            machines:
              "0":
                dns-name: 10.11.12.13
                series: trusty
              "1":
                dns-name: 10.11.12.14
                series: win2012hvr2
            """)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            machines = [(m, r.address, r.series)
                        for m, r in iter_remote_machines(client)]
        self.assertEqual(
            [('0', '10.11.12.13', 'trusty'),
             ('1', '10.11.12.14', 'win2012hvr2')], machines)

    def test_retain_jenv(self):
        with temp_dir() as jenv_dir:
            jenv_path = os.path.join(jenv_dir, "temp.jenv")
            with open(jenv_path, 'w') as f:
                f.write('jenv data')
                with temp_dir() as log_dir:
                    status = retain_jenv(jenv_path, log_dir)
                    self.assertIs(status, True)
                    self.assertEqual(['temp.jenv'], os.listdir(log_dir))

        with patch('shutil.copy', autospec=True,
                   side_effect=IOError) as rj_mock:
            status = retain_jenv('src', 'dst')
        self.assertIs(status, False)
        rj_mock.assert_called_with('src', 'dst')


class TestDeployDummyStack(TestCase):

    def setUp(self):
        setup_test_logging(self)

    def test_deploy_dummy_stack(self):
        env = SimpleEnvironment('foo', {'type': 'nonlocal'})
        client = EnvJujuClient(env, None, '/foo/juju')
        status = yaml.safe_dump({
            'machines': {'0': {'agent-state': 'started'}},
            'services': {
                'dummy-sink': {'units': {
                    'dummy-sink/0': {'agent-state': 'started'}}
                }
            }
        })

        def output(args, **kwargs):
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                ('juju', '--show-log', 'ssh', '-e', 'foo', 'dummy-sink/0',
                 GET_TOKEN_SCRIPT): 'fake-token',
            }
            return output[args]

        with patch('subprocess.check_output', side_effect=output,
                   autospec=True) as co_mock:
            with patch('subprocess.check_call', autospec=True) as cc_mock:
                with patch('deploy_stack.get_random_string',
                           return_value='fake-token', autospec=True):
                    with patch('sys.stdout', autospec=True):
                        deploy_dummy_stack(client, 'bar-')
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'deploy', '-e', 'foo', 'bar-dummy-source'),
            0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'set', '-e', 'foo', 'dummy-source',
            'token=fake-token'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'deploy', '-e', 'foo', 'bar-dummy-sink'), 2)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-relation', '-e', 'foo',
            'dummy-source', 'dummy-sink'), 3)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'expose', '-e', 'foo', 'dummy-sink'), 4)
        self.assertEqual(cc_mock.call_count, 5)
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'status', '-e', 'foo'), 0,
            assign_stderr=True)
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'status', '-e', 'foo'), 1,
            assign_stderr=True)
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'ssh', '-e', 'foo', 'dummy-sink/0',
            GET_TOKEN_SCRIPT), 2, assign_stderr=True)
        self.assertEqual(co_mock.call_count, 3)


def fake_SimpleEnvironment(name):
    return SimpleEnvironment(name, {})


def fake_EnvJujuClient(env, path=None, debug=None):
    return EnvJujuClient(env=env, version='1.2.3.4', full_path=path)


class TestDeployJob(TestCase):

    @skipIf(sys.platform in ('win32', 'darwin'),
            'Not supported on Windown and OS X')
    @patch('jujupy.EnvJujuClient.by_version', side_effect=fake_EnvJujuClient)
    @patch('jujupy.SimpleEnvironment.from_config',
           side_effect=fake_SimpleEnvironment)
    @patch('deploy_stack.boot_context', autospec=True)
    @patch('deploy_stack.EnvJujuClient.juju', autospec=True)
    def test_background_chaos_used(self, *args):
        with patch('deploy_stack.background_chaos', autospec=True) as bc_mock:
            with patch('deploy_stack.deploy_dummy_stack', autospec=True):
                with patch('deploy_stack.assess_juju_run', autospec=True):
                    _deploy_job('foo', None, None, '', None, None, None, 'log',
                                None, None, None, None, None, None, 1, False,
                                False,)
        self.assertEqual(bc_mock.mock_calls[0][1][0], 'foo')
        self.assertEqual(bc_mock.mock_calls[0][1][2], 'log')
        self.assertEqual(bc_mock.mock_calls[0][1][3], 1)

    @skipIf(sys.platform in ('win32', 'darwin'),
            'Not supported on Windown and OS X')
    @patch('jujupy.EnvJujuClient.by_version', side_effect=fake_EnvJujuClient)
    @patch('jujupy.SimpleEnvironment.from_config',
           side_effect=fake_SimpleEnvironment)
    @patch('deploy_stack.boot_context', autospec=True)
    @patch('deploy_stack.EnvJujuClient.juju', autospec=True)
    def test_background_chaos_not_used(self, *args):
        with patch('deploy_stack.background_chaos', autospec=True) as bc_mock:
            with patch('deploy_stack.deploy_dummy_stack', autospec=True):
                with patch('deploy_stack.assess_juju_run', autospec=True):
                    _deploy_job('foo', None, None, '', None, None, None, None,
                                None, None, None, None, None, None, 0, False,
                                False)
        self.assertEqual(bc_mock.call_count, 0)


class TestTestUpgrade(TestCase):

    RUN_UNAME = (
        'juju', '--show-log', 'run', '-e', 'foo', '--format', 'json',
        '--service', 'dummy-source,dummy-sink', 'uname')
    VERSION = ('/bar/juju', '--version')
    STATUS = ('juju', '--show-log', 'status', '-e', 'foo')
    GET_ENV = ('juju', '--show-log', 'get-env', '-e', 'foo',
               'tools-metadata-url')

    def setUp(self):
        setup_test_logging(self)

    @classmethod
    def upgrade_output(cls, args, **kwargs):
        status = yaml.safe_dump({
            'machines': {'0': {
                'agent-state': 'started',
                'agent-version': '1.38'}},
            'services': {}})
        juju_run_out = json.dumps([
            {"MachineId": "1", "Stdout": "Linux\n"},
            {"MachineId": "2", "Stdout": "Linux\n"}])
        output = {
            cls.STATUS: status,
            cls.RUN_UNAME: juju_run_out,
            cls.VERSION: '1.38',
            cls.GET_ENV: 'testing'
        }
        return output[args]

    @contextmanager
    def upgrade_mocks(self):
        with patch('subprocess.check_output', side_effect=self.upgrade_output,
                   autospec=True) as co_mock:
            with patch('subprocess.check_call', autospec=True) as cc_mock:
                with patch('deploy_stack.check_token', autospec=True):
                    with patch('deploy_stack.get_random_string',
                               return_value="FAKETOKEN", autospec=True):
                        with patch('sys.stdout', autospec=True):
                            yield (co_mock, cc_mock)

    def test_assess_upgrade(self):
        env = SimpleEnvironment('foo', {'type': 'foo'})
        old_client = EnvJujuClient(env, None, '/foo/juju')
        with self.upgrade_mocks() as (co_mock, cc_mock):
            assess_upgrade(old_client, '/bar/juju')
        new_client = EnvJujuClient(env, None, '/bar/juju')
        assert_juju_call(self, cc_mock, new_client, (
            'juju', '--show-log', 'upgrade-juju', '-e', 'foo', '--version',
            '1.38'), 0)
        assert_juju_call(self, cc_mock, new_client, (
            'juju', '--show-log', 'set', '-e', 'foo', 'dummy-source',
            'token=FAKETOKEN'), 1)
        self.assertEqual(cc_mock.call_count, 2)
        self.assertEqual(co_mock.mock_calls[0], call(self.VERSION))
        assert_juju_call(self, co_mock, new_client, self.GET_ENV, 1,
                         assign_stderr=True)
        assert_juju_call(self, co_mock, new_client, self.GET_ENV, 2,
                         assign_stderr=True)
        assert_juju_call(self, co_mock, new_client, self.STATUS, 3,
                         assign_stderr=True)
        assert_juju_call(self, co_mock, new_client, self.RUN_UNAME, 4,
                         assign_stderr=True)
        self.assertEqual(co_mock.call_count, 5)

    def test_mass_timeout(self):
        config = {'type': 'foo'}
        old_client = EnvJujuClient(SimpleEnvironment('foo', config),
                                   None, '/foo/juju')
        with self.upgrade_mocks():
            with patch.object(EnvJujuClient, 'wait_for_version') as wfv_mock:
                assess_upgrade(old_client, '/bar/juju')
            wfv_mock.assert_called_once_with('1.38', 600)
            config['type'] = 'maas'
            with patch.object(EnvJujuClient, 'wait_for_version') as wfv_mock:
                assess_upgrade(old_client, '/bar/juju')
        wfv_mock.assert_called_once_with('1.38', 1200)


class TestBootContext(TestCase):

    def setUp(self):
        self.addContext(patch('subprocess.Popen', side_effect=Exception))
        self.addContext(patch('sys.stdout'))

    def addContext(self, cxt):
        """Enter context manager for the remainder of the test, then leave.

        :return: The value emitted by cxt.__enter__.
        """
        result = cxt.__enter__()
        self.addCleanup(lambda: cxt.__exit__(None, None, None))
        return result

    def test_bootstrap_context(self):
        cc_mock = self.addContext(patch('subprocess.check_call'))
        client = EnvJujuClient(SimpleEnvironment(
            'foo', {'type': 'paas'}), '1.23', 'path')
        self.addContext(patch('deploy_stack.get_machine_dns_name',
                              return_value='foo'))
        c_mock = self.addContext(patch('subprocess.call'))
        dl_mock = self.addContext(patch('deploy_stack.dump_env_logs'))
        with boot_context('bar', client, None, [], None, None, None, 'log_dir',
                          keep_env=False, upload_tools=False):
            pass
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'bootstrap', '-e', 'bar', '--constraints',
            'mem=2G'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'status', '-e', 'bar'), 1)
        assert_juju_call(self, c_mock, client, (
            'timeout', '600.00s', 'juju', '--show-log', 'destroy-environment',
            'bar', '--force', '-y'))
        juju_home = get_juju_home()
        jenv_path = os.path.join(juju_home, 'environments', 'bar.jenv')
        dl_mock.assert_called_once_with(
            client, 'foo', 'log_dir', jenv_path=jenv_path)

    def test_keep_env(self):
        cc_mock = self.addContext(patch('subprocess.check_call'))
        client = EnvJujuClient(SimpleEnvironment(
            'foo', {'type': 'paas'}), '1.23', 'path')
        self.addContext(patch('deploy_stack.get_machine_dns_name',
                              return_value='foo'))
        c_mock = self.addContext(patch('subprocess.call'))
        self.addContext(patch('deploy_stack.dump_env_logs'))
        with boot_context('bar', client, None, [], None, None, None, None,
                          keep_env=True, upload_tools=False):
            pass
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'bootstrap', '-e', 'bar', '--constraints',
            'mem=2G'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'status', '-e', 'bar'), 1)
        self.assertEqual(c_mock.call_count, 0)

    def test_upload_tools(self):
        cc_mock = self.addContext(patch('subprocess.check_call'))
        client = EnvJujuClient(SimpleEnvironment(
            'foo', {'type': 'paas'}), '1.23', 'path')
        self.addContext(patch('deploy_stack.get_machine_dns_name',
                              return_value='foo'))
        self.addContext(patch('subprocess.call'))
        self.addContext(patch('deploy_stack.dump_env_logs'))
        with boot_context('bar', client, None, [], None, None, None, None,
                          keep_env=False, upload_tools=True):
            pass
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'bootstrap', '-e', 'bar', '--upload-tools',
            '--constraints', 'mem=2G'), 0)

    def test_calls_update_env(self):
        cc_mock = self.addContext(patch('subprocess.check_call'))
        client = EnvJujuClient(SimpleEnvironment(
            'foo', {'type': 'paas'}), '1.23', 'path')
        self.addContext(patch('deploy_stack.get_machine_dns_name',
                              return_value='foo'))
        self.addContext(patch('subprocess.call'))
        self.addContext(patch('deploy_stack.dump_env_logs'))
        ue_mock = self.addContext(
            patch('deploy_stack.update_env', wraps=update_env))
        with boot_context('bar', client, None, [], 'wacky', 'url', 'devel',
                          None, keep_env=False, upload_tools=False):
            pass
        ue_mock.assert_called_with(
            client.env, 'bar', series='wacky', bootstrap_host=None,
            agent_url='url', agent_stream='devel')
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'bootstrap', '-e', 'bar',
            '--constraints', 'mem=2G'), 0)

    def test_with_bootstrap_failure(self):
        client = EnvJujuClient(SimpleEnvironment(
            'foo', {'type': 'paas'}), '1.23', 'path')
        self.addContext(patch('deploy_stack.get_machine_dns_name',
                              return_value='foo'))
        self.addContext(patch('subprocess.check_call'))
        self.addContext(patch('subprocess.call'))
        self.addContext(patch('deploy_stack.wait_for_port'))
        self.addContext(patch.object(client, 'bootstrap',
                                     side_effect=Exception))
        dl_mock = self.addContext(patch('deploy_stack.dump_logs'))
        with self.assertRaises(Exception):
            with boot_context('bar', client, 'baz', [], None, None, None,
                              'log_dir', keep_env=False, upload_tools=True):
                pass
        dl_mock.assert_called_once_with(client, 'baz', 'log_dir', None)


class TestDeployJobParseArgs(TestCase):

    def test_deploy_job_parse_args(self):
        args = deploy_job_parse_args(['foo', 'bar', 'baz', 'qux'])
        self.assertEqual(args, Namespace(
            agent_stream=None,
            agent_url=None,
            bootstrap_host=None,
            debug=False,
            env='foo',
            temp_env_name='qux',
            keep_env=False,
            logs='baz',
            machine=[],
            juju_bin='bar',
            series=None,
            upgrade=False,
            verbose=logging.INFO,
            upload_tools=False,
            with_chaos=0,
            jes=False,
            pre_destroy=False,
        ))

    def test_upload_tools(self):
        args = deploy_job_parse_args(
            ['foo', 'bar', 'baz', 'qux', '--upload-tools'])
        self.assertEqual(args.upload_tools, True)

    def test_agent_stream(self):
        args = deploy_job_parse_args(
            ['foo', 'bar', 'baz', 'qux', '--agent-stream', 'wacky'])
        self.assertEqual('wacky', args.agent_stream)

    def test_jes(self):
        args = deploy_job_parse_args(
            ['foo', 'bar', 'baz', 'qux', '--jes'])
        self.assertIs(args.jes, True)
