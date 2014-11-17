__metaclass__ = type

from argparse import Namespace
from contextlib import contextmanager
import os
from StringIO import StringIO
from tempfile import NamedTemporaryFile
from textwrap import dedent
from unittest import TestCase

from mock import (
    patch,
    call,
    )
import yaml

from industrial_test import (
    BackupRestoreAttempt,
    BootstrapAttempt,
    DeployManyAttempt,
    DestroyEnvironmentAttempt,
    EnsureAvailabilityAttempt,
    IndustrialTest,
    maybe_write_json,
    MultiIndustrialTest,
    parse_args,
    StageAttempt,
    )
from jujuconfig import get_euca_env
from jujupy import (
    EnvJujuClient,
    SimpleEnvironment,
    )
from test_jujupy import assert_juju_call
from test_substrate import get_aws_env


@contextmanager
def parse_error(test_case):
    stderr = StringIO()
    with test_case.assertRaises(SystemExit):
        with patch('sys.stderr', stderr):
            yield stderr


class TestParseArgs(TestCase):

    def test_parse_args(self):
        with parse_error(self) as stderr:
            args = parse_args([])
        self.assertRegexpMatches(
            stderr.getvalue(), '.*error: too few arguments.*')
        with parse_error(self) as stderr:
            args = parse_args(['env'])
        self.assertRegexpMatches(
            stderr.getvalue(), '.*error: too few arguments.*')
        args = parse_args(['rai', 'new-juju'])
        self.assertEqual(args.env, 'rai')
        self.assertEqual(args.new_juju_path, 'new-juju')

    def test_parse_args_attempts(self):
        args = parse_args(['rai', 'new-juju'])
        self.assertEqual(args.attempts, 2)
        args = parse_args(['rai', 'new-juju', '--attempts', '3'])
        self.assertEqual(args.attempts, 3)

    def test_parse_args_json_file(self):
        args = parse_args(['rai', 'new-juju'])
        self.assertIs(args.json_file, None)
        args = parse_args(['rai', 'new-juju', '--json-file', 'foobar'])
        self.assertEqual(args.json_file, 'foobar')

    def test_parse_args_quick(self):
        args = parse_args(['rai', 'new-juju'])
        self.assertEqual(args.quick, False)
        args = parse_args(['rai', 'new-juju', '--quick'])
        self.assertEqual(args.quick, True)

    def test_parse_args_agent_url(self):
        args = parse_args(['rai', 'new-juju'])
        self.assertEqual(args.new_agent_url, None)
        args = parse_args(['rai', 'new-juju', '--new-agent-url',
                           'http://example.org'])
        self.assertEqual(args.new_agent_url, 'http://example.org')


class FakeAttempt:

    def __init__(self, old_result, new_result, test_id='foo-id'):
        self.result = old_result, new_result
        self.test_id = test_id

    def do_stage(self, old_client, new_client):
        return self.result

    def iter_test_results(self, old, new):
        old_result, new_result = self.do_stage(old, new)
        yield self.test_id, old_result, new_result


class FakeAttemptClass:

    def __init__(self, title, *result):
        self.title = title
        self.test_id = '{}-id'.format(title)
        self.result = result

    def get_test_info(self):
        return {self.test_id: {'title': self.title}}

    def __call__(self):
        return FakeAttempt(*self.result, test_id=self.test_id)


class StubJujuClient:

    def destroy_environment(self):
        pass


