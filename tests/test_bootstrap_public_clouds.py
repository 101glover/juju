#!/usr/bin/env python
"""Tests for bootstrap_public_clouds."""

from argparse import Namespace
from contextlib import contextmanager
import os

from mock import (
    call,
    Mock,
    patch,
    )

from bootstrap_public_clouds import (
    bootstrap_cloud,
    bootstrap_cloud_regions,
    default_log_dir,
    iter_cloud_regions,
    main,
    make_logging_dir,
    parse_args,
    yaml_file_load,
    )
from fakejuju import (
    fake_juju_client,
    )
from tests import (
    FakeHomeTestCase,
    TestCase,
    )
from utility import (
    temp_dir,
    )


_LOCAL = 'bootstrap_public_clouds'


@contextmanager
def patch_local(target, **kwargs):
    with patch(_LOCAL + '.' + target, **kwargs) as patched_object:
        yield patched_object


class TestParseArgs(TestCase):

    def test_parse_args(self):
        args = parse_args([])
        self.assertEqual(Namespace(
            deadline=None, debug=False, juju_bin='/usr/bin/juju', logs=None,
            start=0,
            ), args)

    def test_parse_args_start(self):
        args = parse_args(['--start', '7'])
        self.assertEqual(7, args.start)


class TestMain(TestCase):

    @contextmanager
    def patch_for_test(self, test_iterator, args=[], clouds={}, creds={}):
        fake_yaml = {'clouds': clouds, 'credentials': creds}
        with patch_local('configure_logging', autospec=True):
            with patch_local('parse_args', return_value=parse_args(args)):
                with patch_local('yaml_file_load', return_value=fake_yaml):
                    with patch_local('bootstrap_cloud_regions', autospec=True,
                                     return_value=test_iterator):
                        with patch_local('print') as print_mock:
                            yield print_mock

    def test_main(self):
        with self.patch_for_test([]) as print_mock:
            retval = main()
        self.assertEqual(0, retval)
        print_mock.assert_called_once_with('No failures!')

    def test_main_failures(self):
        errors = [('config', 'region', 'one'), ('config', 'region', 'two')]
        with self.patch_for_test(errors) as print_mock:
            retval = main()
        self.assertEqual(1, retval)
        print_mock.assert_has_calls([
            call('Failed:'),
            call(' * config region one'),
            call(' * config region two'),
            ])
        self.assertEqual(3, print_mock.call_count)


class TestHelpers(TestCase):

    def test_make_logging_dir(self):
        with patch('os.makedirs', autospec=True) as mkdirs_mock:
            log_dir = make_logging_dir('base', 'config', 'region')
        self.assertEqual('base/config/region', log_dir)
        mkdirs_mock.assert_called_once_with('base/config/region')

    def test_yaml_file_load(self):
        with temp_dir() as root_dir:
            src_file = os.path.join(root_dir, 'test.yaml')
            with open(src_file, 'w') as yaml_file:
                yaml_file.write('data:\n  alpha: A\n  beta: B\n')
            with patch('bootstrap_public_clouds.get_juju_home', autospec=True,
                       return_value=root_dir) as get_home_mock:
                data = yaml_file_load('test.yaml')
        get_home_mock.assert_called_once_with()
        self.assertEqual(data, {'data': {'alpha': 'A', 'beta': 'B'}})

    def test_default_log_dir(self):
        settings = Namespace(logs=None)
        with patch(
                'deploy_stack.BootstrapManager._generate_default_clean_dir',
                return_value='/tmp12345') as clean_dir_mock:
            default_log_dir(settings)
        self.assertEqual('/tmp12345', settings.logs)
        self.assertEqual(1, clean_dir_mock.call_count)

    def test_default_log_dir_provided(self):
        settings = Namespace(logs='/tmpABCDE')
        with patch(
                'deploy_stack.BootstrapManager._generate_default_clean_dir',
                autospec=True) as clean_dir_mock:
            default_log_dir(settings)
        self.assertEqual('/tmpABCDE', settings.logs)
        self.assertFalse(clean_dir_mock.called)


