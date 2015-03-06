from argparse import (
    ArgumentParser,
    Namespace,
    )
import json
import logging
import os
from StringIO import StringIO
import subprocess
import sys
from textwrap import dedent
from unittest import TestCase

from mock import (
    call,
    patch,
    )
import yaml

from deploy_stack import (
    add_juju_args,
    add_output_args,
    add_path_args,
    assess_juju_run,
    copy_remote_logs,
    deploy_dummy_stack,
    describe_instances,
    destroy_environment,
    destroy_job_instances,
    dump_env_logs,
    dump_logs,
    get_job_instances,
    get_juju_path,
    get_log_level,
    GET_TOKEN_SCRIPT,
    parse_euca,
    run_instances,
)
from jujupy import (
    EnvJujuClient,
    SimpleEnvironment,
)
from test_jujupy import assert_juju_call
from utility import temp_dir


def make_logs(log_dir):
    def write_dumped_files(*args):
        with open(os.path.join(log_dir, 'cloud.log'), 'w') as l:
            l.write('fake log')
        with open(os.path.join(log_dir, 'extra'), 'w') as l:
            l.write('not compressed')
    return write_dumped_files


def get_machine_addresses():
    return {
        '0': '10.10.0.1',
        '1': '10.10.0.11',
        '2': '10.10.0.22',
    }


class ArgParserTestCase(TestCase):

    def test_add_path_args(self):
        parser = ArgumentParser('proc')
        add_path_args(parser)
        cmd_line = ['proc', '--new-juju-bin', '/tmp/juju']
        with patch('sys.argv', cmd_line):
            args_dict = parser.parse_args().__dict__
        expected = {'run_startup': False, 'new_juju_bin': '/tmp/juju'}
        self.assertEqual(args_dict, expected)

    def test_add_path_args_new_juju_bin_default(self):
        parser = ArgumentParser('foo')
        add_path_args(parser)
        args = parser.parse_args([])
        self.assertIs(args.new_juju_bin, None)

    def test_add_output_args(self):
        parser = ArgumentParser('proc')
        add_output_args(parser)
        cmd_line = ['proc', '--debug', '--verbose']
        with patch('sys.argv', cmd_line):
            args_dict = parser.parse_args().__dict__
        expected = {'debug': True, 'verbose': True}
        self.assertEqual(args_dict, expected)

    def test_add_juju_args(self):
        parser = ArgumentParser('proc')
        add_juju_args(parser)
        cmd_line = ['proc', '--agent-url', 'some_url', '--series', 'vivid']
        with patch('sys.argv', cmd_line):
            args_dict = parser.parse_args().__dict__
        expected = {'agent_url': 'some_url', 'series': 'vivid'}
        self.assertEqual(args_dict, expected)

    def test_get_juju_path_new_juju_bin(self):
        args = Namespace(new_juju_bin='/tmp/juju', run_startup=False)
        juju_path = get_juju_path(args)
        self.assertEqual(juju_path, '/tmp/juju/juju')

    def test_get_juju_path_run_startup(self):
        args = Namespace(run_startup=True, env='proc')
        with patch.dict(os.environ, {'PATH': os.environ['PATH']}):
            with patch('subprocess.check_call', autospec=True) as cc_mock:
                with patch('subprocess.check_output',
                           return_value='foo') as co_mock:
                    juju_path = get_juju_path(args)
        env = dict(os.environ)
        scripts = os.path.dirname(os.path.abspath(sys.argv[0]))
        env['ENV'] = args.env
        cc_mock.assert_called_once_with(
            ['bash', '{}/common-startup.sh'.format(scripts)], env=env)
        co_mock.assert_called_once_with(
            ['find', 'extracted-bin', '-name', 'juju'])
        self.assertEqual(juju_path, os.path.abspath('foo'))

    def test_get_log_level_debug(self):
        parser = ArgumentParser('proc')
        add_output_args(parser)
        cmd_line = ['proc', '--debug']
        with patch('sys.argv', cmd_line):
            args = parser.parse_args()
        log_level = get_log_level(args)
        self.assertEqual(log_level, 20)

    def test_get_log_level_verbose(self):
        parser = ArgumentParser('proc')
        add_output_args(parser)
        cmd_line = ['proc', '--verbose']
        with patch('sys.argv', cmd_line):
            args = parser.parse_args()
        log_level = get_log_level(args)
        self.assertEqual(log_level, 10)