class TestMultiIndustrialTest(TestCase):

    def test_from_args(self):
        args = Namespace(env='foo', new_juju_path='new-path', attempts=7,
                         quick=True, new_agent_url=None)
        mit = MultiIndustrialTest.from_args(args)
        self.assertEqual(mit.env, 'foo')
        self.assertEqual(mit.new_juju_path, 'new-path')
        self.assertEqual(mit.attempt_count, 7)
        self.assertEqual(mit.max_attempts, 14)
        self.assertEqual(
            mit.stages, [BootstrapAttempt, DestroyEnvironmentAttempt])
        args = Namespace(env='bar', new_juju_path='new-path2', attempts=6,
                         quick=False, new_agent_url=None)
        mit = MultiIndustrialTest.from_args(args)
        self.assertEqual(mit.env, 'bar')
        self.assertEqual(mit.new_juju_path, 'new-path2')
        self.assertEqual(mit.attempt_count, 6)
        self.assertEqual(mit.max_attempts, 12)
        self.assertEqual(
            mit.stages, [
                BootstrapAttempt, DeployManyAttempt, BackupRestoreAttempt,
                EnsureAvailabilityAttempt, DestroyEnvironmentAttempt])

    def test_from_args_new_agent_url(self):
        args = Namespace(env='foo', new_juju_path='new-path', attempts=7,
                         quick=True, new_agent_url='http://example.net')
        mit = MultiIndustrialTest.from_args(args)
        self.assertEqual(mit.new_agent_url, 'http://example.net')

    def test_init(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            DestroyEnvironmentAttempt, BootstrapAttempt], 5)
        self.assertEqual(mit.env, 'foo-env')
        self.assertEqual(mit.new_juju_path, 'bar-path')
        self.assertEqual(mit.stages, [DestroyEnvironmentAttempt,
                                      BootstrapAttempt])
        self.assertEqual(mit.attempt_count, 5)

    def test_make_results(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            DestroyEnvironmentAttempt, BootstrapAttempt], 5)
        results = mit.make_results()
        self.assertEqual(results, {'results': [
            {'attempts': 0, 'old_failures': 0, 'new_failures': 0,
             'title': 'destroy environment', 'test_id': 'destroy-env'},
            {'attempts': 0, 'old_failures': 0, 'new_failures': 0,
             'title': 'check substrate clean', 'test_id': 'substrate-clean'},
            {'attempts': 0, 'old_failures': 0, 'new_failures': 0,
             'title': 'bootstrap', 'test_id': 'bootstrap'},
        ]})

    def test_make_industrial_test(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            DestroyEnvironmentAttempt, BootstrapAttempt], 5)
        side_effect = lambda x, y=None: (x, y)
        with patch('jujupy.EnvJujuClient.by_version', side_effect=side_effect):
            with patch('jujupy.SimpleEnvironment.from_config',
                       side_effect=lambda x: SimpleEnvironment(x, {})):
                industrial = mit.make_industrial_test()
        self.assertEqual(industrial.old_client,
                         (SimpleEnvironment('foo-env-old', {}), None))
        self.assertEqual(industrial.new_client,
                         (SimpleEnvironment('foo-env-new', {}), 'bar-path'))
        self.assertEqual(len(industrial.stage_attempts), 2)
        for stage, attempt in zip(mit.stages, industrial.stage_attempts):
            self.assertIs(type(attempt), stage)

    def test_make_industrial_test_new_agent_url(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [],
                                  new_agent_url='http://example.com')
        side_effect = lambda x, y=None: (x, y)
        with patch('jujupy.EnvJujuClient.by_version', side_effect=side_effect):
            with patch('jujupy.SimpleEnvironment.from_config',
                       side_effect=lambda x: SimpleEnvironment(x, {})):
                industrial = mit.make_industrial_test()
        self.assertEqual(
            industrial.new_client, (
                SimpleEnvironment('foo-env-new', {
                    'tools-metadata-url': 'http://example.com'}),
                'bar-path')
            )

    def test_update_results(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            DestroyEnvironmentAttempt, BootstrapAttempt], 2)
        results = mit.make_results()
        mit.update_results([('destroy-env', True, False)], results)
        expected = {'results': [
            {'title': 'destroy environment', 'test_id': 'destroy-env',
             'attempts': 1, 'new_failures': 1, 'old_failures': 0},
            {'title': 'check substrate clean', 'test_id': 'substrate-clean',
             'attempts': 0, 'new_failures': 0, 'old_failures': 0},
            {'title': 'bootstrap', 'test_id': 'bootstrap', 'attempts': 0,
             'new_failures': 0, 'old_failures': 0},
            ]}
        self.assertEqual(results, expected)
        mit.update_results(
            [('destroy-env', True, True), ('substrate-clean', True, True),
             ('bootstrap', False, True)],
            results)
        self.assertEqual(results, {'results': [
            {'title': 'destroy environment', 'test_id': 'destroy-env',
             'attempts': 2, 'new_failures': 1, 'old_failures': 0},
            {'title': 'check substrate clean', 'test_id': 'substrate-clean',
             'attempts': 1, 'new_failures': 0, 'old_failures': 0},
            {'title': 'bootstrap', 'test_id': 'bootstrap', 'attempts': 1,
             'new_failures': 0, 'old_failures': 1},
            ]})
        mit.update_results(
            [('destroy-env', False, False), ('substrate-clean', True, True),
             ('bootstrap', False, False)],
            results)
        expected = {'results': [
            {'title': 'destroy environment', 'test_id': 'destroy-env',
             'attempts': 2, 'new_failures': 1, 'old_failures': 0},
            {'title': 'check substrate clean', 'test_id': 'substrate-clean',
             'attempts': 2, 'new_failures': 0, 'old_failures': 0},
            {'title': 'bootstrap', 'test_id': 'bootstrap', 'attempts': 2,
             'new_failures': 1, 'old_failures': 2},
            ]}
        self.assertEqual(results, expected)

    def test_run_tests(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            FakeAttemptClass('foo', True, True),
            FakeAttemptClass('bar', True, False),
            ], 5, 10)
        side_effect = lambda x, y=None: StubJujuClient()
        with patch('jujupy.EnvJujuClient.by_version', side_effect=side_effect):
            with patch('jujupy.SimpleEnvironment.from_config',
                       side_effect=lambda x: SimpleEnvironment(x, {})):
                results = mit.run_tests()
        self.assertEqual(results, {'results': [
            {'title': 'foo', 'test_id': 'foo-id', 'attempts': 5,
             'old_failures': 0, 'new_failures': 0},
            {'title': 'bar', 'test_id': 'bar-id', 'attempts': 5,
             'old_failures': 0, 'new_failures': 5},
            ]})

    def test_run_tests_max_attempts(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            FakeAttemptClass('foo', True, False),
            FakeAttemptClass('bar', True, False),
            ], 5, 6)
        side_effect = lambda x, y=None: StubJujuClient()
        with patch('jujupy.EnvJujuClient.by_version', side_effect=side_effect):
            with patch('jujupy.SimpleEnvironment.from_config',
                       side_effect=lambda x: SimpleEnvironment(x, {})):
                results = mit.run_tests()
        self.assertEqual(results, {'results': [
            {'title': 'foo', 'test_id': 'foo-id', 'attempts': 5,
             'old_failures': 0, 'new_failures': 5},
            {'title': 'bar', 'test_id': 'bar-id', 'attempts': 0,
             'old_failures': 0, 'new_failures': 0},
            ]})

    def test_run_tests_max_attempts_less_than_attempt_count(self):
        mit = MultiIndustrialTest('foo-env', 'bar-path', [
            FakeAttemptClass('foo', True, False),
            FakeAttemptClass('bar', True, False),
            ], 5, 4)
        side_effect = lambda x, y=None: StubJujuClient()
        with patch('jujupy.EnvJujuClient.by_version', side_effect=side_effect):
            with patch('jujupy.SimpleEnvironment.from_config',
                       side_effect=lambda x: SimpleEnvironment(x, {})):
                results = mit.run_tests()
        self.assertEqual(results, {'results': [
            {'title': 'foo', 'test_id': 'foo-id', 'attempts': 4,
             'old_failures': 0, 'new_failures': 4},
            {'title': 'bar', 'test_id': 'bar-id', 'attempts': 0,
             'old_failures': 0, 'new_failures': 0},
            ]})

    def test_results_table(self):
        results = [
            {'title': 'foo', 'attempts': 5, 'old_failures': 1,
             'new_failures': 2},
            {'title': 'bar', 'attempts': 5, 'old_failures': 3,
             'new_failures': 4},
            ]
        self.assertEqual(
            ''.join(MultiIndustrialTest.results_table(results)),
            dedent("""\
                old failure | new failure | attempt | title
                          1 |           2 |       5 | foo
                          3 |           4 |       5 | bar
            """))


