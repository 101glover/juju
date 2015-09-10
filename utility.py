from contextlib import contextmanager
from datetime import (
    datetime,
    timedelta,
    )
import errno
import json
import logging
import os
import re
from shutil import rmtree
import subprocess
import socket
from StringIO import StringIO
import sys
from time import (
    sleep,
    time,
    )
from tempfile import mkdtemp
from unittest import FunctionTestCase
import xml.etree.ElementTree as ET

# Export shell quoting function which has moved in newer python versions
try:
    from shlex import quote
except ImportError:
    from pipes import quote

quote


@contextmanager
def scoped_environ(new_environ=None):
    old_environ = dict(os.environ)
    try:
        if new_environ is not None:
            os.environ.clear()
            os.environ.update(new_environ)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_environ)


class PortTimeoutError(Exception):
    pass


class until_timeout:

    """Yields remaining number of seconds.  Stops when timeout is reached.

    :ivar timeout: Number of seconds to wait.
    """
    def __init__(self, timeout, start=None):
        self.timeout = timeout
        if start is None:
            start = self.now()
        self.start = start

    def __iter__(self):
        return self

    @staticmethod
    def now():
        return datetime.now()

    def next(self):
        elapsed = self.now() - self.start
        remaining = self.timeout - elapsed.total_seconds()
        if remaining <= 0:
            raise StopIteration
        return remaining


def pause(seconds):
    print_now('Sleeping for %d seconds.' % seconds)
    sleep(seconds)


def wait_for_port(host, port, closed=False, timeout=30):
    for remaining in until_timeout(timeout):
        try:
            addrinfo = socket.getaddrinfo(host, port, socket.AF_INET,
                                          socket.SOCK_STREAM)
        except socket.error as e:
            if e.errno != -5:  # "No address associated with hostname"
                raise
            if closed:
                return
            else:
                continue
        sockaddr = addrinfo[0][4]
        # Treat Azure messed-up address lookup as a closed port.
        if sockaddr[0] == '0.0.0.0':
            if closed:
                return
            else:
                continue
        conn = socket.socket(*addrinfo[0][:3])
        conn.settimeout(max(remaining, 5))
        try:
            conn.connect(sockaddr)
        except socket.timeout:
            if closed:
                return
        except socket.error as e:
            if e.errno not in (errno.ECONNREFUSED, errno.ETIMEDOUT):
                raise
            if closed:
                return
        except socket.gaierror as e:
            print_now(str(e))
        except Exception as e:
            print_now('Unexpected %r: %s' % (type(e), e))
            raise
        else:
            conn.close()
            if not closed:
                return
            sleep(1)
    raise PortTimeoutError('Timed out waiting for port.')


def print_now(string):
    print(string)
    sys.stdout.flush()


@contextmanager
def temp_dir(parent=None):
    directory = mkdtemp(dir=parent)
    try:
        yield directory
    finally:
        rmtree(directory)


