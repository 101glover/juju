from mock import patch
import os
import shutil
import subprocess
import tarfile
from unittest import TestCase

from gotesttarfile import (
    go_test_package,
    main,
    parse_args,
    run,
    untar_gopath,
)
from utility import temp_dir


class gotesttarfileTestCase(TestCase):

    def test_run_success(self):
        env = {'a': 'b'}
        with patch('subprocess.check_output', return_value='pass') as mock:
            returncode, output = run('go', 'test', './...', env=env)
        self.assertEqual(0, returncode)
        self.assertEqual('pass', output)
        args, kwargs = mock.call_args
        self.assertEqual((('go', 'test', './...'), ), args)
        self.assertIs(env, kwargs['env'])
        self.assertIs(subprocess.STDOUT, kwargs['stderr'])

    def test_run_fail(self):
        env = {'a': 'b'}
        e = subprocess.CalledProcessError(1, ['foo'], output='fail')
        with patch('subprocess.check_output', side_effect=e):
            returncode, output = run('go', 'test', './...', env=env)
        self.assertEqual(1, returncode)
        self.assertEqual('fail', output)

    def test_untar_gopath(self):
        with temp_dir() as base_dir:
            old_dir = os.path.join(base_dir, 'juju_1.2.3')
            for sub in ['bin', 'pkg', 'src']:
                os.makedirs(os.path.join(old_dir, sub))
            tarfile_path = os.path.join(base_dir, 'juju_1.2.3.tar.gz')
            with tarfile.open(name=tarfile_path, mode='w:gz') as tar:
                tar.add(old_dir, arcname='juju_1.2.3')
            shutil.rmtree(old_dir)
            gopath = os.path.join(base_dir, 'gogo')
            untar_gopath(tarfile_path, gopath, delete=True)
            self.assertTrue(os.path.isdir(gopath))
            self.assertItemsEqual(['bin', 'src', 'pkg'], os.listdir(gopath))
            self.assertFalse(os.path.isfile(tarfile_path))

    def test_go_test_package(self):
        with temp_dir() as gopath:
            package_path = os.path.join(
                gopath, 'src', 'github.com', 'juju', 'juju')
            os.makedirs(package_path)
            with patch('gotesttarfile.run', return_value=[0, 'success'],
                       autospec=True) as run_mock:
                devnull = open(os.devnull, 'w')
                with patch('sys.stdout', devnull):
                    returncode = go_test_package(
                        'github.com/juju/juju', 'go', gopath)
        self.assertEqual(0, returncode)
        args, kwargs = run_mock.call_args
        self.assertEqual(('go', 'test', './...'), args)
        self.assertEqual('amd64', kwargs['env'].get('GOARCH'))
        self.assertEqual(gopath, kwargs['env'].get('GOPATH'))

    def test_go_test_package_win32(self):
        with temp_dir() as gopath:
            package_path = os.path.join(
                gopath, 'src', 'github.com', 'juju', 'juju')
            os.makedirs(package_path)
            with patch('gotesttarfile.run', return_value=[0, 'success'],
                       autospec=True) as run_mock:
                devnull = open(os.devnull, 'w')
                with patch('sys.stdout', devnull):
                    with patch('sys.platform', 'win32'):
                        tainted_path = r'C:\foo;C:\bar\OpenSSH;C:\baz'
                        with patch.dict(os.environ, {'PATH': tainted_path}):
                            returncode = go_test_package(
                                'github.com/juju/juju', 'go', gopath)
        self.assertEqual(0, returncode)
        args, kwargs = run_mock.call_args
        self.assertEqual(
            ('powershell.exe', '-Command', 'go', 'test', './...'),
            args)
        self.assertEqual(r'C:\foo;C:\baz', kwargs['env'].get('PATH'))
        self.assertEqual(kwargs['env'].get('PATH'), kwargs['env'].get('Path'))

    def test_parse_args(self):
        args = parse_args(
            ['-v', '-g', 'go', '-p' 'github/foo', '-r', 'juju.tar.gz'])
        self.assertTrue(args.verbose)
        self.assertEqual('go', args.go)
        self.assertEqual('github/foo', args.package)
        self.assertTrue(args.remove_tarfile)
        self.assertEqual('juju.tar.gz', args.tarfile)

    def test_main(self):
        with patch('gotesttarfile.untar_gopath', autospec=True) as ug_mock:
            with patch('gotesttarfile.go_test_package',
                       autospec=True, return_value=0) as gt_mock:
                returncode = main(['/juju.tar.gz'])
        self.assertEqual(0, returncode)
        args, kwargs = ug_mock.call_args
        self.assertEqual('/juju.tar.gz', args[0])
        gopath = args[1]
        self.assertEqual('gogo', gopath.split(os.sep)[-1])
        self.assertFalse(kwargs['delete'])
        self.assertFalse(kwargs['verbose'])
        args, kwargs = gt_mock.call_args
        self.assertEqual(('github.com/juju/juju', 'go', gopath), args)
        self.assertFalse(kwargs['verbose'])