class TestIndustrialTest(TestCase):

    def test_init(self):
        old_client = object()
        new_client = object()
        attempt_list = []
        industrial = IndustrialTest(old_client, new_client, attempt_list)
        self.assertIs(old_client, industrial.old_client)
        self.assertIs(new_client, industrial.new_client)
        self.assertIs(attempt_list, industrial.stage_attempts)

    def test_from_args(self):
        side_effect = lambda x, y=None: (x, y)
        with patch('jujupy.EnvJujuClient.by_version', side_effect=side_effect):
            with patch('jujupy.SimpleEnvironment.from_config',
                       side_effect=lambda x: SimpleEnvironment(x, {})):
                industrial = IndustrialTest.from_args(
                    'foo', 'new-juju-path', [])
        self.assertIsInstance(industrial, IndustrialTest)
        self.assertEqual(industrial.old_client,
                         (SimpleEnvironment('foo-old', {}), None))
        self.assertEqual(industrial.new_client,
                         (SimpleEnvironment('foo-new', {}), 'new-juju-path'))
        self.assertNotEqual(industrial.old_client[0].environment,
                            industrial.new_client[0].environment)

    def test_run_stages(self):
        old_client = FakeEnvJujuClient('old')
        new_client = FakeEnvJujuClient('new')
        industrial = IndustrialTest(old_client, new_client, [
            FakeAttempt(True, True), FakeAttempt(True, True)])
        with patch('subprocess.call') as cc_mock:
            result = industrial.run_stages()
            self.assertItemsEqual(result, [('foo-id', True, True),
                                           ('foo-id', True, True)])
        self.assertEqual(len(cc_mock.mock_calls), 0)

    def test_run_stages_old_fail(self):
        old_client = FakeEnvJujuClient('old')
        new_client = FakeEnvJujuClient('new')
        industrial = IndustrialTest(old_client, new_client, [
            FakeAttempt(False, True), FakeAttempt(True, True)])
        with patch('subprocess.call') as cc_mock:
            result = industrial.run_stages()
            self.assertItemsEqual(result, [('foo-id', False, True)])
        assert_juju_call(self, cc_mock, old_client,
                         ('juju', '--show-log', 'destroy-environment',
                          'old', '--force', '-y'), 0)
        assert_juju_call(self, cc_mock, new_client,
                         ('juju', '--show-log', 'destroy-environment',
                          'new', '--force', '-y'), 1)

    def test_run_stages_new_fail(self):
        old_client = FakeEnvJujuClient('old')
        new_client = FakeEnvJujuClient('new')
        industrial = IndustrialTest(old_client, new_client, [
            FakeAttempt(True, False), FakeAttempt(True, True)])
        with patch('subprocess.call') as cc_mock:
            result = industrial.run_stages()
            self.assertItemsEqual(result, [('foo-id', True, False)])
        assert_juju_call(self, cc_mock, old_client,
                         ('juju', '--show-log', 'destroy-environment',
                          'old', '--force', '-y'), 0)
        assert_juju_call(self, cc_mock, new_client,
                         ('juju', '--show-log', 'destroy-environment',
                          'new', '--force', '-y'), 1)

    def test_run_stages_both_fail(self):
        old_client = FakeEnvJujuClient('old')
        new_client = FakeEnvJujuClient('new')
        industrial = IndustrialTest(old_client, new_client, [
            FakeAttempt(False, False), FakeAttempt(True, True)])
        with patch('subprocess.call') as cc_mock:
            result = industrial.run_stages()
            self.assertItemsEqual(result, [('foo-id', False, False)])
        assert_juju_call(self, cc_mock, old_client,
                         ('juju', '--show-log', 'destroy-environment',
                          'old', '--force', '-y'), 0)
        assert_juju_call(self, cc_mock, new_client,
                         ('juju', '--show-log', 'destroy-environment',
                          'new', '--force', '-y'), 1)

    def test_destroy_both_even_with_exception(self):
        old_client = FakeEnvJujuClient('old')
        new_client = FakeEnvJujuClient('new')
        industrial = IndustrialTest(old_client, new_client, [
            FakeAttempt(False, False), FakeAttempt(True, True)])
        with patch.object(old_client, 'destroy_environment',
                          side_effect=Exception) as oc_mock:
            with patch.object(new_client, 'destroy_environment',
                              side_effect=Exception) as nc_mock:
                with self.assertRaises(Exception):
                    industrial.destroy_both()
        oc_mock.assert_called_once_with()
        nc_mock.assert_called_once_with()

    def test_run_attempt(self):
        old_client = FakeEnvJujuClient('old')
        new_client = FakeEnvJujuClient('new')
        attempt = FakeAttempt(True, True)
        industrial = IndustrialTest(old_client, new_client, [attempt])
        with patch.object(attempt, 'do_stage', side_effect=Exception('Foo')):
            with patch('logging.exception') as le_mock:
                with patch.object(industrial, 'destroy_both') as db_mock:
                    with self.assertRaises(SystemExit):
                        industrial.run_attempt()
        le_mock.assert_called_once()
        self.assertEqual(db_mock.mock_calls, [call(), call()])