def setup_test_logging(testcase, level=None):
    log = logging.getLogger()
    testcase.addCleanup(setattr, log, 'handlers', log.handlers)
    log.handlers = []
    testcase.log_stream = StringIO()
    handler = logging.StreamHandler(testcase.log_stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(handler)
    if level is not None:
        testcase.addCleanup(setattr, log, 'level', log.level)
        log.level = level


def get_revision_build(build_info):
    for action in build_info['actions']:
        if 'parameters' in action:
            for parameter in action['parameters']:
                if parameter['name'] == 'revision_build':
                    return parameter['value']


def builds_for_revision(job, revision_build, jenkins):
    """Return the build_info data for the given job and revision_build.

    Only successful builds are included.

    :param job: The name of the job.
    :param revision_build: The revision_build to searh for. Note that
        this parameter is a string.
    :parameter  jenkins: A Jenkins instance.
    """
    job_info = jenkins.get_job_info(job)
    result = []
    for build in job_info['builds']:
        build_info = jenkins.get_build_info(job, build['number'])
        if (get_revision_build(build_info) == revision_build and
                build_info['result'] == 'SUCCESS'):
            result.append(build_info)
    return result


def get_auth_token(root, job):
    tree = ET.parse(os.path.join(root, 'jobs', job, 'config.xml'))
    return tree.getroot().find('authToken').text


def check_free_disk_space(path, required, purpose):
    df_result = subprocess.check_output(["df", "-k", path])
    df_result = df_result.split('\n')[1]
    df_result = re.split(' +', df_result)
    available = int(df_result[3])
    if available < required:
        message = (
            "Warning: Probably not enough disk space available for\n"
            "%(purpose)s in directory %(path)s,\n"
            "mount point %(mount)s\n"
            "required: %(required)skB, available: %(available)skB."
            )
        print(message % {
            'path': path, 'mount': df_result[5], 'required': required,
            'available': available, 'purpose': purpose
            })


def get_winrm_certs():
    """"Returns locations of key and cert files for winrm in cloud-city."""
    home = os.environ['HOME']
    return (
        os.path.join(home, 'cloud-city/winrm_client_cert.key'),
        os.path.join(home, 'cloud-city/winrm_client_cert.pem'),
    )


def s3_cmd(params, drop_output=False):
    s3cfg_path = os.path.join(
        os.environ['HOME'], 'cloud-city/juju-qa.s3cfg')
    command = ['s3cmd', '-c', s3cfg_path, '--no-progress'] + params
    if drop_output:
        return subprocess.check_call(
            command, stdout=open('/dev/null', 'w'))
    else:
        return subprocess.check_output(command)


def add_basic_testing_arguments(parser, using_jes=False):
    """Returns the parser loaded with basic testing arguments.

    The basic testing arguments, used in conjuction with boot_context ensures
    a test can be run in any supported substrate in parallel.

    This helper adds 4 positional arguments that define the minimum needed
    to run a test script: env, juju_bin, logs, and temp_env_name.

    There are many optional args that either update the env's config or
    manipulate the juju command line options to test in controlled situations
    or in uncommon substrates: --debug, --verbose, --agent-url, --agent-stream,
    --series, --bootstrap-host, --machine, --keep-env. If not using_jes, the
    --upload-tools arg will also be added.

    :param parser: an ArgumentParser.
    :param using_jes: whether args should be tailored for JES testing.
    """
    # Required positional arguments.
    # (name, help)
    positional_args = [
        ('env', 'The juju environment to base the temp test environment on.'),
        ('juju_bin', 'Full path to the Juju binary.'),
        ('logs', 'A directory in which to store logs.'),
        ('temp_env_name', 'A temporary test environment name.'),
    ]
    for p_arg in positional_args:
        name, help_txt = p_arg
        parser.add_argument(name, help=help_txt)
    parser.add_argument('--debug', action='store_true',
                        help='Pass --debug to Juju.')
    parser.add_argument('--verbose', action='store_const',
                        default=logging.INFO, const=logging.DEBUG,
                        help='Verbose test harness output.')
    parser.add_argument('--agent-url', action='store', default=None,
                        help='URL for retrieving agent binaries.')
    parser.add_argument('--agent-stream', action='store', default=None,
                        help='URL for retrieving agent binaries.')
    parser.add_argument('--series', action='store', default=None,
                        help='Name of the Ubuntu series to use.')
    if not using_jes:
        parser.add_argument('--upload-tools', action='store_true',
                            help='upload local version of tools to bootstrap.')
    parser.add_argument('--bootstrap-host',
                        help='The host to use for bootstrap.')
    parser.add_argument('--machine', help='A machine to add or when used with '
                        'KVM based MaaS, a KVM image to start.',
                        action='append', default=[])
    parser.add_argument('--keep-env', action='store_true',
                        help='Keep the Juju environment after the test'
                        ' completes.')
    return parser


def configure_logging(log_level):
    logging.basicConfig(
        level=log_level, format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')


def ensure_deleted(path):
    try:
        os.unlink(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def get_candidates_path(root_dir):
    return os.path.join(root_dir, 'candidate')


def find_candidates(root_dir, find_all=False):
    return (path for path, buildvars in _find_candidates(root_dir, find_all))


def find_latest_branch_candidates(root_dir):
    """Return a list of one candidate per branch.

    :param root_dir: The root directory to find candidates from.
    """
    candidates = []
    for path, buildvars_path in _find_candidates(root_dir, find_all=False):
        with open(buildvars_path) as buildvars_file:
            buildvars = json.load(buildvars_file)
            candidates.append(
                (buildvars['branch'], int(buildvars['revision_build']), path))
    latest = dict((branch, path) for branch, build, path in sorted(candidates))
    return latest.values()


def _find_candidates(root_dir, find_all=False):
    candidates_path = get_candidates_path(root_dir)
    a_week_ago = time() - timedelta(days=7).total_seconds()
    for candidate_dir in os.listdir(candidates_path):
        if candidate_dir.endswith('-artifacts'):
            continue
        candidate_path = os.path.join(candidates_path, candidate_dir)
        buildvars = os.path.join(candidate_path, 'buildvars.json')
        try:
            stat = os.stat(buildvars)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                continue
            raise
        if not find_all and stat.st_mtime < a_week_ago:
            continue
        yield candidate_path, buildvars


def get_deb_arch():
    """Get the debian machine architecture."""
    return subprocess.check_output(['dpkg', '--print-architecture']).strip()


def extract_deb(package_path, directory):
    """Extract a debian package to a specified directory."""
    subprocess.check_call(['dpkg', '-x', package_path, directory])


def run_command(command, dry_run=False, verbose=False):
    """Optionally execute a command and maybe print the output."""
    if verbose:
        print_now('Executing: {}'.format(command))
    if not dry_run:
        output = subprocess.check_output(command)
        if verbose:
            print_now(output)
    return output


def to_unit_test(test_function):

    def unit_testify(*args, **kwargs):
        tc = FunctionTestCase(test_function)
        largs = list(args)
        largs.insert(1, tc)
        args = tuple(largs)
        return test_function(*args, **kwargs)

    return unit_testify
