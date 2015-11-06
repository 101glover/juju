from argparse import Namespace
from ConfigParser import NoOptionError
import logging
import os
from StringIO import StringIO
from tempfile import NamedTemporaryFile
from textwrap import dedent
from unittest import TestCase

from boto.s3.bucket import Bucket
from boto.s3.key import Key as S3Key
from mock import (
    create_autospec,
    patch,
    )

from jujuci import PackageNamer
from s3ci import (
    fetch_juju_binary,
    find_package_key,
    get_job_path,
    get_s3_credentials,
    main,
    PackageNotFound,
    parse_args,
    )
from tests import (
    parse_error,
    stdout_guard,
    TestCase as StrictTestCase,
    use_context,
    )
from utility import temp_dir


class TestParseArgs(TestCase):

    def test_get_juju_bin_defaults(self):
        args = parse_args(['get-juju-bin', 'myconfig', '3275'])
        self.assertEqual(Namespace(
            command='get-juju-bin', config='myconfig', revision_build=3275,
            workspace='.', verbose=0),
            args)

    def test_get_juju_bin_workspace(self):
        args = parse_args(['get-juju-bin', 'myconfig', '3275', 'myworkspace'])
        self.assertEqual('myworkspace', args.workspace)

    def test_get_juju_bin_too_few(self):
        with parse_error(self) as stderr:
            parse_args(['get-juju-bin', 'myconfig'])
        self.assertRegexpMatches(stderr.getvalue(), 'too few arguments$')

    def test_get_juju_bin_verbosity(self):
        args = parse_args(['get-juju-bin', 'myconfig', '3275', '-v'])
        self.assertEqual(1, args.verbose)
        args = parse_args(['get-juju-bin', 'myconfig', '3275', '-vv'])
        self.assertEqual(2, args.verbose)


class TestGetS3Credentials(TestCase):

    def test_get_s3_credentials(self):
        with NamedTemporaryFile() as temp_file:
            temp_file.write(dedent("""\
                [default]
                access_key = fake_username
                secret_key = fake_pass
                """))
            temp_file.flush()
            access_key, secret_key = get_s3_credentials(temp_file.name)
        self.assertEqual(access_key, "fake_username")
        self.assertEqual(secret_key, "fake_pass")

    def test_no_access_key(self):
        with NamedTemporaryFile() as temp_file:
            temp_file.write(dedent("""\
                [default]
                secret_key = fake_pass
                """))
            temp_file.flush()
            with self.assertRaisesRegexp(
                    NoOptionError,
                    "No option 'access_key' in section: 'default'"):
                get_s3_credentials(temp_file.name)

    def test_get_s3_access_no_secret_key(self):
        with NamedTemporaryFile() as temp_file:
            temp_file.write(dedent("""\
                [default]
                access_key = fake_username
                """))
            temp_file.flush()
            with self.assertRaisesRegexp(
                    NoOptionError,
                    "No option 'secret_key' in section: 'default'"):
                get_s3_credentials(temp_file.name)


def mock_package_key(revision_build, build=27, distro_release=None):
    key = create_autospec(S3Key, instance=True)
    namer = PackageNamer.factory()
    if distro_release is not None:
        namer.distro_release = distro_release
    package = namer.get_release_package('109.6')
    key.name = '{}/build-{}/{}'.format(
        get_job_path(revision_build), build, package)
    return key


def mock_bucket(keys):
    bucket = create_autospec(Bucket, instance=True)
    bucket.list.return_value = keys
    return bucket


def get_key_filename(key):
    return key.name.split('/')[-1]


class TestFindPackageKey(StrictTestCase):

    def setUp(self):
        use_context(self, patch('utility.get_deb_arch', return_value='amd65',
                                autospec=True))

    def test_find_package_key(self):
        key = mock_package_key(390)
        bucket = mock_bucket([key])
        found_key, filename = find_package_key(bucket, 390)
        bucket.list.assert_called_once_with(get_job_path(390))
        self.assertIs(key, found_key)
        self.assertEqual(filename, get_key_filename(key))

    def test_selects_latest(self):
        new_key = mock_package_key(390, build=27)
        old_key = mock_package_key(390, build=9)
        bucket = mock_bucket([old_key, new_key, old_key])
        found_key = find_package_key(bucket, 390)[0]
        self.assertIs(new_key, found_key)

    def test_wrong_version(self):
        key = mock_package_key(390, distro_release='01.01')
        bucket = mock_bucket([key])
        with self.assertRaises(PackageNotFound):
            find_package_key(bucket, 390)

    def test_wrong_file(self):
        key = mock_package_key(390)
        key.name = key.name.replace('juju-core', 'juju-dore')
        bucket = mock_bucket([key])
        with self.assertRaises(PackageNotFound):
            find_package_key(bucket, 390)


class TestFetchJujuBinary(StrictTestCase):

    def setUp(self):
        use_context(self, patch('utility.get_deb_arch', return_value='amd65',
                                autospec=True))

    def test_fetch_juju_binary(self):
        key = mock_package_key(275)
        filename = get_key_filename(key)
        bucket = mock_bucket([key])

        def extract(package, out_dir):
            os.mkdir(out_dir)
            open(os.path.join(out_dir, 'juju'), 'w')

        with temp_dir() as workspace:
            with patch('jujuci.extract_deb', autospec=True,
                       side_effect=extract) as ed_mock:
                extracted = fetch_juju_binary(bucket, 275, workspace)
        local_deb = os.path.join(workspace, filename)
        key.get_contents_to_filename.assert_called_once_with(local_deb)
        eb_dir = os.path.join(workspace, 'extracted-bin')
        ed_mock.assert_called_once_with(local_deb, eb_dir)
        self.assertEqual(os.path.join(eb_dir, 'juju'), extracted)


class TestMain(StrictTestCase):

    def setUp(self):
        use_context(self, stdout_guard())

    def test_main_args(self):
        stdout = StringIO()
        with NamedTemporaryFile() as temp_file:
            temp_file.write(dedent("""\
                [default]
                access_key = fake_username
                secret_key = fake_pass
                """))
            temp_file.flush()
            with patch('sys.argv', [
                    'foo', 'get-juju-bin', temp_file.name, '28',
                    'bar-workspace']):
                with patch('s3ci.S3Connection', autospec=True) as s3c_mock:
                    with patch('s3ci.fetch_juju_binary', autospec=True,
                               return_value='gjb') as gbj_mock:
                        with patch('sys.stdout', stdout):
                            main()
        s3c_mock.assert_called_once_with('fake_username', 'fake_pass')
        gb_mock = s3c_mock.return_value.get_bucket
        gb_mock.assert_called_once_with('juju-qa-data')
        gbj_mock.assert_called_once_with(gb_mock.return_value, 28,
                                         'bar-workspace')
        self.assertEqual('gjb\n', stdout.getvalue())