class DeployStackTestCase(TestCase):

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

    def test_destroy_job_instances_none(self):
        with patch('deploy_stack.get_job_instances',
                   return_value=[], autospec=True) as gji_mock:
            with patch('subprocess.check_call') as cc_mock:
                destroy_job_instances('foo')
        gji_mock.assert_called_with('foo')
        self.assertEqual(0, cc_mock.call_count)

    def test_destroy_job_instances_some(self):
        with patch('deploy_stack.get_job_instances',
                   return_value=['i-bar'], autospec=True) as gji_mock:
            with patch('subprocess.check_call') as cc_mock:
                destroy_job_instances('foo')
        gji_mock.assert_called_with('foo')
        cc_mock.assert_called_with(['euca-terminate-instances', 'i-bar'])

    def test_get_job_instances_none(self):
        with patch('deploy_stack.describe_instances',
                   return_value=[], autospec=True) as di_mock:
            ids = get_job_instances('foo')
        self.assertEqual([], [i for i in ids])
        di_mock.assert_called_with(job_name='foo', running=True)

    def test_get_job_instances_some(self):
        description = ('i-bar', 'foo-0')
        with patch('deploy_stack.describe_instances',
                   return_value=[description], autospec=True) as di_mock:
            ids = get_job_instances('foo')
        self.assertEqual(['i-bar'], [i for i in ids])
        di_mock.assert_called_with(job_name='foo', running=True)

    def test_describe_instances(self):
        with patch('subprocess.check_output',
                   return_value='', autospec=True) as co_mock:
            with patch('deploy_stack.parse_euca', autospec=True) as pe_mock:
                describe_instances(
                    instances=['i-foo'], job_name='bar', running=True)
        co_mock.assert_called_with(
            ['euca-describe-instances',
             '--filter', 'tag:job_name=bar',
             '--filter', 'instance-state-name=running',
             'i-foo'], env=None)
        pe_mock.assert_called_with('')

    def test_parse_euca(self):
        description = parse_euca('')
        self.assertEqual([], [d for d in description])
        euca_data = dedent("""
            header
            INSTANCE\ti-foo\tblah\tbar-0
            INSTANCE\ti-baz\tblah\tbar-1
        """)
        description = parse_euca(euca_data)
        self.assertEqual(
            [('i-foo', 'bar-0'), ('i-baz', 'bar-1')], [d for d in description])

    def test_run_instances(self):
        euca_data = dedent("""
            header
            INSTANCE\ti-foo\tblah\tbar-0
            INSTANCE\ti-baz\tblah\tbar-1
        """)
        description = [('i-foo', 'bar-0'), ('i-baz', 'bar-1')]
        with patch('subprocess.check_output',
                   return_value=euca_data, autospec=True) as co_mock:
            with patch('subprocess.check_call', autospec=True) as cc_mock:
                with patch('deploy_stack.describe_instances',
                           return_value=description, autospec=True) as di_mock:
                    run_instances(2, 'qux')
        co_mock.assert_called_once_with(
            ['euca-run-instances', '-k', 'id_rsa', '-n', '2',
             '-t', 'm1.large', '-g', 'manual-juju-test', 'ami-36aa4d5e'],
            env=os.environ)
        cc_mock.assert_called_once_with(
            ['euca-create-tags', '--tag', 'job_name=qux', 'i-foo', 'i-baz'],
            env=os.environ)
        di_mock.assert_called_once_with(['i-foo', 'i-baz'], env=os.environ)

    def test_run_instances_tagging_failed(self):
        euca_data = 'INSTANCE\ti-foo\tblah\tbar-0'
        description = [('i-foo', 'bar-0')]
        with patch('subprocess.check_output',
                   return_value=euca_data, autospec=True):
            with patch('subprocess.check_call', autospec=True,
                       side_effect=subprocess.CalledProcessError('', '')):
                with patch('deploy_stack.describe_instances',
                           return_value=description, autospec=True):
                    with patch('subprocess.call', autospec=True) as c_mock:
                        with self.assertRaises(subprocess.CalledProcessError):
                            run_instances(1, 'qux')
        c_mock.assert_called_with(['euca-terminate-instances', 'i-foo'])

    def test_run_instances_describe_failed(self):
        euca_data = 'INSTANCE\ti-foo\tblah\tbar-0'
        with patch('subprocess.check_output',
                   return_value=euca_data, autospec=True):
            with patch('deploy_stack.describe_instances',
                       side_effect=subprocess.CalledProcessError('', '')):
                with patch('subprocess.call', autospec=True) as c_mock:
                    with self.assertRaises(subprocess.CalledProcessError):
                            run_instances(1, 'qux')
        c_mock.assert_called_with(['euca-terminate-instances', 'i-foo'])

    def test_juju_run_test(self):
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