class TestStageAttempt(TestCase):

    def test_do_stage(self):

        class StubSA(StageAttempt):

            def __init__(self):
                super(StageAttempt, self).__init__()
                self.did_op = []

            def do_operation(self, client):
                self.did_op.append(client)

            def get_result(self, client):
                return self.did_op.index(client)

        attempt = StubSA()
        old = object()
        new = object()
        result = attempt.do_stage(old, new)
        self.assertEqual([old, new], attempt.did_op)
        self.assertEqual(result, (0, 1))

    def test_get_test_info(self):

        class StubSA(StageAttempt):

            test_id = 'foo-bar'

            title = 'baz'

        self.assertEqual(StubSA.get_test_info(), {'foo-bar': {'title': 'baz'}})

    def test_iter_test_results(self):

        did_operation = [False]

        class StubSA(StageAttempt):

            test_id = 'foo-id'

            def do_stage(self, old, new):

                did_operation[0] = True
                return True, True

        for result in StubSA().iter_test_results(None, None):
            self.assertEqual(result, ('foo-id', True, True))


class FakeEnvJujuClient(EnvJujuClient):

    def __init__(self, name='steve'):
        super(FakeEnvJujuClient, self).__init__(
            SimpleEnvironment(name, {'type': 'fake'}), '1.2', '/jbin/juju')

    def wait_for_started(self):
        with patch('sys.stdout'):
            return super(FakeEnvJujuClient, self).wait_for_started(0.01)

    def wait_for_ha(self):
        with patch('sys.stdout'):
            return super(FakeEnvJujuClient, self).wait_for_ha(0.01)

    def juju(self, *args, **kwargs):
        # Suppress stdout for juju commands.
        with patch('sys.stdout'):
            return super(FakeEnvJujuClient, self).juju(*args, **kwargs)


