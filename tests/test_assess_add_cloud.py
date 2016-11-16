from contextlib import contextmanager
from copy import deepcopy
from textwrap import dedent
from StringIO import StringIO

from mock import (
    call,
    patch,
    )

from fakejuju import fake_juju_client
from jujupy import (
    AuthNotAccepted,
    JujuData,
    )
from assess_add_cloud import (
    assess_all_clouds,
    assess_cloud,
    CloudSpec,
    iter_clouds,
    write_status,
    )
from tests import FakeHomeTestCase
from utility import JujuAssertionError


class TestCase(FakeHomeTestCase):

    def make_fake_juju_client(self):
        env = JujuData('foo', juju_home=self.juju_home)
        return fake_juju_client(env=env)


class TestAssessCloud(TestCase):

    @contextmanager
    def cloud_client(self, clouds):
        client = self.make_fake_juju_client()
        client.env.load_yaml()

        def dump(cloud_name, cloud):
            client.env.write_clouds(client.env.juju_home,
                                    clouds)

        with patch.object(client, 'add_cloud_interactive', dump):
            yield client

    def test_assess_cloud(self):
        expected_cloud = {'clouds': {'foo': {
                'type': 'maas',
                'endpoint': 'http://bar.example.com',
                }}}
        with self.cloud_client(expected_cloud) as client:
            assess_cloud(client, 'foo', expected_cloud['clouds']['foo'])

    def test_assess_cloud_missing(self):
        with self.cloud_client({'clouds': {}}) as client:
            with self.assertRaisesRegexp(JujuAssertionError,
                                         'Clouds missing!'):
                assess_cloud(client, 'foo', {
                    'type': 'maas',
                    'endpoint': 'http://bar.example.com',
                    })

    def test_assess_cloud_mismatch(self):
        with self.cloud_client({'clouds': {'foo': {}}}) as client:
            with self.assertRaisesRegexp(JujuAssertionError,
                                         'Cloud mismatch'):
                stderr = StringIO()
                with patch('sys.stderr', stderr):
                    assess_cloud(client, 'foo', {
                        'type': 'maas',
                        'endpoint': 'http://bar.example.com',
                        })
        self.assertEqual(dedent("""
            Expected:
            {endpoint: 'http://bar.example.com', type: maas}

            Actual:
            {}
            """), stderr.getvalue())


def make_long_endpoint(spec, regions=False):
    config = deepcopy(spec.config)
    config['endpoint'] = 'A' * 4096
    if regions:
        for region in config['regions'].values():
            region['endpoint'] = 'A' * 4096
    return CloudSpec('long-endpoint-{}'.format(spec.name), spec.name, config,
                     exception=None)


class TestIterClouds(TestCase):

    def test_manual(self):
        cloud = {'type': 'manual', 'endpoint': 'http://example.com'}
        spec = CloudSpec('foo', 'foo', cloud, exception=None)
        self.assertItemsEqual([
            spec,
            CloudSpec('long-name-foo', 'A' * 4096, cloud, exception=None),
            make_long_endpoint(spec),
            ], iter_clouds({'foo': cloud}))

    def test_vsphere(self):
        cloud = {
            'type': 'vsphere',
            'endpoint': '1.2.3.4',
            'regions': {'q': {'endpoint': '1.2.3.4'}},
            }
        spec = CloudSpec('foo', 'foo', cloud, exception=None)
        self.assertItemsEqual([
            spec,
            CloudSpec('long-name-foo', 'A' * 4096, cloud, exception=None),
            make_long_endpoint(spec, regions=True),
            ], iter_clouds({'foo': cloud}))

    def test_maas(self):
        cloud = {
            'type': 'maas',
            'endpoint': 'http://example.com',
            }
        spec = CloudSpec('foo', 'foo', cloud, exception=None)
        self.assertItemsEqual([
            spec,
            CloudSpec('long-name-foo', 'A' * 4096, cloud, exception=None),
            make_long_endpoint(spec),
            ], iter_clouds({'foo': cloud}))

    def test_openstack(self):
        config = {'type': 'openstack', 'endpoint': 'http://example.com',
                  'regions': {'bar': {'endpoint': 'http://baz.example.com'}}}
        spec = CloudSpec('foo', 'foo', config, exception=None)
        long_name = CloudSpec('long-name-foo', 'A' * 4096, config,
                              exception=None)
        long_region = CloudSpec('long-endpoint-foo-bar', 'foo',
                                deepcopy(config), exception=None)
        long_region.config['regions']['bar']['endpoint'] = 'A' * 4096
        bogus_auth = CloudSpec('bogus-auth-foo', 'foo',
                               deepcopy(config), exception=AuthNotAccepted)
        bogus_auth.config['auth-types'] = ['asdf']
        self.assertItemsEqual([
            spec, long_name, long_region, bogus_auth, make_long_endpoint(spec),
            ], iter_clouds({'foo': config}))


class TestAssessAllClouds(TestCase):

    def test_assess_all_clouds(self):
        client = self.make_fake_juju_client()
        clouds = {'a': {'type': 'foo'}, 'b': {'type': 'bar'}}
        exception = Exception()
        with patch('assess_add_cloud.assess_cloud',
                   side_effect=[None] + [exception] * 5):
            with patch('sys.stdout'):
                with patch('logging.exception') as exception_mock:
                    succeeded, failed = assess_all_clouds(client, clouds)
        self.assertEqual({'a'}, succeeded)
        self.assertEqual({
            'b', 'bogus-auth-a', 'bogus-auth-b', 'long-name-a', 'long-name-b'},
            failed)
        self.assertEqual(exception_mock.mock_calls, [call(exception)] * 5)


class TestWriteStatus(TestCase):

    def do_write(self, status, items):
        stdout = StringIO()
        with patch('sys.stdout', stdout):
            write_status(status, items)
        return stdout.getvalue()

    def test_write_none(self):
        self.assertEqual('pending: none\n', self.do_write('pending', set()))

    def test_write_one(self):
        self.assertEqual('pending: q\n', self.do_write('pending', {'q'}))

    def test_write_two(self):
        self.assertEqual('pending: q, r\n',
                         self.do_write('pending', {'r', 'q'}))