class DumpEnvLogsTestCase(TestCase):

    def setUp(self):
        self.log = logging.getLogger()
        self.old_handlers = self.log.handlers
        for handler in self.log.handlers:
            self.log.removeHandler(handler)
        self.stream = StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.log.addHandler(self.handler)
        self.handler.setLevel(logging.DEBUG)
        self.log.level = logging.DEBUG

        def reset_logger():
            self.log.removeHandler(self.handler)
            self.handler.close()
            for handler in self.old_handlers:
                self.log.addHandler(handler)
            self.log.level = logging.NOTSET

        self.addCleanup(reset_logger)

    def test_dump_env_logs_non_local_env(self):
        with temp_dir() as artifacts_dir:
            with patch('deploy_stack.get_machines_for_logs', autospec=True,
                       return_value=get_machine_addresses()) as gm_mock:
                with patch('deploy_stack.dump_logs', autospec=True) as dl_mock:
                    client = EnvJujuClient(
                        SimpleEnvironment(
                            'foo', {'type': 'nonlocal'}), '1.234-76', None)
                    dump_env_logs(client, '10.10.0.1', artifacts_dir)
            self.assertEqual(
                ['0', '1', '2'], sorted(os.listdir(artifacts_dir)))
        self.assertEqual(
            (client, '10.10.0.1'), gm_mock.call_args[0])
        call_list = sorted((cal[0], cal[1]) for cal in dl_mock.call_args_list)
        self.assertEqual(
            [((client, '10.10.0.1', '%s/0' % artifacts_dir),
              {'local_state_server': False}),
             ((client, '10.10.0.11', '%s/1' % artifacts_dir),
              {'local_state_server': False}),
             ((client, '10.10.0.22', '%s/2' % artifacts_dir),
              {'local_state_server': False})],
            call_list)
        self.assertEqual(
            ['Retrieving logs for machine-0', 'Retrieving logs for machine-1',
             'Retrieving logs for machine-2'],
            sorted(self.stream.getvalue().splitlines()))

    def test_dump_env_logs_local_env(self):
        with temp_dir() as artifacts_dir:
            with patch('deploy_stack.get_machines_for_logs', autospec=True,
                       return_value=get_machine_addresses()):
                with patch('deploy_stack.dump_logs', autospec=True) as dl_mock:
                    client = EnvJujuClient(
                        SimpleEnvironment(
                            'foo', {'type': 'local'}), '1.234-76', None)
                    dump_env_logs(client, '10.10.0.1', artifacts_dir)
        call_list = sorted((cal[0], cal[1]) for cal in dl_mock.call_args_list)
        self.assertEqual(
            [((client, '10.10.0.1', '%s/0' % artifacts_dir),
              {'local_state_server': True}),
             ((client, '10.10.0.11', '%s/1' % artifacts_dir),
              {'local_state_server': False}),
             ((client, '10.10.0.22', '%s/2' % artifacts_dir),
              {'local_state_server': False})],
            call_list)

    def test_dump_logs_with_local_state_server_false(self):
        # copy_remote_logs is called for non-local envs.
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'nonlocal'}), '1.234-76', None)
        with temp_dir() as log_dir:
            with patch('deploy_stack.copy_local_logs',
                       autospec=True) as cll_mock:
                with patch('deploy_stack.copy_remote_logs', autospec=True,
                           side_effect=make_logs(log_dir)) as crl_mock:
                    dump_logs(client, '10.10.0.1', log_dir,
                              local_state_server=False)
            self.assertEqual(['cloud.log.gz', 'extra'],
                             sorted(os.listdir(log_dir)))
        self.assertEqual(0, cll_mock.call_count)
        self.assertEqual(('10.10.0.1', log_dir), crl_mock.call_args[0])

    def test_dump_logs_with_local_state_server_true(self):
        # copy_local_logs is called for machine 0 in a local env.
        client = EnvJujuClient(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with temp_dir() as log_dir:
            with patch('deploy_stack.copy_local_logs', autospec=True,
                       side_effect=make_logs(log_dir)) as cll_mock:
                with patch('deploy_stack.copy_remote_logs',
                           autospec=True) as crl_mock:
                    dump_logs(client, '10.10.0.1', log_dir,
                              local_state_server=True)
            self.assertEqual(['cloud.log.gz', 'extra'],
                             sorted(os.listdir(log_dir)))
        self.assertEqual((log_dir, client), cll_mock.call_args[0])
        self.assertEqual(0, crl_mock.call_count)

    def test_copy_remote_logs(self):
        # To get the logs, their permissions must be updated first,
        # then downloaded in the order that they will be created
        # to ensure errors do not prevent some logs from being retrieved.
        with patch('deploy_stack.wait_for_port', autospec=True):
            with patch('subprocess.check_call') as cc_mock:
                copy_remote_logs('10.10.0.1', '/foo')
        self.assertEqual(
            (['timeout', '5m', 'ssh',
              '-o', 'UserKnownHostsFile /dev/null',
              '-o', 'StrictHostKeyChecking no',
              'ubuntu@10.10.0.1',
              'sudo chmod go+r /var/log/juju/*'], ),
            cc_mock.call_args_list[0][0])
        self.assertEqual(
            (['timeout', '5m', 'scp', '-C',
              '-o', 'UserKnownHostsFile /dev/null',
              '-o', 'StrictHostKeyChecking no',
              'ubuntu@10.10.0.1:/var/log/{cloud-init*.log,juju/*.log}',
              '/foo'],),
            cc_mock.call_args_list[1][0])

    def test_copy_remote_logs_with_errors(self):
        # Ssh and scp errors will happen when /var/log/juju doesn't exist yet,
        # but we log the case anc continue to retrieve as much as we can.
        def remote_op(*args, **kwargs):
            if 'ssh' in args:
                raise subprocess.CalledProcessError('ssh error', 'output')
            else:
                raise subprocess.CalledProcessError('scp error', 'output')

        with patch('subprocess.check_call', side_effect=remote_op) as cc_mock:
            with patch('deploy_stack.wait_for_port', autospec=True):
                copy_remote_logs('10.10.0.1', '/foo')
        self.assertEqual(2, cc_mock.call_count)
        self.assertEqual(
            ['Could not change the permission of the juju logs:',
             'None',
             'Could not retrieve some or all logs:',
             'None'],
            self.stream.getvalue().splitlines())


class TestDeployDummyStack(TestCase):

    def test_deploy_dummy_stack(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')

        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {'0': {'agent-state': 'started'}},
                'services': {}})
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
            'juju', '--show-log', 'deploy', '-e', 'foo',  'bar-dummy-source'),
            0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'set', '-e', 'foo',  'dummy-source',
            'token=fake-token'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'deploy', '-e', 'foo',  'bar-dummy-sink'), 2)
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
            'juju', '--show-log', 'ssh', '-e', 'foo', 'dummy-sink/0',
            GET_TOKEN_SCRIPT), 1, assign_stderr=True)
        self.assertEqual(co_mock.call_count, 2)