class TestBootstrapAttempt(TestCase):

    def test_do_operation(self):
        client = FakeEnvJujuClient()
        bootstrap = BootstrapAttempt()
        with patch('subprocess.check_call') as mock_cc:
            bootstrap.do_operation(client)
        assert_juju_call(self, mock_cc, client, (
            'juju', '--show-log', 'bootstrap', '-e', 'steve',
            '--constraints', 'mem=2G'))

    def test_do_operation_exception(self):
        client = FakeEnvJujuClient()
        bootstrap = BootstrapAttempt()
        with patch('subprocess.check_call', side_effect=Exception
                   ) as mock_cc:
            with patch('logging.exception') as le_mock:
                bootstrap.do_operation(client)
        le_mock.assert_called_once()
        assert_juju_call(self, mock_cc, client, (
            'juju', '--show-log', 'bootstrap', '-e', 'steve',
            '--constraints', 'mem=2G'))
        output = yaml.safe_dump({
            'machines': {'0': {'agent-state': 'started'}},
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output):
            with patch('logging.exception') as le_mock:
                self.assertFalse(bootstrap.get_result(client))
        le_mock.assert_called_once()

    def test_get_result_true(self):
        bootstrap = BootstrapAttempt()
        client = FakeEnvJujuClient()
        output = yaml.safe_dump({
            'machines': {'0': {'agent-state': 'started'}},
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output):
            self.assertTrue(bootstrap.get_result(client))

    def test_get_result_false(self):
        bootstrap = BootstrapAttempt()
        client = FakeEnvJujuClient()
        output = yaml.safe_dump({
            'machines': {'0': {'agent-state': 'pending'}},
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output):
            with patch('logging.exception') as le_mock:
                self.assertFalse(bootstrap.get_result(client))
        le_mock.assert_called_once()


class TestDestroyEnvironmentAttempt(TestCase):

    def test_do_operation(self):
        client = FakeEnvJujuClient()
        destroy_env = DestroyEnvironmentAttempt()
        with patch('subprocess.check_call') as mock_cc:
            destroy_env.do_operation(client)
        assert_juju_call(self, mock_cc, client, (
            'juju', '--show-log', 'destroy-environment', '-y', 'steve'))


class TestEnsureAvailabilityAttempt(TestCase):

    def test__operation(self):
        client = FakeEnvJujuClient()
        ensure_av = EnsureAvailabilityAttempt()
        with patch('subprocess.check_call') as mock_cc:
            ensure_av._operation(client)
        assert_juju_call(self, mock_cc, client, (
            'juju', '--show-log', 'ensure-availability', '-e', 'steve', '-n',
            '3'))

    def test__result_true(self):
        ensure_av = EnsureAvailabilityAttempt()
        client = FakeEnvJujuClient()
        output = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
                '2': {'state-server-member-status': 'has-vote'},
                },
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output):
            self.assertTrue(ensure_av.get_result(client))

    def test__result_false(self):
        ensure_av = EnsureAvailabilityAttempt()
        client = FakeEnvJujuClient()
        output = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
                },
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output):
            with self.assertRaisesRegexp(
                    Exception, 'Timed out waiting for voting to be enabled.'):
                ensure_av._result(client)