@contextmanager
def fake_booted_context(upload_tools, **kwargs):
    yield


class TestBootstrapCloud(TestCase):

    def test_bootstrap_cloud(self):
        client = fake_juju_client()
        bs_manager = Mock()
        bs_manager.attach_mock(Mock(side_effect=fake_booted_context),
                               'booted_context')
        with patch_local('make_logging_dir', autospec=True):
            with patch_local('BootstrapManager', autospec=True,
                             return_value=bs_manager) as bsm_mock:
                with patch_local('BootstrapManager.booted_context'):
                    with patch('jujupy.EnvJujuClient.wait_for_started'):
                        bootstrap_cloud('config', 'region', client, 'log_dir')
        self.assertEqual(1, bsm_mock.call_count)
        args, kwargs = bsm_mock.call_args
        self.assertEqual('boot-cpc-foo-region', args[0])
        bs_manager.booted_context.assert_called_once_with(False)


class TestIterCloudRegions(TestCase):

    def test_iter_cloud_regions(self):
        credentials = {'aws', 'google', 'rackspace'}
        public_clouds = {
            'aws': {'regions': ['north', 'south']},
            'google': {'regions': ['west']},
            }
        regions = list(iter_cloud_regions(public_clouds, credentials))
        self.assertEqual([('default-aws', 'north'), ('default-aws', 'south'),
                          ('default-gce', 'west')], regions)

    def test_iter_cloud_regions_credential_skip(self):
        credentials = {}
        public_clouds = {'rackspace': {'regions': 'none'}}
        with patch('logging.warning') as warning_mock:
            regions = list(iter_cloud_regions(public_clouds, credentials))
        self.assertEqual(1, warning_mock.call_count)
        self.assertEqual([], regions)


class TestBootstrapCloudRegions(FakeHomeTestCase):

    @contextmanager
    def patch_for_test(self, cloud_regions, client, error=None):
        """Handles all the patching for testing bootstrap_cloud_regions."""
        with patch('bootstrap_public_clouds.iter_cloud_regions',
                   autospec=True, return_value=cloud_regions) as iter_mock:
            with patch('bootstrap_public_clouds.bootstrap_cloud',
                       autospec=True, side_effect=error) as bootstrap_mock:
                with patch('bootstrap_public_clouds.client_from_config',
                           autospec=True, return_value=client):
                    with patch('logging.info', autospec=True) as info_mock:
                        yield (iter_mock, bootstrap_mock, info_mock)

    def run_test_bootstrap_cloud_regions(self, start=0, error=None):
        pc_key = 'public_clouds'
        cred_key = 'credentials'
        cloud_regions = [('foo.config', 'foo'), ('bar.config', 'bar')]
        args = Namespace(start=start, debug=True, deadline=None,
                         juju_bin='juju', logs='/tmp/log')
        fake_client = fake_juju_client()
        expect_calls = [call('foo.config', 'foo', fake_client, '/tmp/log'),
                        call('bar.config', 'bar', fake_client, '/tmp/log')]
        with self.patch_for_test(cloud_regions, fake_client, error) as (
                iter_mock, bootstrap_mock, info_mock):
            errors = list(bootstrap_cloud_regions(pc_key, cred_key, args))

        iter_mock.assert_called_once_with(pc_key, cred_key)
        bootstrap_mock.assert_has_calls(expect_calls[start:])
        self.assertEqual(len(cloud_regions) - start, info_mock.call_count)
        if error is None:
            self.assertEqual([], errors)
        else:
            expect_errors = [cr + (error,) for cr in cloud_regions]
            self.assertEqual(expect_errors, errors)

    def test_bootstrap_cloud_regions(self):
        self.run_test_bootstrap_cloud_regions(start=0, error=None)

    def test_bootstrap_cloud_regions_start(self):
        self.run_test_bootstrap_cloud_regions(start=1, error=None)

    def test_bootstrap_cloud_regions_error(self):
        self.run_test_bootstrap_cloud_regions(error=Exception('test'))