class TestTestUpgrade(TestCase):

    def test_test_upgrade(self):
        # Prevent nosetests from attempting to run test_upgrade directly.
        from deploy_stack import test_upgrade
        old_client = EnvJujuClient(SimpleEnvironment('foo', {'type': 'foo'}),
                               None, '/foo/juju')
        RUN_UNAME = (
            'juju', '--show-log', 'run', '-e', 'foo', '--format', 'json',
             '--machine', '1,2', 'uname')
        VERSION = ('/bar/juju', '--version')
        STATUS = ('juju', '--show-log', 'status', '-e', 'foo')
        GET_ENV = ('juju', '--show-log', 'get-env', '-e', 'foo',
                   'tools-metadata-url')
        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {'0': {
                    'agent-state': 'started',
                    'agent-version': '1.38'}},
                'services': {}})
            juju_run_out = json.dumps(
                    [{"MachineId": "1", "Stdout": "Linux\n"},
                    {"MachineId": "2", "Stdout": "Linux\n"}])
            output = {
                STATUS: status,
                RUN_UNAME: juju_run_out,
                VERSION: '1.38',
                GET_ENV: 'testing'
                }
            return output[args]

        with patch('subprocess.check_output', side_effect=output,
                   autospec=True) as co_mock:
            with patch('subprocess.check_call', autospec=True) as cc_mock:
                test_upgrade(old_client, '/bar/juju')
        new_client = EnvJujuClient(old_client.env,
                               None, '/bar/juju')
        assert_juju_call(self, co_mock, old_client, RUN_UNAME,
                         0, assign_stderr=True)
        assert_juju_call(self, cc_mock, new_client, (
            'juju', '--show-log', 'upgrade-juju', '-e', 'foo', '--version',
            '1.38'))
        self.assertEqual(co_mock.mock_calls[1], call(VERSION))
        assert_juju_call(self, co_mock, new_client, GET_ENV, 2,
                         assign_stderr=True)
        assert_juju_call(self, co_mock, new_client, GET_ENV, 3,
                         assign_stderr=True)
        assert_juju_call(self, co_mock, new_client, STATUS, 4,
                         assign_stderr=True)
        assert_juju_call(self, co_mock, old_client, RUN_UNAME, 5,
                         assign_stderr=True)
        self.assertEqual(co_mock.call_count, 6)