class TestDeployManyAttempt(TestCase):

    def test__operation(self):
        client = FakeEnvJujuClient()
        deploy_many = DeployManyAttempt(9, 11)
        outputs = (yaml.safe_dump(x) for x in [
            # Before adding 10 machines
            {
                'machines': {'0': {'agent-state': 'started'}},
                'services': {},
            },
            # After adding 10 machines
            {
                'machines': dict((str(x), {'agent-state': 'started'})
                                 for x in range(deploy_many.host_count + 1)),
                'services': {},
            },
            ])

        with patch('subprocess.check_output', side_effect=lambda *x, **y:
                   outputs.next()):
            with patch('subprocess.check_call') as mock_cc:
                deploy_many.do_operation(client)
        for index in range(deploy_many.host_count):
            assert_juju_call(self, mock_cc, client, (
                'juju', '--show-log', 'add-machine', '-e', 'steve'), index)
        for host in range(1, deploy_many.host_count + 1):
            for container in range(deploy_many.container_count):
                index = ((host-1) * deploy_many.container_count +
                         deploy_many.host_count + container)
                target = 'lxc:{}'.format(host)
                service = 'ubuntu{}x{}'.format(host, container)
                assert_juju_call(self, mock_cc, client, (
                    'juju', '--show-log', 'deploy', '-e', 'steve', '--to',
                    target, 'ubuntu', service), index)

    def test__result_false(self):
        deploy_many = DeployManyAttempt()
        client = FakeEnvJujuClient()
        output = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'pending'},
                },
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output):
            with self.assertRaisesRegexp(
                    Exception,
                    'Timed out waiting for agents to start in steve.'):
                deploy_many._result(client)


class TestBackupRestoreAttempt(TestCase):

    def test__operation(self):
        br_attempt = BackupRestoreAttempt()
        client = FakeEnvJujuClient()
        client.env = get_aws_env()
        environ = dict(os.environ)
        environ.update(get_euca_env(client.env.config))

        def check_output(*args, **kwargs):
            if args == (['juju', 'backup'],):
                return 'juju-backup-24.tgz'
            if args == (('juju', '--show-log', 'status', '-e', 'baz'),):
                return yaml.safe_dump({
                    'machines': {'0': {
                        'instance-id': 'asdf',
                        'dns-name': '128.100.100.128',
                        }}
                    })
            self.assertEqual([], args)
        with patch('subprocess.check_output',
                   side_effect=check_output) as co_mock:
            with patch('subprocess.check_call') as cc_mock:
                with patch('sys.stdout'):
                    br_attempt._operation(client)
        assert_juju_call(self, co_mock, client, ['juju', 'backup'], 0)
        self.assertEqual(
            cc_mock.mock_calls[0],
            call(['euca-terminate-instances', 'asdf'], env=environ))
        assert_juju_call(
            self, cc_mock, client, (
                'juju', '--show-log', 'restore', '-e', 'baz',
                os.path.abspath('juju-backup-24.tgz')), 1)

    def test__result(self):
        br_attempt = BackupRestoreAttempt()
        client = FakeEnvJujuClient()
        output = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
                },
            'services': {},
            })
        with patch('subprocess.check_output', return_value=output) as co_mock:
            br_attempt._result(client)
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'status', '-e', 'steve'), assign_stderr=True)


class TestMaybeWriteJson(TestCase):

    def test_maybe_write_json(self):
        with NamedTemporaryFile() as temp_file:
            maybe_write_json(temp_file.name, {})
            self.assertEqual('{}', temp_file.read())

    def test_maybe_write_json_none(self):
        maybe_write_json(None, {})

    def test_maybe_write_json_pretty(self):
        with NamedTemporaryFile() as temp_file:
            maybe_write_json(temp_file.name, {'a': ['b'], 'b': 'c'})
            expected = dedent("""\
                {
                  "a": [
                    "b"
                  ],\x20
                  "b": "c"
                }""")
            self.assertEqual(temp_file.read(), expected)
