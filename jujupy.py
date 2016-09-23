from __future__ import print_function

from collections import (
    defaultdict,
    namedtuple,
)
from contextlib import (
    contextmanager,
    nested,
)
from copy import deepcopy
from cStringIO import StringIO
from datetime import datetime
import errno
from itertools import chain
import json
import logging
import os
import pexpect
import re
from shutil import rmtree
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time

import yaml

from jujuconfig import (
    get_environments_path,
    get_jenv_path,
    get_juju_home,
    get_selected_environment,
)
from utility import (
    check_free_disk_space,
    ensure_deleted,
    ensure_dir,
    is_ipv6_address,
    JujuResourceTimeout,
    pause,
    quote,
    qualified_model_name,
    scoped_environ,
    split_address_port,
    temp_dir,
    unqualified_model_name,
    until_timeout,
)


__metaclass__ = type

AGENTS_READY = set(['started', 'idle'])
WIN_JUJU_CMD = os.path.join('\\', 'Progra~2', 'Juju', 'juju.exe')

JUJU_DEV_FEATURE_FLAGS = 'JUJU_DEV_FEATURE_FLAGS'
CONTROLLER = 'controller'
KILL_CONTROLLER = 'kill-controller'
SYSTEM = 'system'

KVM_MACHINE = 'kvm'
LXC_MACHINE = 'lxc'
LXD_MACHINE = 'lxd'

_DEFAULT_BUNDLE_TIMEOUT = 3600

_jes_cmds = {KILL_CONTROLLER: {
    'create': 'create-environment',
    'kill': KILL_CONTROLLER,
}}
for super_cmd in [SYSTEM, CONTROLLER]:
    _jes_cmds[super_cmd] = {
        'create': '{} create-environment'.format(super_cmd),
        'kill': '{} kill'.format(super_cmd),
    }

log = logging.getLogger("jujupy")


class IncompatibleConfigClass(Exception):
    """Raised when a client is initialised with the wrong config class."""


class SoftDeadlineExceeded(Exception):
    """Raised when an overall client operation takes too long."""


def get_timeout_path():
    import timeout
    return os.path.abspath(timeout.__file__)


def get_timeout_prefix(duration, timeout_path=None):
    """Return extra arguments to run a command with a timeout."""
    if timeout_path is None:
        timeout_path = get_timeout_path()
    return (sys.executable, timeout_path, '%.2f' % duration, '--')


def get_teardown_timeout(client):
    """Return the timeout need byt the client to teardown resources."""
    if client.env.config['type'] == 'azure':
        return 1800
    else:
        return 600


def parse_new_state_server_from_error(error):
    err_str = str(error)
    output = getattr(error, 'output', None)
    if output is not None:
        err_str += output
    matches = re.findall(r'Attempting to connect to (.*):22', err_str)
    if matches:
        return matches[-1]
    return None


class ErroredUnit(Exception):

    def __init__(self, unit_name, state):
        msg = '%s is in state %s' % (unit_name, state)
        Exception.__init__(self, msg)
        self.unit_name = unit_name
        self.state = state


class BootstrapMismatch(Exception):

    def __init__(self, arg_name, arg_val, env_name, env_val):
        super(BootstrapMismatch, self).__init__(
            '--{} {} does not match {}: {}'.format(
                arg_name, arg_val, env_name, env_val))


class UpgradeMongoNotSupported(Exception):

    def __init__(self):
        super(UpgradeMongoNotSupported, self).__init__(
            'This client does not support upgrade-mongo')


class JESNotSupported(Exception):

    def __init__(self):
        super(JESNotSupported, self).__init__(
            'This client does not support JES')


class JESByDefault(Exception):

    def __init__(self):
        super(JESByDefault, self).__init__(
            'This client does not need to enable JES')


Machine = namedtuple('Machine', ['machine_id', 'info'])


def yaml_loads(yaml_str):
    return yaml.safe_load(StringIO(yaml_str))


def coalesce_agent_status(agent_item):
    """Return the machine agent-state or the unit agent-status."""
    state = agent_item.get('agent-state')
    if state is None and agent_item.get('agent-status') is not None:
        state = agent_item.get('agent-status').get('current')
    if state is None and agent_item.get('juju-status') is not None:
        state = agent_item.get('juju-status').get('current')
    if state is None:
        state = 'no-agent'
    return state


def make_client(juju_path, debug, env_name, temp_env_name):
    """Create a new juju client.

    :param juju_path: Full path to the juju binary the client should wrap.
    :param debug: Debug flag for the client.
    :param env_name: Name of the environment to use the configuration from.
    :param temp_env_name: Name of client's model/environment, if None env_name
    is used."""
    client = client_from_config(env_name, juju_path, debug)
    if temp_env_name is not None:
        client.env.set_model_name(temp_env_name)
    return client


class CannotConnectEnv(subprocess.CalledProcessError):

    def __init__(self, e):
        super(CannotConnectEnv, self).__init__(e.returncode, e.cmd, e.output)


class StatusNotMet(Exception):

    _fmt = 'Expected status not reached in {env}.'

    def __init__(self, environment_name, status):
        self.env = environment_name
        self.status = status

    def __str__(self):
        return self._fmt.format(env=self.env)


class AgentsNotStarted(StatusNotMet):

    _fmt = 'Timed out waiting for agents to start in {env}.'


class VersionsNotUpdated(StatusNotMet):

    _fmt = 'Some versions did not update.'


class WorkloadsNotReady(StatusNotMet):

    _fmt = 'Workloads not ready in {env}.'


@contextmanager
def temp_yaml_file(yaml_dict):
    temp_file = NamedTemporaryFile(suffix='.yaml', delete=False)
    try:
        with temp_file:
            yaml.safe_dump(yaml_dict, temp_file)
        yield temp_file.name
    finally:
        os.unlink(temp_file.name)


class SimpleEnvironment:
    """Represents a model in a JUJU_HOME directory for juju 1."""

    def __init__(self, environment, config=None, juju_home=None,
                 controller=None):
        """Constructor.

        :param environment: Name of the environment.
        :param config: Dictionary with configuration options, default is None.
        :param juju_home: Path to JUJU_HOME directory, default is None.
        :param controller: Controller instance-- this model's controller.
            If not given or None a new instance is created."""
        self.user_name = None
        if controller is None:
            controller = Controller(environment)
        self.controller = controller
        self.environment = environment
        self.config = config
        self.juju_home = juju_home
        if self.config is not None:
            self.local = bool(self.config.get('type') == 'local')
            self.kvm = (
                self.local and bool(self.config.get('container') == 'kvm'))
            self.maas = bool(self.config.get('type') == 'maas')
            self.joyent = bool(self.config.get('type') == 'joyent')
        else:
            self.local = False
            self.kvm = False
            self.maas = False
            self.joyent = False

    def clone(self, model_name=None):
        config = deepcopy(self.config)
        if model_name is None:
            model_name = self.environment
        else:
            config['name'] = unqualified_model_name(model_name)
        result = self.__class__(model_name, config, self.juju_home,
                                self.controller)
        result.local = self.local
        result.kvm = self.kvm
        result.maas = self.maas
        result.joyent = self.joyent
        return result

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        if self.environment != other.environment:
            return False
        if self.config != other.config:
            return False
        if self.local != other.local:
            return False
        if self.maas != other.maas:
            return False
        return True

    def __ne__(self, other):
        return not self == other

    def set_model_name(self, model_name, set_controller=True):
        if set_controller:
            self.controller.name = model_name
        self.environment = model_name
        self.config['name'] = unqualified_model_name(model_name)

    @classmethod
    def from_config(cls, name):
        """Create an environment from the configuation file.

        :param name: Name of the environment to get the configuration from."""
        return cls._from_config(name)

    @classmethod
    def _from_config(cls, name):
        config, selected = get_selected_environment(name)
        if name is None:
            name = selected
        return cls(name, config)

    def needs_sudo(self):
        return self.local

    @contextmanager
    def make_jes_home(self, juju_home, dir_name, new_config):
        home_path = jes_home_path(juju_home, dir_name)
        if os.path.exists(home_path):
            rmtree(home_path)
        os.makedirs(home_path)
        self.dump_yaml(home_path, new_config)
        yield home_path

    def get_cloud_credentials(self):
        """Return the credentials for this model's cloud.

        This implementation returns config variables in addition to
        credentials.
        """
        return self.config

    def dump_yaml(self, path, config):
        dump_environments_yaml(path, config)


class JujuData(SimpleEnvironment):
    """Represents a model in a JUJU_DATA directory for juju 2."""

    def __init__(self, environment, config=None, juju_home=None,
                 controller=None):
        """Constructor.

        This extends SimpleEnvironment's constructor.

        :param environment: Name of the environment.
        :param config: Dictionary with configuration options; default is None.
        :param juju_home: Path to JUJU_DATA directory. If None (the default),
            the home directory is autodetected.
        :param controller: Controller instance-- this model's controller.
            If not given or None, a new instance is created.
        """
        if juju_home is None:
            juju_home = get_juju_home()
        super(JujuData, self).__init__(environment, config, juju_home,
                                       controller)
        self.credentials = {}
        self.clouds = {}

    def clone(self, model_name=None):
        result = super(JujuData, self).clone(model_name)
        result.credentials = deepcopy(self.credentials)
        result.clouds = deepcopy(self.clouds)
        return result

    @classmethod
    def from_env(cls, env):
        juju_data = cls(env.environment, env.config, env.juju_home)
        juju_data.load_yaml()
        return juju_data

    def load_yaml(self):
        try:
            with open(os.path.join(self.juju_home, 'credentials.yaml')) as f:
                self.credentials = yaml.safe_load(f)
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise RuntimeError(
                    'Failed to read credentials file: {}'.format(str(e)))
            self.credentials = {}
        try:
            with open(os.path.join(self.juju_home, 'clouds.yaml')) as f:
                self.clouds = yaml.safe_load(f)
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise RuntimeError(
                    'Failed to read clouds file: {}'.format(str(e)))
            # Default to an empty clouds file.
            self.clouds = {'clouds': {}}

    @classmethod
    def from_config(cls, name):
        """Create a model from the three configuration files."""
        juju_data = cls._from_config(name)
        juju_data.load_yaml()
        return juju_data

    def dump_yaml(self, path, config):
        """Dump the configuration files to the specified path.

        config is unused, but is accepted for compatibility with
        SimpleEnvironment and make_jes_home().
        """
        with open(os.path.join(path, 'credentials.yaml'), 'w') as f:
            yaml.safe_dump(self.credentials, f)
        with open(os.path.join(path, 'clouds.yaml'), 'w') as f:
            yaml.safe_dump(self.clouds, f)

    def find_endpoint_cloud(self, cloud_type, endpoint):
        for cloud, cloud_config in self.clouds['clouds'].items():
            if cloud_config['type'] != cloud_type:
                continue
            if cloud_config['endpoint'] == endpoint:
                return cloud
        raise LookupError('No such endpoint: {}'.format(endpoint))

    def get_cloud(self):
        provider = self.config['type']
        # Separate cloud recommended by: Juju Cloud / Credentials / BootStrap /
        # Model CLI specification
        if provider == 'ec2' and self.config['region'] == 'cn-north-1':
            return 'aws-china'
        if provider not in ('maas', 'openstack'):
            return {
                'ec2': 'aws',
                'gce': 'google',
            }.get(provider, provider)
        if provider == 'maas':
            endpoint = self.config['maas-server']
        elif provider == 'openstack':
            endpoint = self.config['auth-url']
        return self.find_endpoint_cloud(provider, endpoint)

    def get_region(self):
        provider = self.config['type']
        if provider == 'azure':
            if 'tenant-id' not in self.config:
                return self.config['location'].replace(' ', '').lower()
            return self.config['location']
        elif provider == 'joyent':
            matcher = re.compile('https://(.*).api.joyentcloud.com')
            return matcher.match(self.config['sdc-url']).group(1)
        elif provider == 'lxd':
            return 'localhost'
        elif provider == 'manual':
            return self.config['bootstrap-host']
        elif provider in ('maas', 'manual'):
            return None
        else:
            return self.config['region']

    def get_cloud_credentials(self):
        """Return the credentials for this model's cloud."""
        cloud_name = self.get_cloud()
        cloud = self.credentials['credentials'][cloud_name]
        (credentials,) = cloud.values()
        return credentials


class Status:

    def __init__(self, status, status_text):
        self.status = status
        self.status_text = status_text

    @classmethod
    def from_text(cls, text):
        try:
            # Parsing as JSON is much faster than parsing as YAML, so try
            # parsing as JSON first and fall back to YAML.
            status_yaml = json.loads(text)
        except ValueError:
            status_yaml = yaml_loads(text)
        return cls(status_yaml, text)

    def get_applications(self):
        return self.status.get('applications', {})

    def iter_machines(self, containers=False, machines=True):
        for machine_name, machine in sorted(self.status['machines'].items()):
            if machines:
                yield machine_name, machine
            if containers:
                for contained, unit in machine.get('containers', {}).items():
                    yield contained, unit

    def iter_new_machines(self, old_status):
        for machine, data in self.iter_machines():
            if machine in old_status.status['machines']:
                continue
            yield machine, data

    def iter_units(self):
        for service_name, service in sorted(self.get_applications().items()):
            for unit_name, unit in sorted(service.get('units', {}).items()):
                yield unit_name, unit
                subordinates = unit.get('subordinates', ())
                for sub_name in sorted(subordinates):
                    yield sub_name, subordinates[sub_name]

    def agent_items(self):
        for machine_name, machine in self.iter_machines(containers=True):
            yield machine_name, machine
        for unit_name, unit in self.iter_units():
            yield unit_name, unit

    def agent_states(self):
        """Map agent states to the units and machines in those states."""
        states = defaultdict(list)
        for item_name, item in self.agent_items():
            states[coalesce_agent_status(item)].append(item_name)
        return states

    def check_agents_started(self, environment_name=None):
        """Check whether all agents are in the 'started' state.

        If not, return agent_states output.  If so, return None.
        If an error is encountered for an agent, raise ErroredUnit
        """
        bad_state_info = re.compile(
            '(.*error|^(cannot set up groups|cannot run instance)).*')
        for item_name, item in self.agent_items():
            state_info = item.get('agent-state-info', '')
            if bad_state_info.match(state_info):
                raise ErroredUnit(item_name, state_info)
        states = self.agent_states()
        if set(states.keys()).issubset(AGENTS_READY):
            return None
        for state, entries in states.items():
            if 'error' in state:
                # sometimes the state may be hidden in juju status message
                juju_status = dict(
                    self.agent_items())[entries[0]].get('juju-status')
                if juju_status:
                    juju_status_msg = juju_status.get('message')
                    if juju_status_msg:
                        state = juju_status_msg
                raise ErroredUnit(entries[0], state)
        return states

    def get_service_count(self):
        return len(self.get_applications())

    def get_service_unit_count(self, service):
        return len(
            self.get_applications().get(service, {}).get('units', {}))

    def get_agent_versions(self):
        versions = defaultdict(set)
        for item_name, item in self.agent_items():
            if item.get('juju-status', None):
                version = item['juju-status'].get('version', 'unknown')
                versions[version].add(item_name)
            else:
                versions[item.get('agent-version', 'unknown')].add(item_name)
        return versions

    def get_instance_id(self, machine_id):
        return self.status['machines'][machine_id]['instance-id']

    def get_unit(self, unit_name):
        """Return metadata about a unit."""
        for service in sorted(self.get_applications().values()):
            if unit_name in service.get('units', {}):
                return service['units'][unit_name]
        raise KeyError(unit_name)

    def service_subordinate_units(self, service_name):
        """Return subordinate metadata for a service_name."""
        services = self.get_applications()
        if service_name in services:
            for unit in sorted(services[service_name].get(
                    'units', {}).values()):
                for sub_name, sub in unit.get('subordinates', {}).items():
                    yield sub_name, sub

    def get_open_ports(self, unit_name):
        """List the open ports for the specified unit.

        If no ports are listed for the unit, the empty list is returned.
        """
        return self.get_unit(unit_name).get('open-ports', [])


class ServiceStatus(Status):

    def get_applications(self):
        return self.status.get('services', {})


class Juju2Backend:
    """A Juju backend referring to a specific juju 2 binary.

    Uses -m to specify models, uses JUJU_DATA to specify home directory.
    """

    def __init__(self, full_path, version, feature_flags, debug,
                 soft_deadline=None):
        self._version = version
        self._full_path = full_path
        self.feature_flags = feature_flags
        self.debug = debug
        self._timeout_path = get_timeout_path()
        self.juju_timings = {}
        self.soft_deadline = soft_deadline
        self._ignore_soft_deadline = False

    def _now(self):
        return datetime.utcnow()

    @contextmanager
    def _check_timeouts(self):
        # If an exception occurred, we don't want to replace it with
        # SoftDeadlineExceeded.
        yield
        if self.soft_deadline is None or self._ignore_soft_deadline:
            return
        if self._now() > self.soft_deadline:
            raise SoftDeadlineExceeded('Operation exceeded deadline.')

    @contextmanager
    def ignore_soft_deadline(self):
        """Ignore the client deadline.  For cleanup code."""
        old_val = self._ignore_soft_deadline
        self._ignore_soft_deadline = True
        try:
            yield
        finally:
            self._ignore_soft_deadline = old_val

    def clone(self, full_path, version, debug, feature_flags):
        if version is None:
            version = self.version
        if full_path is None:
            full_path = self.full_path
        if debug is None:
            debug = self.debug
        result = self.__class__(full_path, version, feature_flags, debug,
                                self.soft_deadline)
        return result

    @property
    def version(self):
        return self._version

    @property
    def full_path(self):
        return self._full_path

    @property
    def juju_name(self):
        return os.path.basename(self._full_path)

    def _get_attr_tuple(self):
        return (self._version, self._full_path, self.feature_flags,
                self.debug, self.juju_timings)

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        return self._get_attr_tuple() == other._get_attr_tuple()

    def shell_environ(self, used_feature_flags, juju_home):
        """Generate a suitable shell environment.

        Juju's directory must be in the PATH to support plugins.
        """
        env = dict(os.environ)
        if self.full_path is not None:
            env['PATH'] = '{}{}{}'.format(os.path.dirname(self.full_path),
                                          os.pathsep, env['PATH'])
        flags = self.feature_flags.intersection(used_feature_flags)
        if flags:
            env[JUJU_DEV_FEATURE_FLAGS] = ','.join(sorted(flags))
        env['JUJU_DATA'] = juju_home
        return env

    def full_args(self, command, args, model, timeout):
        if model is not None:
            e_arg = ('-m', model)
        else:
            e_arg = ()
        if timeout is None:
            prefix = ()
        else:
            prefix = get_timeout_prefix(timeout, self._timeout_path)
        logging = '--debug' if self.debug else '--show-log'

        # If args is a string, make it a tuple. This makes writing commands
        # with one argument a bit nicer.
        if isinstance(args, basestring):
            args = (args,)
        # we split the command here so that the caller can control where the -m
        # model flag goes.  Everything in the command string is put before the
        # -m flag.
        command = command.split()
        return (prefix + (self.juju_name, logging,) + tuple(command) + e_arg +
                args)

    def juju(self, command, args, used_feature_flags,
             juju_home, model=None, check=True, timeout=None, extra_env=None):
        """Run a command under juju for the current environment."""
        args = self.full_args(command, args, model, timeout)
        log.info(' '.join(args))
        env = self.shell_environ(used_feature_flags, juju_home)
        if extra_env is not None:
            env.update(extra_env)
        if check:
            call_func = subprocess.check_call
        else:
            call_func = subprocess.call
        start_time = time.time()
        # Mutate os.environ instead of supplying env parameter so Windows can
        # search env['PATH']
        with scoped_environ(env):
            with self._check_timeouts():
                rval = call_func(args)
        self.juju_timings.setdefault(args, []).append(
            (time.time() - start_time))
        return rval

    def expect(self, command, args, used_feature_flags, juju_home, model=None,
               timeout=None, extra_env=None):
        args = self.full_args(command, args, model, timeout)
        log.info(' '.join(args))
        env = self.shell_environ(used_feature_flags, juju_home)
        if extra_env is not None:
            env.update(extra_env)
        # pexpect.spawn expects a string. This is better than trying to extract
        # command + args from the returned tuple (as there could be an intial
        # timing command tacked on).
        command_string = ' '.join(quote(a) for a in args)
        with scoped_environ(env):
            return pexpect.spawn(command_string)

    @contextmanager
    def juju_async(self, command, args, used_feature_flags,
                   juju_home, model=None, timeout=None):
        full_args = self.full_args(command, args, model, timeout)
        log.info(' '.join(args))
        env = self.shell_environ(used_feature_flags, juju_home)
        # Mutate os.environ instead of supplying env parameter so Windows can
        # search env['PATH']
        with scoped_environ(env):
            with self._check_timeouts():
                proc = subprocess.Popen(full_args)
        yield proc
        retcode = proc.wait()
        if retcode != 0:
            raise subprocess.CalledProcessError(retcode, full_args)

    def get_juju_output(self, command, args, used_feature_flags, juju_home,
                        model=None, timeout=None, user_name=None,
                        merge_stderr=False):
        args = self.full_args(command, args, model, timeout)
        env = self.shell_environ(used_feature_flags, juju_home)
        log.debug(args)
        # Mutate os.environ instead of supplying env parameter so
        # Windows can search env['PATH']
        with scoped_environ(env):
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE)
            with self._check_timeouts():
                sub_output, sub_error = proc.communicate()
            log.debug(sub_output)
            if proc.returncode != 0:
                log.debug(sub_error)
                e = subprocess.CalledProcessError(
                    proc.returncode, args, sub_output)
                e.stderr = sub_error
                if sub_error and (
                    'Unable to connect to environment' in sub_error or
                        'MissingOrIncorrectVersionHeader' in sub_error or
                        '307: Temporary Redirect' in sub_error):
                    raise CannotConnectEnv(e)
                raise e
        return sub_output

    def pause(self, seconds):
        pause(seconds)


class Juju2A2Backend(Juju2Backend):
    """Backend for 2A2.

    Uses -m to specify models, uses JUJU_HOME and JUJU_DATA to specify home
    directory.
    """

    def shell_environ(self, used_feature_flags, juju_home):
        """Generate a suitable shell environment.

        For 2.0-alpha2 set both JUJU_HOME and JUJU_DATA.
        """
        env = super(Juju2A2Backend, self).shell_environ(used_feature_flags,
                                                        juju_home)
        env['JUJU_HOME'] = juju_home
        return env


class Juju1XBackend(Juju2A2Backend):
    """Backend for Juju 1.x - 2A1.

    Uses -e to specify models ("environments", uses JUJU_HOME to specify home
    directory.
    """

    def shell_environ(self, used_feature_flags, juju_home):
        """Generate a suitable shell environment.

        For 2.0-alpha1 and earlier set only JUJU_HOME and not JUJU_DATA.
        """
        env = super(Juju1XBackend, self).shell_environ(used_feature_flags,
                                                       juju_home)
        env['JUJU_HOME'] = juju_home
        del env['JUJU_DATA']
        return env

    def full_args(self, command, args, model, timeout):
        if model is None:
            e_arg = ()
        else:
            # In 1.x terminology, "model" is "environment".
            e_arg = ('-e', model)
        if timeout is None:
            prefix = ()
        else:
            prefix = get_timeout_prefix(timeout, self._timeout_path)
        logging = '--debug' if self.debug else '--show-log'

        # If args is a string, make it a tuple. This makes writing commands
        # with one argument a bit nicer.
        if isinstance(args, basestring):
            args = (args,)
        # we split the command here so that the caller can control where the -e
        # <env> flag goes.  Everything in the command string is put before the
        # -e flag.
        command = command.split()
        return (prefix + (self.juju_name, logging,) + tuple(command) + e_arg +
                args)


def get_client_class(version):
    if version.startswith('1.16'):
        raise Exception('Unsupported juju: %s' % version)
    elif re.match('^1\.22[.-]', version):
        client_class = EnvJujuClient22
    elif re.match('^1\.24[.-]', version):
        client_class = EnvJujuClient24
    elif re.match('^1\.25[.-]', version):
        client_class = EnvJujuClient25
    elif re.match('^1\.26[.-]', version):
        client_class = EnvJujuClient26
    elif re.match('^1\.', version):
        client_class = EnvJujuClient1X
    # Ensure alpha/beta number matches precisely
    elif re.match('^2\.0-alpha1([^\d]|$)', version):
        client_class = EnvJujuClient2A1
    elif re.match('^2\.0-alpha2([^\d]|$)', version):
        client_class = EnvJujuClient2A2
    elif re.match('^2\.0-(alpha3|beta[12])([^\d]|$)', version):
        client_class = EnvJujuClient2B2
    elif re.match('^2\.0-(beta[3-6])([^\d]|$)', version):
        client_class = EnvJujuClient2B3
    elif re.match('^2\.0-(beta7)([^\d]|$)', version):
        client_class = EnvJujuClient2B7
    elif re.match('^2\.0-beta8([^\d]|$)', version):
        client_class = EnvJujuClient2B8
    # between beta 9-14
    elif re.match('^2\.0-beta(9|1[0-4])([^\d]|$)', version):
        client_class = EnvJujuClient2B9
    else:
        client_class = EnvJujuClient
    return client_class


def client_from_config(config, juju_path, debug=False, soft_deadline=None):
    """Create a client from an environment's configuration.

    :param config: Name of the environment to use the config from.
    :param juju_path: Path to juju binary the client should wrap.
    :param debug=False: The debug flag for the client, False by default.
    :param soft_deadline: A datetime representing the deadline by which
        normal operations should complete.  If None, no deadline is
        enforced.
    """
    version = EnvJujuClient.get_version(juju_path)
    client_class = get_client_class(version)
    env = client_class.config_class.from_config(config)
    if juju_path is None:
        full_path = EnvJujuClient.get_full_path()
    else:
        full_path = os.path.abspath(juju_path)
    return client_class(env, version, full_path, debug=debug,
                        soft_deadline=soft_deadline)


class EnvJujuClient:
    """Wraps calls to a juju instance, associated with a single model.

    Note: A model is often called an enviroment (Juju 1 legacy).

    This class represents the latest Juju version.  Subclasses are used to
    support older versions (see get_client_class).
    """

    # The environments.yaml options that are replaced by bootstrap options.
    #
    # As described in bug #1538735, default-series and --bootstrap-series must
    # match.  'default-series' should be here, but is omitted so that
    # default-series is always forced to match --bootstrap-series.
    bootstrap_replaces = frozenset(['agent-version'])

    # What feature flags have existed that CI used.
    known_feature_flags = frozenset([
        'actions', 'jes', 'address-allocation', 'cloudsigma', 'migration'])

    # What feature flags are used by this version of the juju client.
    used_feature_flags = frozenset(['address-allocation', 'migration'])

    destroy_model_command = 'destroy-model'

    supported_container_types = frozenset([KVM_MACHINE, LXC_MACHINE,
                                           LXD_MACHINE])

    default_backend = Juju2Backend

    config_class = JujuData

    status_class = Status

    agent_metadata_url = 'agent-metadata-url'

    model_permissions = frozenset(['read', 'write', 'admin'])

    controller_permissions = frozenset(['login', 'addmodel', 'superuser'])

    @classmethod
    def preferred_container(cls):
        for container_type in [LXD_MACHINE, LXC_MACHINE]:
            if container_type in cls.supported_container_types:
                return container_type

    _show_status = 'show-status'

    @classmethod
    def get_version(cls, juju_path=None):
        """Get the version data from a juju binary.

        :param juju_path: Path to binary. If not given or None, 'juju' is used.
        """
        if juju_path is None:
            juju_path = 'juju'
        return subprocess.check_output((juju_path, '--version')).strip()

    def check_timeouts(self):
        return self._backend._check_timeouts()

    def ignore_soft_deadline(self):
        return self._backend.ignore_soft_deadline()

    def enable_feature(self, flag):
        """Enable juju feature by setting the given flag.

        New versions of juju with the feature enabled by default will silently
        allow this call, but will not export the environment variable.
        """
        if flag not in self.known_feature_flags:
            raise ValueError('Unknown feature flag: %r' % (flag,))
        self.feature_flags.add(flag)

    def get_jes_command(self):
        """For Juju 2.0, this is always kill-controller."""
        return KILL_CONTROLLER

    def is_jes_enabled(self):
        """Does the state-server support multiple environments."""
        try:
            self.get_jes_command()
            return True
        except JESNotSupported:
            return False

    def enable_jes(self):
        """Enable JES if JES is optional.

        Specifically implemented by the clients that optionally support JES.
        This version raises either JESByDefault or JESNotSupported.

        :raises: JESByDefault when JES is always enabled; Juju has the
            'destroy-controller' command.
        :raises: JESNotSupported when JES is not supported; Juju does not have
            the 'system kill' command when the JES feature flag is set.
        """
        if self.is_jes_enabled():
            raise JESByDefault()
        else:
            raise JESNotSupported()

    @classmethod
    def get_full_path(cls):
        if sys.platform == 'win32':
            return WIN_JUJU_CMD
        return subprocess.check_output(('which', 'juju')).rstrip('\n')

    def clone_path_cls(self, juju_path):
        """Clone using the supplied path to determine the class."""
        version = self.get_version(juju_path)
        cls = get_client_class(version)
        if juju_path is None:
            full_path = self.get_full_path()
        else:
            full_path = os.path.abspath(juju_path)
        return self.clone(version=version, full_path=full_path, cls=cls)

    def clone(self, env=None, version=None, full_path=None, debug=None,
              cls=None):
        """Create a clone of this EnvJujuClient.

        By default, the class, environment, version, full_path, and debug
        settings will match the original, but each can be overridden.
        """
        if env is None:
            env = self.env
        if cls is None:
            cls = self.__class__
        feature_flags = self.feature_flags.intersection(cls.used_feature_flags)
        backend = self._backend.clone(full_path, version, debug, feature_flags)
        other = cls.from_backend(backend, env)
        return other

    @classmethod
    def from_backend(cls, backend, env):
        return cls(env=env, version=backend.version,
                   full_path=backend.full_path,
                   debug=backend.debug, _backend=backend)

    def get_cache_path(self):
        return get_cache_path(self.env.juju_home, models=True)

    def _cmd_model(self, include_e, controller):
        if controller:
            return '{controller}:{model}'.format(
                controller=self.env.controller.name,
                model=self.get_controller_model_name())
        elif self.env is None or not include_e:
            return None
        else:
            return '{controller}:{model}'.format(
                controller=self.env.controller.name,
                model=self.model_name)

    def _full_args(self, command, sudo, args,
                   timeout=None, include_e=True, controller=False):
        model = self._cmd_model(include_e, controller)
        # sudo is not needed for devel releases.
        return self._backend.full_args(command, args, model, timeout)

    @staticmethod
    def _get_env(env):
        if not isinstance(env, JujuData) and isinstance(env,
                                                        SimpleEnvironment):
            # FIXME: JujuData should be used from the start.
            env = JujuData.from_env(env)
        return env

    def __init__(self, env, version, full_path, juju_home=None, debug=False,
                 soft_deadline=None, _backend=None):
        """Create a new juju client.

        Required Arguments
        :param env: Object representing a model in a data directory.
        :param version: Version of juju the client wraps.
        :param full_path: Full path to juju binary.

        Optional Arguments
        :param juju_home: default value for env.juju_home.  Will be
            autodetected if None (the default).
        :param debug: Flag to activate debugging output; False by default.
        :param soft_deadline: A datetime representing the deadline by which
            normal operations should complete.  If None, no deadline is
            enforced.
        :param _backend: The backend to use for interacting with the client.
            If None (the default), self.default_backend will be used.
        """
        self.env = self._get_env(env)
        if _backend is None:
            _backend = self.default_backend(full_path, version, set(), debug,
                                            soft_deadline)
        self._backend = _backend
        if version != _backend.version:
            raise ValueError('Version mismatch: {} {}'.format(
                version, _backend.version))
        if full_path != _backend.full_path:
            raise ValueError('Path mismatch: {} {}'.format(
                full_path, _backend.full_path))
        if debug is not _backend.debug:
            raise ValueError('debug mismatch: {} {}'.format(
                debug, _backend.debug))
        if env is not None:
            if juju_home is None:
                if env.juju_home is None:
                    env.juju_home = get_juju_home()
            else:
                env.juju_home = juju_home

    @property
    def version(self):
        return self._backend.version

    @property
    def full_path(self):
        return self._backend.full_path

    @property
    def feature_flags(self):
        return self._backend.feature_flags

    @feature_flags.setter
    def feature_flags(self, feature_flags):
        self._backend.feature_flags = feature_flags

    @property
    def debug(self):
        return self._backend.debug

    @property
    def model_name(self):
        return self.env.environment

    def _shell_environ(self):
        """Generate a suitable shell environment.

        Juju's directory must be in the PATH to support plugins.
        """
        return self._backend.shell_environ(self.used_feature_flags,
                                           self.env.juju_home)

    def add_ssh_machines(self, machines):
        for count, machine in enumerate(machines):
            try:
                self.juju('add-machine', ('ssh:' + machine,))
            except subprocess.CalledProcessError:
                if count != 0:
                    raise
                logging.warning('add-machine failed.  Will retry.')
                pause(30)
                self.juju('add-machine', ('ssh:' + machine,))

    @staticmethod
    def get_cloud_region(cloud, region):
        if region is None:
            return cloud
        return '{}/{}'.format(cloud, region)

    def get_bootstrap_args(self, upload_tools, config_filename,
                           bootstrap_series=None, credential=None):
        """Return the bootstrap arguments for the substrate."""
        if self.env.joyent:
            # Only accept kvm packages by requiring >1 cpu core, see lp:1446264
            constraints = 'mem=2G cpu-cores=1'
        else:
            constraints = 'mem=2G'
        cloud_region = self.get_cloud_region(self.env.get_cloud(),
                                             self.env.get_region())
        args = ['--constraints', constraints, self.env.environment,
                cloud_region, '--config', config_filename,
                '--default-model', self.env.environment]
        if upload_tools:
            args.insert(0, '--upload-tools')
        else:
            args.extend(['--agent-version', self.get_matching_agent_version()])

        if bootstrap_series is not None:
            args.extend(['--bootstrap-series', bootstrap_series])

        if credential is not None:
            args.extend(['--credential', credential])

        return tuple(args)

    def add_model(self, env):
        """Add a model to this model's controller and return its client.

        :param env: Class representing the new model/environment."""
        model_client = self.clone(env)
        with model_client._bootstrap_config() as config_file:
            self._add_model(env.environment, config_file)
        return model_client

    def make_model_config(self):
        config_dict = make_safe_config(self)
        agent_metadata_url = config_dict.pop('tools-metadata-url', None)
        if agent_metadata_url is not None:
            config_dict.setdefault('agent-metadata-url', agent_metadata_url)
        # Strip unneeded variables.
        return dict((k, v) for k, v in config_dict.items() if k not in {
            'access-key',
            'admin-secret',
            'application-id',
            'application-password',
            'auth-url',
            'bootstrap-host',
            'client-email',
            'client-id',
            'control-bucket',
            'location',
            'maas-oauth',
            'maas-server',
            'management-certificate',
            'management-subscription-id',
            'manta-key-id',
            'manta-user',
            'name',
            'password',
            'private-key',
            'region',
            'sdc-key-id',
            'sdc-url',
            'sdc-user',
            'secret-key',
            'storage-account-name',
            'subscription-id',
            'tenant-id',
            'tenant-name',
            'type',
            'username',
        })

    @contextmanager
    def _bootstrap_config(self):
        with temp_yaml_file(self.make_model_config()) as config_filename:
            yield config_filename

    def _check_bootstrap(self):
        if self.env.environment != self.env.controller.name:
            raise AssertionError(
                'Controller and environment names should not vary (yet)')

    def update_user_name(self):
        self.env.user_name = 'admin@local'

    def bootstrap(self, upload_tools=False, bootstrap_series=None,
                  credential=None):
        """Bootstrap a controller."""
        self._check_bootstrap()
        with self._bootstrap_config() as config_filename:
            args = self.get_bootstrap_args(
                upload_tools, config_filename, bootstrap_series, credential)
            self.update_user_name()
            self.juju('bootstrap', args, include_e=False)

    @contextmanager
    def bootstrap_async(self, upload_tools=False, bootstrap_series=None):
        self._check_bootstrap()
        with self._bootstrap_config() as config_filename:
            args = self.get_bootstrap_args(
                upload_tools, config_filename, bootstrap_series)
            self.update_user_name()
            with self.juju_async('bootstrap', args, include_e=False):
                yield
                log.info('Waiting for bootstrap of {}.'.format(
                    self.env.environment))

    def _add_model(self, model_name, config_file):
        self.controller_juju('add-model', (
            model_name, '--config', config_file))

    def destroy_model(self):
        exit_status = self.juju(
            'destroy-model', (self.env.environment, '-y',),
            include_e=False, timeout=get_teardown_timeout(self))
        return exit_status

    def kill_controller(self):
        """Kill a controller and its environments."""
        seen_cmd = self.get_jes_command()
        self.juju(
            _jes_cmds[seen_cmd]['kill'], (self.env.controller.name, '-y'),
            include_e=False, check=False, timeout=get_teardown_timeout(self))

    def get_juju_output(self, command, *args, **kwargs):
        """Call a juju command and return the output.

        Sub process will be called as 'juju <command> <args> <kwargs>'. Note
        that <command> may be a space delimited list of arguments. The -e
        <environment> flag will be placed after <command> and before args.
        """
        model = self._cmd_model(kwargs.get('include_e', True),
                                kwargs.get('controller', False))
        pass_kwargs = dict(
            (k, kwargs[k]) for k in kwargs if k in ['timeout', 'merge_stderr'])
        return self._backend.get_juju_output(
            command, args, self.used_feature_flags, self.env.juju_home,
            model, user_name=self.env.user_name, **pass_kwargs)

    def show_status(self):
        """Print the status to output."""
        self.juju(self._show_status, ('--format', 'yaml'))

    def get_status(self, timeout=60, raw=False, controller=False, *args):
        """Get the current status as a dict."""
        # GZ 2015-12-16: Pass remaining timeout into get_juju_output call.
        for ignored in until_timeout(timeout):
            try:
                if raw:
                    return self.get_juju_output(self._show_status, *args)
                return self.status_class.from_text(
                    self.get_juju_output(
                        self._show_status, '--format', 'yaml',
                        controller=controller))
            except subprocess.CalledProcessError:
                pass
        raise Exception(
            'Timed out waiting for juju status to succeed')

    @staticmethod
    def _dict_as_option_strings(options):
        return tuple('{}={}'.format(*item) for item in options.items())

    def set_config(self, service, options):
        option_strings = self._dict_as_option_strings(options)
        self.juju('config', (service,) + option_strings)

    def get_config(self, service):
        return yaml_loads(self.get_juju_output('config', service))

    def get_service_config(self, service, timeout=60):
        for ignored in until_timeout(timeout):
            try:
                return self.get_config(service)
            except subprocess.CalledProcessError:
                pass
        raise Exception(
            'Timed out waiting for juju get %s' % (service))

    def set_model_constraints(self, constraints):
        constraint_strings = self._dict_as_option_strings(constraints)
        return self.juju('set-model-constraints', constraint_strings)

    def get_model_config(self):
        """Return the value of the environment's configured options."""
        return yaml.safe_load(
            self.get_juju_output('model-config', '--format', 'yaml'))

    def get_env_option(self, option):
        """Return the value of the environment's configured option."""
        return self.get_juju_output('model-config', option)

    def set_env_option(self, option, value):
        """Set the value of the option in the environment."""
        option_value = "%s=%s" % (option, value)
        return self.juju('model-config', (option_value,))

    def unset_env_option(self, option):
        """Unset the value of the option in the environment."""
        return self.juju('model-config', ('--reset', option,))

    def get_agent_metadata_url(self):
        return self.get_env_option(self.agent_metadata_url)

    def set_testing_agent_metadata_url(self):
        url = self.get_agent_metadata_url()
        if 'testing' not in url:
            testing_url = url.replace('/tools', '/testing/tools')
            self.set_env_option(self.agent_metadata_url, testing_url)

    def juju(self, command, args, sudo=False, check=True, include_e=True,
             timeout=None, extra_env=None):
        """Run a command under juju for the current environment."""
        model = self._cmd_model(include_e, controller=False)
        return self._backend.juju(
            command, args, self.used_feature_flags, self.env.juju_home,
            model, check, timeout, extra_env)

    def expect(self, command, args=(), sudo=False, include_e=True,
               timeout=None, extra_env=None):
        """Return a process object that is running an interactive `command`.

        The interactive command ability is provided by using pexpect.

        :param command: String of the juju command to run.
        :param args: Tuple containing arguments for the juju `command`.
        :param sudo: Whether to call `command` using sudo.
        :param include_e: Boolean regarding supplying the juju environment to
          `command`.
        :param timeout: A float that, if provided, is the timeout in which the
          `command` is run.

        :return: A pexpect.spawn object that has been called with `command` and
          `args`.

        """
        model = self._cmd_model(include_e, controller=False)
        return self._backend.expect(
            command, args, self.used_feature_flags, self.env.juju_home,
            model, timeout, extra_env)

    def controller_juju(self, command, args):
        args = ('-c', self.env.controller.name) + args
        return self.juju(command, args, include_e=False)

    def get_juju_timings(self):
        stringified_timings = {}
        for command, timings in self._backend.juju_timings.items():
            stringified_timings[' '.join(command)] = timings
        return stringified_timings

    def juju_async(self, command, args, include_e=True, timeout=None):
        model = self._cmd_model(include_e, controller=False)
        return self._backend.juju_async(command, args, self.used_feature_flags,
                                        self.env.juju_home, model, timeout)

    def deploy(self, charm, repository=None, to=None, series=None,
               service=None, force=False, resource=None,
               storage=None, constraints=None):
        args = [charm]
        if service is not None:
            args.extend([service])
        if to is not None:
            args.extend(['--to', to])
        if series is not None:
            args.extend(['--series', series])
        if force is True:
            args.extend(['--force'])
        if resource is not None:
            args.extend(['--resource', resource])
        if storage is not None:
            args.extend(['--storage', storage])
        if constraints is not None:
            args.extend(['--constraints', constraints])
        return self.juju('deploy', tuple(args))

    def attach(self, service, resource):
        args = (service, resource)
        return self.juju('attach', args)

    def list_resources(self, service_or_unit, details=True):
        args = ('--format', 'yaml', service_or_unit)
        if details:
            args = args + ('--details',)
        return yaml_loads(self.get_juju_output('list-resources', *args))

    def wait_for_resource(self, resource_id, service_or_unit, timeout=60):
        log.info('Waiting for resource. Resource id:{}'.format(resource_id))
        with self.check_timeouts():
            with self.ignore_soft_deadline():
                for _ in until_timeout(timeout):
                    resources_dict = self.list_resources(service_or_unit)
                    resources = resources_dict['resources']
                    for resource in resources:
                        if resource['expected']['resourceid'] == resource_id:
                            if (resource['expected']['fingerprint'] ==
                                    resource['unit']['fingerprint']):
                                return
                    time.sleep(.1)
                raise JujuResourceTimeout(
                    'Timeout waiting for a resource to be downloaded. '
                    'ResourceId: {} Service or Unit: {} Timeout: {}'.format(
                        resource_id, service_or_unit, timeout))

    def upgrade_charm(self, service, charm_path=None):
        args = (service,)
        if charm_path is not None:
            args = args + ('--path', charm_path)
        self.juju('upgrade-charm', args)

    def remove_service(self, service):
        self.juju('remove-application', (service,))

    @classmethod
    def format_bundle(cls, bundle_template):
        return bundle_template.format(container=cls.preferred_container())

    def deploy_bundle(self, bundle_template, timeout=_DEFAULT_BUNDLE_TIMEOUT):
        """Deploy bundle using native juju 2.0 deploy command."""
        bundle = self.format_bundle(bundle_template)
        self.juju('deploy', bundle, timeout=timeout)

    def deployer(self, bundle_template, name=None, deploy_delay=10,
                 timeout=3600):
        """Deploy a bundle using deployer."""
        bundle = self.format_bundle(bundle_template)
        args = (
            '--debug',
            '--deploy-delay', str(deploy_delay),
            '--timeout', str(timeout),
            '--config', bundle,
        )
        if name:
            args += (name,)
        e_arg = ('-e', '{}:{}'.format(
            self.env.controller.name, self.env.environment))
        args = e_arg + args
        self.juju('deployer', args, self.env.needs_sudo(), include_e=False)

    def _get_substrate_constraints(self):
        if self.env.joyent:
            # Only accept kvm packages by requiring >1 cpu core, see lp:1446264
            return 'mem=2G cpu-cores=1'
        else:
            return 'mem=2G'

    def quickstart(self, bundle_template, upload_tools=False):
        """quickstart, using sudo if necessary."""
        bundle = self.format_bundle(bundle_template)
        constraints = 'mem=2G'
        args = ('--constraints', constraints)
        if upload_tools:
            args = ('--upload-tools',) + args
        args = args + ('--no-browser', bundle,)
        self.juju('quickstart', args, self.env.needs_sudo(),
                  extra_env={'JUJU': self.full_path})

    def status_until(self, timeout, start=None):
        """Call and yield status until the timeout is reached.

        Status will always be yielded once before checking the timeout.

        This is intended for implementing things like wait_for_started.

        :param timeout: The number of seconds to wait before timing out.
        :param start: If supplied, the time to count from when determining
            timeout.
        """
        with self.check_timeouts():
            with self.ignore_soft_deadline():
                yield self.get_status()
                for remaining in until_timeout(timeout, start=start):
                    yield self.get_status()

    def _wait_for_status(self, reporter, translate, exc_type=StatusNotMet,
                         timeout=1200, start=None):
        """Wait till status reaches an expected state with pretty reporting.

        Always tries to get status at least once. Each status call has an
        internal timeout of 60 seconds. This is independent of the timeout for
        the whole wait, note this means this function may be overrun.

        :param reporter: A GroupReporter instance for output.
        :param translate: A callable that takes status to make states dict.
        :param exc_type: Optional StatusNotMet subclass to raise on timeout.
        :param timeout: Optional number of seconds to wait before timing out.
        :param start: Optional time to count from when determining timeout.
        """
        status = None
        try:
            with self.check_timeouts():
                with self.ignore_soft_deadline():
                    for _ in chain([None],
                                   until_timeout(timeout, start=start)):
                        try:
                            status = self.get_status()
                        except CannotConnectEnv:
                            log.info(
                                'Suppressing "Unable to connect to'
                                ' environment"')
                            continue
                        states = translate(status)
                        if states is None:
                            break
                        reporter.update(states)
                    else:
                        if status is not None:
                            log.error(status.status_text)
                        raise exc_type(self.env.environment, status)
        finally:
            reporter.finish()
        return status

    def wait_for_started(self, timeout=1200, start=None):
        """Wait until all unit/machine agents are 'started'."""
        reporter = GroupReporter(sys.stdout, 'started')
        return self._wait_for_status(
            reporter, Status.check_agents_started, AgentsNotStarted,
            timeout=timeout, start=start)

    def wait_for_subordinate_units(self, service, unit_prefix, timeout=1200,
                                   start=None):
        """Wait until all service units have a started subordinate with
        unit_prefix."""
        def status_to_subordinate_states(status):
            service_unit_count = status.get_service_unit_count(service)
            subordinate_unit_count = 0
            unit_states = defaultdict(list)
            for name, unit in status.service_subordinate_units(service):
                if name.startswith(unit_prefix + '/'):
                    subordinate_unit_count += 1
                    unit_states[coalesce_agent_status(unit)].append(name)
            if (subordinate_unit_count == service_unit_count and
                    set(unit_states.keys()).issubset(AGENTS_READY)):
                return None
            return unit_states
        reporter = GroupReporter(sys.stdout, 'started')
        self._wait_for_status(
            reporter, status_to_subordinate_states, AgentsNotStarted,
            timeout=timeout, start=start)

    def wait_for_version(self, version, timeout=300, start=None):
        def status_to_version(status):
            versions = status.get_agent_versions()
            if versions.keys() == [version]:
                return None
            return versions
        reporter = GroupReporter(sys.stdout, version)
        self._wait_for_status(reporter, status_to_version, VersionsNotUpdated,
                              timeout=timeout, start=start)

    def list_models(self):
        """List the models registered with the current controller."""
        self.controller_juju('list-models', ())

    def get_models(self):
        """return a models dict with a 'models': [] key-value pair.

        The server has 120 seconds to respond because this method is called
        often when tearing down a controller-less deployment.
        """
        output = self.get_juju_output(
            'list-models', '-c', self.env.controller.name, '--format', 'yaml',
            include_e=False, timeout=120)
        models = yaml_loads(output)
        return models

    def _get_models(self):
        """return a list of model dicts."""
        return self.get_models()['models']

    def iter_model_clients(self):
        """Iterate through all the models that share this model's controller.

        Works only if JES is enabled.
        """
        models = self._get_models()
        if not models:
            yield self
        for model in models:
            yield self._acquire_model_client(model['name'])

    def get_controller_model_name(self):
        """Return the name of the 'controller' model.

        Return the name of the environment when an 'controller' model does
        not exist.
        """
        return 'controller'

    def _acquire_model_client(self, name):
        """Get a client for a model with the supplied name.

        If the name matches self, self is used.  Otherwise, a clone is used.
        """
        if name == self.env.environment:
            return self
        else:
            env = self.env.clone(model_name=name)
            return self.clone(env=env)

    def get_model_uuid(self):
        name = self.env.environment
        model = self._cmd_model(True, False)
        output_yaml = self.get_juju_output(
            'show-model', '--format', 'yaml', model, include_e=False)
        output = yaml.safe_load(output_yaml)
        return output[name]['model-uuid']

    def get_controller_uuid(self):
        name = self.env.controller.name
        output_yaml = self.get_juju_output(
            'show-controller', '--format', 'yaml', include_e=False)
        output = yaml.safe_load(output_yaml)
        return output[name]['details']['uuid']

    def get_controller_model_uuid(self):
        output_yaml = self.get_juju_output(
            'show-model', 'controller', '--format', 'yaml', include_e=False)
        output = yaml.safe_load(output_yaml)
        return output['controller']['model-uuid']

    def get_controller_client(self):
        """Return a client for the controller model.  May return self.

        This may be inaccurate for models created using add_model
        rather than bootstrap.
        """
        return self._acquire_model_client(self.get_controller_model_name())

    def list_controllers(self):
        """List the controllers."""
        self.juju('list-controllers', (), include_e=False)

    def get_controller_endpoint(self):
        """Return the address of the controller leader."""
        controller = self.env.controller.name
        output = self.get_juju_output(
            'show-controller', controller, include_e=False)
        info = yaml_loads(output)
        endpoint = info[controller]['details']['api-endpoints'][0]
        address, port = split_address_port(endpoint)
        return address

    def get_controller_members(self):
        """Return a list of Machines that are members of the controller.

        The first machine in the list is the leader. the remaining machines
        are followers in a HA relationship.
        """
        members = []
        status = self.get_status()
        for machine_id, machine in status.iter_machines():
            if self.get_controller_member_status(machine):
                members.append(Machine(machine_id, machine))
        if len(members) <= 1:
            return members
        # Search for the leader and make it the first in the list.
        # If the endpoint address is not the same as the leader's dns_name,
        # the members are return in the order they were discovered.
        endpoint = self.get_controller_endpoint()
        log.debug('Controller endpoint is at {}'.format(endpoint))
        members.sort(key=lambda m: m.info.get('dns-name') != endpoint)
        return members

    def get_controller_leader(self):
        """Return the controller leader Machine."""
        controller_members = self.get_controller_members()
        return controller_members[0]

    @staticmethod
    def get_controller_member_status(info_dict):
        """Return the controller-member-status of the machine if it exists."""
        return info_dict.get('controller-member-status')

    def wait_for_ha(self, timeout=1200):
        desired_state = 'has-vote'
        reporter = GroupReporter(sys.stdout, desired_state)
        try:
            with self.check_timeouts():
                with self.ignore_soft_deadline():
                    for remaining in until_timeout(timeout):
                        status = self.get_status(controller=True)
                        status.check_agents_started()
                        states = {}
                        for machine, info in status.iter_machines():
                            status = self.get_controller_member_status(info)
                            if status is None:
                                continue
                            states.setdefault(status, []).append(machine)
                        if states.keys() == [desired_state]:
                            if len(states.get(desired_state, [])) >= 3:
                                break
                        reporter.update(states)
                    else:
                        raise Exception('Timed out waiting for voting to be'
                                        ' enabled.')
        finally:
            reporter.finish()
        # XXX sinzui 2014-12-04: bug 1399277 happens because
        # juju claims HA is ready when the monogo replica sets
        # are not. Juju is not fully usable. The replica set
        # lag might be 5 minutes.
        self._backend.pause(300)

    def wait_for_deploy_started(self, service_count=1, timeout=1200):
        """Wait until service_count services are 'started'.

        :param service_count: The number of services for which to wait.
        :param timeout: The number of seconds to wait.
        """
        with self.check_timeouts():
            with self.ignore_soft_deadline():
                for remaining in until_timeout(timeout):
                    status = self.get_status()
                    if status.get_service_count() >= service_count:
                        return
                else:
                    raise Exception('Timed out waiting for services to start.')

    def wait_for_workloads(self, timeout=600, start=None):
        """Wait until all unit workloads are in a ready state."""
        def status_to_workloads(status):
            unit_states = defaultdict(list)
            for name, unit in status.iter_units():
                workload = unit.get('workload-status')
                if workload is not None:
                    state = workload['current']
                else:
                    state = 'unknown'
                unit_states[state].append(name)
            if set(('active', 'unknown')).issuperset(unit_states):
                return None
            unit_states.pop('unknown', None)
            return unit_states
        reporter = GroupReporter(sys.stdout, 'active')
        self._wait_for_status(reporter, status_to_workloads, WorkloadsNotReady,
                              timeout=timeout, start=start)

    def wait_for(self, thing, search_type, timeout=300):
        """ Wait for a something (thing) matching none/all/some machines.

        Examples:
          wait_for('containers', 'all')
          This will wait for a container to appear on all machines.

          wait_for('machines-not-0', 'none')
          This will wait for all machines other than 0 to be removed.

        :param thing: string, either 'containers' or 'not-machine-0'
        :param search_type: string containing none, some or all
        :param timeout: number of seconds to wait for condition to be true.
        :return:
        """
        try:
            for status in self.status_until(timeout):
                hit = False
                miss = False

                for machine, details in status.status['machines'].iteritems():
                    if thing == 'containers':
                        if 'containers' in details:
                            hit = True
                        else:
                            miss = True

                    elif thing == 'machines-not-0':
                        if machine != '0':
                            hit = True
                        else:
                            miss = True

                    else:
                        raise ValueError("Unrecognised thing to wait for: %s",
                                         thing)

                if search_type == 'none':
                    if not hit:
                        return
                elif search_type == 'some':
                    if hit:
                        return
                elif search_type == 'all':
                    if not miss:
                        return
        except Exception:
            raise Exception("Timed out waiting for %s" % thing)

    def get_matching_agent_version(self, no_build=False):
        # strip the series and srch from the built version.
        version_parts = self.version.split('-')
        if len(version_parts) == 4:
            version_number = '-'.join(version_parts[0:2])
        else:
            version_number = version_parts[0]
        if not no_build and self.env.local:
            version_number += '.1'
        return version_number

    def upgrade_controller(self, force_version=True):
        args = ()
        if force_version:
            version = self.get_matching_agent_version(no_build=True)
            args += ('--version', version)
        if self.env.local:
            args += ('--upload-tools',)

        self._upgrade_controller(args)

    def _upgrade_controller(self, args):
        controller = self.get_controller_client()
        controller.juju('upgrade-juju', args)

    def upgrade_juju(self, force_version=True):
        args = ()
        if force_version:
            version = self.get_matching_agent_version(no_build=True)
            args += ('--version', version)
        self._upgrade_juju(args)

    def _upgrade_juju(self, args):
        self.juju('upgrade-juju', args)

    def upgrade_mongo(self):
        self.juju('upgrade-mongo', ())

    def backup(self):
        try:
            output = self.get_juju_output('create-backup')
        except subprocess.CalledProcessError as e:
            log.info(e.output)
            raise
        log.info(output)
        backup_file_pattern = re.compile('(juju-backup-[0-9-]+\.(t|tar.)gz)')
        match = backup_file_pattern.search(output)
        if match is None:
            raise Exception("The backup file was not found in output: %s" %
                            output)
        backup_file_name = match.group(1)
        backup_file_path = os.path.abspath(backup_file_name)
        log.info("State-Server backup at %s", backup_file_path)
        return backup_file_path

    def restore_backup(self, backup_file):
        self.juju(
            'restore-backup',
            ('-b', '--constraints', 'mem=2G', '--file', backup_file))

    def restore_backup_async(self, backup_file):
        return self.juju_async('restore-backup', ('-b', '--constraints',
                               'mem=2G', '--file', backup_file))

    def enable_ha(self):
        self.juju('enable-ha', ('-n', '3'))

    def action_fetch(self, id, action=None, timeout="1m"):
        """Fetches the results of the action with the given id.

        Will wait for up to 1 minute for the action results.
        The action name here is just used for an more informational error in
        cases where it's available.
        Returns the yaml output of the fetched action.
        """
        out = self.get_juju_output("show-action-output", id, "--wait", timeout)
        status = yaml_loads(out)["status"]
        if status != "completed":
            name = ""
            if action is not None:
                name = " " + action
            raise Exception(
                "timed out waiting for action%s to complete during fetch" %
                name)
        return out

    def action_do(self, unit, action, *args):
        """Performs the given action on the given unit.

        Action params should be given as args in the form foo=bar.
        Returns the id of the queued action.
        """
        args = (unit, action) + args

        output = self.get_juju_output("run-action", *args)
        action_id_pattern = re.compile(
            'Action queued with id: ([a-f0-9\-]{36})')
        match = action_id_pattern.search(output)
        if match is None:
            raise Exception("Action id not found in output: %s" %
                            output)
        return match.group(1)

    def action_do_fetch(self, unit, action, timeout="1m", *args):
        """Performs given action on given unit and waits for the results.

        Action params should be given as args in the form foo=bar.
        Returns the yaml output of the action.
        """
        id = self.action_do(unit, action, *args)
        return self.action_fetch(id, action, timeout)

    def run(self, commands, applications):
        responses = self.get_juju_output(
            'run', '--format', 'json', '--application', ','.join(applications),
            *commands)
        return json.loads(responses)

    def list_space(self):
        return yaml.safe_load(self.get_juju_output('list-space'))

    def add_space(self, space):
        self.juju('add-space', (space),)

    def add_subnet(self, subnet, space):
        self.juju('add-subnet', (subnet, space))

    def is_juju1x(self):
        return self.version.startswith('1.')

    def _get_register_command(self, output):
        """Return register token from add-user output.

        Return the register token supplied within the output from the add-user
        command.

        """
        for row in output.split('\n'):
            if 'juju register' in row:
                command_string = row.strip().lstrip()
                command_parts = command_string.split(' ')
                return command_parts[-1]
        raise AssertionError('Juju register command not found in output')

    def add_user(self, username):
        """Adds provided user and return register command arguments.

        :return: Registration token provided by the add-user command.
        """
        output = self.get_juju_output(
            'add-user', username, '-c', self.env.controller.name,
            include_e=False)
        return self._get_register_command(output)

    def add_user_perms(self, username, models=None, permissions='login'):
        """Adds provided user and return register command arguments.

        :return: Registration token provided by the add-user command.
        """
        output = self.add_user(username)
        self.grant(username, permissions, models)
        return output

    def revoke(self, username, models=None, permissions='read'):
        if models is None:
            models = self.env.environment

        args = (username, permissions, models)

        self.controller_juju('revoke', args)

    def add_storage(self, unit, storage_type, amount="1"):
        """Add storage instances to service.

        Only type 'disk' is able to add instances.
        """
        self.juju('add-storage', (unit, storage_type + "=" + amount))

    def list_storage(self):
        """Return the storage list."""
        return self.get_juju_output('list-storage', '--format', 'json')

    def list_storage_pool(self):
        """Return the list of storage pool."""
        return self.get_juju_output('list-storage-pools', '--format', 'json')

    def create_storage_pool(self, name, provider, size):
        """Create storage pool."""
        self.juju('create-storage-pool',
                  (name, provider,
                   'size={}'.format(size)))

    def disable_user(self, user_name):
        """Disable an user"""
        self.controller_juju('disable-user', (user_name,))

    def enable_user(self, user_name):
        """Enable an user"""
        self.controller_juju('enable-user', (user_name,))

    def logout(self):
        """Logout an user"""
        self.controller_juju('logout', ())
        self.env.user_name = ''

    def register_user(self, user, juju_home, controller_name=None):
        """Register `user` for the `client` return the cloned client used."""
        username = user.name
        if controller_name is None:
            controller_name = '{}_controller'.format(username)

        model = self.env.environment
        token = self.add_user_perms(username, models=model,
                                    permissions=user.permissions)
        user_client = self.create_cloned_environment(juju_home,
                                                     controller_name,
                                                     username)

        try:
            child = user_client.expect('register', (token), include_e=False)
            child.expect('(?i)name')
            child.sendline(controller_name)
            child.expect('(?i)password')
            child.sendline(username + '_password')
            child.expect('(?i)password')
            child.sendline(username + '_password')
            child.expect(pexpect.EOF)
            if child.isalive():
                raise Exception(
                    'Registering user failed: pexpect session still alive')
        except pexpect.TIMEOUT:
            raise Exception(
                'Registering user failed: pexpect session timed out')
        user_client.env.user_name = username
        return user_client

    def remove_user(self, username):
        self.juju('remove-user', (username, '-y'), include_e=False)

    def create_cloned_environment(
            self, cloned_juju_home, controller_name, user_name=None):
        """Create a cloned environment.

        If `user_name` is passed ensures that the cloned environment is updated
        to match.

        """
        user_client = self.clone(env=self.env.clone())
        user_client.env.juju_home = cloned_juju_home
        if user_name is not None and user_name != self.env.user_name:
            user_client.env.user_name = user_name
            user_client.env.environment = qualified_model_name(
                user_client.env.environment, self.env.user_name)
        # New user names the controller.
        user_client.env.controller = Controller(controller_name)
        return user_client

    def grant(self, user_name, permission, model=None):
        """Grant the user with model or controller permission."""
        if permission in self.controller_permissions:
            self.juju(
                'grant',
                (user_name, permission, '-c', self.env.controller.name),
                include_e=False)
        elif permission in self.model_permissions:
            if model is None:
                model = self.model_name
            self.juju(
                'grant',
                (user_name, permission, model, '-c', self.env.controller.name),
                include_e=False)
        else:
            raise ValueError('Unknown permission {}'.format(permission))

    def list_clouds(self, format='json'):
        """List all the available clouds."""
        return self.get_juju_output('list-clouds', '--format',
                                    format, include_e=False)

    def show_controller(self, format='json'):
        """Show controller's status."""
        return self.get_juju_output('show-controller', '--format',
                                    format, include_e=False)

    def ssh_keys(self, full=False):
        """Give the ssh keys registered for the current model."""
        args = []
        if full:
            args.append('--full')
        return self.get_juju_output('ssh-keys', *args)

    def add_ssh_key(self, *keys):
        """Add one or more ssh keys to the current model."""
        return self.get_juju_output('add-ssh-key', *keys, merge_stderr=True)

    def remove_ssh_key(self, *keys):
        """Remove one or more ssh keys from the current model."""
        return self.get_juju_output('remove-ssh-key', *keys, merge_stderr=True)

    def import_ssh_key(self, *keys):
        """Import ssh keys from one or more identities to the current model."""
        return self.get_juju_output('import-ssh-key', *keys, merge_stderr=True)

    def list_disabled_commands(self):
        """List all the commands disabled on the model."""
        raw = self.get_juju_output('list-disabled-commands',
                                   '--format', 'yaml')
        return yaml.safe_load(raw)

    def disable_command(self, args):
        """Disable a command set."""
        return self.juju('disable-command', args)

    def enable_command(self, args):
        """Enable a command set."""
        return self.juju('enable-command', args)


class EnvJujuClient2B9(EnvJujuClient):

    def update_user_name(self):
        return

    def create_cloned_environment(
            self, cloned_juju_home, controller_name, user_name=None):
        """Create a cloned environment.

        `user_name` is unused in this version of beta.
        """
        user_client = self.clone(env=self.env.clone())
        user_client.env.juju_home = cloned_juju_home
        # New user names the controller.
        user_client.env.controller = Controller(controller_name)
        return user_client

    def get_model_uuid(self):
        name = self.env.environment
        output_yaml = self.get_juju_output('show-model', '--format', 'yaml')
        output = yaml.safe_load(output_yaml)
        return output[name]['model-uuid']

    def add_user_perms(self, username, models=None, permissions='read'):
        """Adds provided user and return register command arguments.

        :return: Registration token provided by the add-user command.

        """
        if models is None:
            models = self.env.environment

        args = (username, '--models', models, '--acl', permissions,
                '-c', self.env.controller.name)

        output = self.get_juju_output('add-user', *args, include_e=False)
        return self._get_register_command(output)

    def grant(self, user_name, permission, model=None):
        """Grant the user with a model."""
        if model is None:
            model = self.model_name
        self.juju('grant', (user_name, model, '--acl', permission),
                  include_e=False)

    def revoke(self, username, models=None, permissions='read'):
        if models is None:
            models = self.env.environment

        args = (username, models, '--acl', permissions)

        self.controller_juju('revoke', args)

    def set_config(self, service, options):
        option_strings = self._dict_as_option_strings(options)
        self.juju('set-config', (service,) + option_strings)

    def get_config(self, service):
        return yaml_loads(self.get_juju_output('get-config', service))

    def get_model_config(self):
        """Return the value of the environment's configured option."""
        return yaml.safe_load(
            self.get_juju_output('get-model-config', '--format', 'yaml'))

    def get_env_option(self, option):
        """Return the value of the environment's configured option."""
        return self.get_juju_output('get-model-config', option)

    def set_env_option(self, option, value):
        """Set the value of the option in the environment."""
        option_value = "%s=%s" % (option, value)
        return self.juju('set-model-config', (option_value,))

    def unset_env_option(self, option):
        """Unset the value of the option in the environment."""
        return self.juju('unset-model-config', (option,))

    def list_disabled_commands(self):
        """List all the commands disabled on the model."""
        raw = self.get_juju_output('block list', '--format', 'yaml')
        return yaml.safe_load(raw)

    def disable_command(self, args):
        """Disable a command set."""
        return self.juju('block', args)

    def enable_command(self, args):
        """Enable a command set."""
        return self.juju('unblock', args)


class EnvJujuClient2B8(EnvJujuClient2B9):

    status_class = ServiceStatus

    def remove_service(self, service):
        self.juju('remove-service', (service,))

    def run(self, commands, applications):
        responses = self.get_juju_output(
            'run', '--format', 'json', '--service', ','.join(applications),
            *commands)
        return json.loads(responses)

    def deployer(self, bundle_template, name=None, deploy_delay=10,
                 timeout=3600):
        """Deploy a bundle using deployer."""
        bundle = self.format_bundle(bundle_template)
        args = (
            '--debug',
            '--deploy-delay', str(deploy_delay),
            '--timeout', str(timeout),
            '--config', bundle,
        )
        if name:
            args += (name,)
        e_arg = ('-e', 'local.{}:{}'.format(
            self.env.controller.name, self.env.environment))
        args = e_arg + args
        self.juju('deployer', args, self.env.needs_sudo(), include_e=False)


class EnvJujuClient2B7(EnvJujuClient2B8):

    def get_controller_model_name(self):
        """Return the name of the 'controller' model.

        Return the name of the environment when an 'controller' model does
        not exist.
        """
        return 'admin'


class EnvJujuClient2B3(EnvJujuClient2B7):

    def _add_model(self, model_name, config_file):
        self.controller_juju('create-model', (
            model_name, '--config', config_file))


class EnvJujuClient2B2(EnvJujuClient2B3):

    def get_bootstrap_args(self, upload_tools, config_filename,
                           bootstrap_series=None, credential=None):
        """Return the bootstrap arguments for the substrate."""
        if self.env.joyent:
            # Only accept kvm packages by requiring >1 cpu core, see lp:1446264
            constraints = 'mem=2G cpu-cores=1'
        else:
            constraints = 'mem=2G'
        cloud_region = self.get_cloud_region(self.env.get_cloud(),
                                             self.env.get_region())
        args = ['--constraints', constraints, self.env.environment,
                cloud_region, '--config', config_filename]
        if upload_tools:
            args.insert(0, '--upload-tools')
        else:
            args.extend(['--agent-version', self.get_matching_agent_version()])

        if bootstrap_series is not None:
            args.extend(['--bootstrap-series', bootstrap_series])

        if credential is not None:
            args.extend(['--credential', credential])

        return tuple(args)

    def get_controller_client(self):
        """Return a client for the controller model.  May return self."""
        return self

    def get_controller_model_name(self):
        """Return the name of the 'controller' model.

        Return the name of the environment when an 'controller' model does
        not exist.
        """
        models = self.get_models()
        # The dict can be empty because 1.x does not support the models.
        # This is an ambiguous case for the jes feature flag which supports
        # multiple models, but none is named 'admin' by default. Since the
        # jes case also uses '-e' for models, the env is the controller model.
        for model in models.get('models', []):
            if 'admin' in model['name']:
                return 'admin'
        return self.env.environment


class EnvJujuClient2A2(EnvJujuClient2B2):
    """Drives Juju 2.0-alpha2 clients."""

    default_backend = Juju2A2Backend

    config_class = SimpleEnvironment

    @classmethod
    def _get_env(cls, env):
        if isinstance(env, JujuData):
            raise IncompatibleConfigClass(
                'JujuData cannot be used with {}'.format(cls.__name__))
        return env

    def bootstrap(self, upload_tools=False, bootstrap_series=None):
        """Bootstrap a controller."""
        self._check_bootstrap()
        args = self.get_bootstrap_args(upload_tools, bootstrap_series)
        self.juju('bootstrap', args, self.env.needs_sudo())

    @contextmanager
    def bootstrap_async(self, upload_tools=False):
        self._check_bootstrap()
        args = self.get_bootstrap_args(upload_tools)
        with self.juju_async('bootstrap', args):
            yield
            log.info('Waiting for bootstrap of {}.'.format(
                self.env.environment))

    def get_bootstrap_args(self, upload_tools, bootstrap_series=None,
                           credential=None):
        """Return the bootstrap arguments for the substrate."""
        if credential is not None:
            raise ValueError(
                '--credential is not supported by this juju version.')
        constraints = self._get_substrate_constraints()
        args = ('--constraints', constraints,
                '--agent-version', self.get_matching_agent_version())
        if upload_tools:
            args = ('--upload-tools',) + args
        if bootstrap_series is not None:
            args = args + ('--bootstrap-series', bootstrap_series)
        return args

    def deploy(self, charm, repository=None, to=None, series=None,
               service=None, force=False, storage=None, constraints=None):
        args = [charm]
        if repository is not None:
            args.extend(['--repository', repository])
        if to is not None:
            args.extend(['--to', to])
        if service is not None:
            args.extend([service])
        if storage is not None:
            args.extend(['--storage', storage])
        if constraints is not None:
            args.extend(['--constraints', constraints])
        return self.juju('deploy', tuple(args))


class EnvJujuClient2A1(EnvJujuClient2A2):
    """Drives Juju 2.0-alpha1 clients."""

    _show_status = 'status'

    default_backend = Juju1XBackend

    def get_cache_path(self):
        return get_cache_path(self.env.juju_home, models=False)

    def remove_service(self, service):
        self.juju('destroy-service', (service,))

    def backup(self):
        environ = self._shell_environ()
        # juju-backup does not support the -e flag.
        environ['JUJU_ENV'] = self.env.environment
        try:
            # Mutate os.environ instead of supplying env parameter so Windows
            # can search env['PATH']
            with scoped_environ(environ):
                args = ['juju', 'backup']
                log.info(' '.join(args))
                output = subprocess.check_output(args)
        except subprocess.CalledProcessError as e:
            log.info(e.output)
            raise
        log.info(output)
        backup_file_pattern = re.compile('(juju-backup-[0-9-]+\.(t|tar.)gz)')
        match = backup_file_pattern.search(output)
        if match is None:
            raise Exception("The backup file was not found in output: %s" %
                            output)
        backup_file_name = match.group(1)
        backup_file_path = os.path.abspath(backup_file_name)
        log.info("State-Server backup at %s", backup_file_path)
        return backup_file_path

    def restore_backup(self, backup_file):
        return self.get_juju_output('restore', '--constraints', 'mem=2G',
                                    backup_file)

    def restore_backup_async(self, backup_file):
        return self.juju_async('restore', ('--constraints', 'mem=2G',
                                           backup_file))

    def enable_ha(self):
        self.juju('ensure-availability', ('-n', '3'))

    def list_models(self):
        """List the models registered with the current controller."""
        log.info('The model is environment {}'.format(self.env.environment))

    def list_clouds(self, format='json'):
        """List all the available clouds."""
        return {}

    def show_controller(self, format='json'):
        """Show controller's status."""
        return {}

    def get_models(self):
        """return a models dict with a 'models': [] key-value pair."""
        return {}

    def _get_models(self):
        """return a list of model dicts."""
        # In 2.0-alpha1, 'list-models' produced a yaml list rather than a
        # dict, but the command and parsing are the same.
        return super(EnvJujuClient2A1, self).get_models()

    def list_controllers(self):
        """List the controllers."""
        log.info(
            'The controller is environment {}'.format(self.env.environment))

    @staticmethod
    def get_controller_member_status(info_dict):
        return info_dict.get('state-server-member-status')

    def action_fetch(self, id, action=None, timeout="1m"):
        """Fetches the results of the action with the given id.

        Will wait for up to 1 minute for the action results.
        The action name here is just used for an more informational error in
        cases where it's available.
        Returns the yaml output of the fetched action.
        """
        # the command has to be "action fetch" so that the -e <env> args are
        # placed after "fetch", since that's where action requires them to be.
        out = self.get_juju_output("action fetch", id, "--wait", timeout)
        status = yaml_loads(out)["status"]
        if status != "completed":
            name = ""
            if action is not None:
                name = " " + action
            raise Exception(
                "timed out waiting for action%s to complete during fetch" %
                name)
        return out

    def action_do(self, unit, action, *args):
        """Performs the given action on the given unit.

        Action params should be given as args in the form foo=bar.
        Returns the id of the queued action.
        """
        args = (unit, action) + args

        # the command has to be "action do" so that the -e <env> args are
        # placed after "do", since that's where action requires them to be.
        output = self.get_juju_output("action do", *args)
        action_id_pattern = re.compile(
            'Action queued with id: ([a-f0-9\-]{36})')
        match = action_id_pattern.search(output)
        if match is None:
            raise Exception("Action id not found in output: %s" %
                            output)
        return match.group(1)

    def list_space(self):
        return yaml.safe_load(self.get_juju_output('space list'))

    def add_space(self, space):
        self.juju('space create', (space),)

    def add_subnet(self, subnet, space):
        self.juju('subnet add', (subnet, space))

    def set_model_constraints(self, constraints):
        constraint_strings = self._dict_as_option_strings(constraints)
        return self.juju('set-constraints', constraint_strings)

    def set_config(self, service, options):
        option_strings = ['{}={}'.format(*item) for item in options.items()]
        self.juju('set', (service,) + tuple(option_strings))

    def get_config(self, service):
        return yaml_loads(self.get_juju_output('get', service))

    def get_model_config(self):
        """Return the value of the environment's configured option."""
        return yaml.safe_load(self.get_juju_output('get-env'))

    def get_env_option(self, option):
        """Return the value of the environment's configured option."""
        return self.get_juju_output('get-env', option)

    def set_env_option(self, option, value):
        """Set the value of the option in the environment."""
        option_value = "%s=%s" % (option, value)
        return self.juju('set-env', (option_value,))


class EnvJujuClient1X(EnvJujuClient2A1):
    """Base for all 1.x client drivers."""

    # The environments.yaml options that are replaced by bootstrap options.
    # For Juju 1.x, no bootstrap options are used.
    bootstrap_replaces = frozenset()

    destroy_model_command = 'destroy-environment'

    supported_container_types = frozenset([KVM_MACHINE, LXC_MACHINE])

    agent_metadata_url = 'tools-metadata-url'

    def _cmd_model(self, include_e, controller):
        if controller:
            return self.get_controller_model_name()
        elif self.env is None or not include_e:
            return None
        else:
            return unqualified_model_name(self.model_name)

    def get_bootstrap_args(self, upload_tools, bootstrap_series=None,
                           credential=None):
        """Return the bootstrap arguments for the substrate."""
        if credential is not None:
            raise ValueError(
                '--credential is not supported by this juju version.')
        constraints = self._get_substrate_constraints()
        args = ('--constraints', constraints)
        if upload_tools:
            args = ('--upload-tools',) + args
        if bootstrap_series is not None:
            env_val = self.env.config.get('default-series')
            if bootstrap_series != env_val:
                raise BootstrapMismatch(
                    'bootstrap-series', bootstrap_series, 'default-series',
                    env_val)
        return args

    def get_jes_command(self):
        """Return the JES command to destroy a controller.

        Juju 2.x has 'kill-controller'.
        Some intermediate versions had 'controller kill'.
        Juju 1.25 has 'system kill' when the jes feature flag is set.

        :raises: JESNotSupported when the version of Juju does not expose
            a JES command.
        :return: The JES command.
        """
        commands = self.get_juju_output('help', 'commands', include_e=False)
        for line in commands.splitlines():
            for cmd in _jes_cmds.keys():
                if line.startswith(cmd):
                    return cmd
        raise JESNotSupported()

    def upgrade_juju(self, force_version=True):
        args = ()
        if force_version:
            version = self.get_matching_agent_version(no_build=True)
            args += ('--version', version)
        if self.env.local:
            args += ('--upload-tools',)
        self._upgrade_juju(args)

    def make_model_config(self):
        config_dict = make_safe_config(self)
        # Strip unneeded variables.
        return config_dict

    def _add_model(self, model_name, config_file):
        seen_cmd = self.get_jes_command()
        if seen_cmd == SYSTEM:
            controller_option = ('-s', self.env.environment)
        else:
            controller_option = ('-c', self.env.environment)
        self.juju(_jes_cmds[seen_cmd]['create'], controller_option + (
            model_name, '--config', config_file), include_e=False)

    def destroy_model(self):
        """With JES enabled, destroy-environment destroys the model."""
        self.destroy_environment(force=False)

    def destroy_environment(self, force=True, delete_jenv=False):
        if force:
            force_arg = ('--force',)
        else:
            force_arg = ()
        exit_status = self.juju(
            'destroy-environment',
            (self.env.environment,) + force_arg + ('-y',),
            self.env.needs_sudo(), check=False, include_e=False,
            timeout=get_teardown_timeout(self))
        if delete_jenv:
            jenv_path = get_jenv_path(self.env.juju_home, self.env.environment)
            ensure_deleted(jenv_path)
        return exit_status

    def _get_models(self):
        """return a list of model dicts."""
        try:
            return yaml.safe_load(self.get_juju_output(
                'environments', '-s', self.env.environment, '--format', 'yaml',
                include_e=False))
        except subprocess.CalledProcessError:
            # This *private* method attempts to use a 1.25 JES feature.
            # The JES design is dead. The private method is not used to
            # directly test juju cli; the failure is not a contract violation.
            log.info('Call to JES juju environments failed, falling back.')
            return []

    def deploy_bundle(self, bundle, timeout=_DEFAULT_BUNDLE_TIMEOUT):
        """Deploy bundle using deployer for Juju 1.X version."""
        self.deployer(bundle, timeout=timeout)

    def deployer(self, bundle_template, name=None, deploy_delay=10,
                 timeout=3600):
        """Deploy a bundle using deployer."""
        bundle = self.format_bundle(bundle_template)
        args = (
            '--debug',
            '--deploy-delay', str(deploy_delay),
            '--timeout', str(timeout),
            '--config', bundle,
        )
        if name:
            args += (name,)
        self.juju('deployer', args, self.env.needs_sudo())

    def upgrade_charm(self, service, charm_path=None):
        args = (service,)
        if charm_path is not None:
            repository = os.path.dirname(os.path.dirname(charm_path))
            args = args + ('--repository', repository)
        self.juju('upgrade-charm', args)

    def get_controller_endpoint(self):
        """Return the address of the state-server leader."""
        endpoint = self.get_juju_output('api-endpoints')
        address, port = split_address_port(endpoint)
        return address

    def upgrade_mongo(self):
        raise UpgradeMongoNotSupported()

    def add_storage(self, unit, storage_type, amount="1"):
        """Add storage instances to service.

        Only type 'disk' is able to add instances.
        """
        self.juju('storage add', (unit, storage_type + "=" + amount))

    def list_storage(self):
        """Return the storage list."""
        return self.get_juju_output('storage list', '--format', 'json')

    def list_storage_pool(self):
        """Return the list of storage pool."""
        return self.get_juju_output('storage pool list', '--format', 'json')

    def create_storage_pool(self, name, provider, size):
        """Create storage pool."""
        self.juju('storage pool create',
                  (name, provider,
                   'size={}'.format(size)))

    def ssh_keys(self, full=False):
        """Give the ssh keys registered for the current model."""
        args = []
        if full:
            args.append('--full')
        return self.get_juju_output('authorized-keys list', *args)

    def add_ssh_key(self, *keys):
        """Add one or more ssh keys to the current model."""
        return self.get_juju_output('authorized-keys add', *keys,
                                    merge_stderr=True)

    def remove_ssh_key(self, *keys):
        """Remove one or more ssh keys from the current model."""
        return self.get_juju_output('authorized-keys delete', *keys,
                                    merge_stderr=True)

    def import_ssh_key(self, *keys):
        """Import ssh keys from one or more identities to the current model."""
        return self.get_juju_output('authorized-keys import', *keys,
                                    merge_stderr=True)


class EnvJujuClient22(EnvJujuClient1X):

    used_feature_flags = frozenset(['actions'])

    def __init__(self, *args, **kwargs):
        super(EnvJujuClient22, self).__init__(*args, **kwargs)
        self.feature_flags.add('actions')


class EnvJujuClient26(EnvJujuClient1X):
    """Drives Juju 2.6-series clients."""

    used_feature_flags = frozenset(['address-allocation', 'cloudsigma', 'jes'])

    def __init__(self, *args, **kwargs):
        super(EnvJujuClient26, self).__init__(*args, **kwargs)
        if self.env is None or self.env.config is None:
            return
        if self.env.config.get('type') == 'cloudsigma':
            self.feature_flags.add('cloudsigma')

    def enable_jes(self):
        """Enable JES if JES is optional.

        :raises: JESByDefault when JES is always enabled; Juju has the
            'destroy-controller' command.
        :raises: JESNotSupported when JES is not supported; Juju does not have
            the 'system kill' command when the JES feature flag is set.
        """

        if 'jes' in self.feature_flags:
            return
        if self.is_jes_enabled():
            raise JESByDefault()
        self.feature_flags.add('jes')
        if not self.is_jes_enabled():
            self.feature_flags.remove('jes')
            raise JESNotSupported()

    def disable_jes(self):
        if 'jes' in self.feature_flags:
            self.feature_flags.remove('jes')

    def enable_container_address_allocation(self):
        self.feature_flags.add('address-allocation')


class EnvJujuClient25(EnvJujuClient26):
    """Drives Juju 2.5-series clients."""


class EnvJujuClient24(EnvJujuClient25):
    """Similar to EnvJujuClient25, but lacking JES support."""

    used_feature_flags = frozenset(['cloudsigma'])

    def enable_jes(self):
        raise JESNotSupported()

    def add_ssh_machines(self, machines):
        for machine in machines:
            self.juju('add-machine', ('ssh:' + machine,))


def get_local_root(juju_home, env):
    return os.path.join(juju_home, env.environment)


def bootstrap_from_env(juju_home, client):
    with temp_bootstrap_env(juju_home, client):
        client.bootstrap()


def quickstart_from_env(juju_home, client, bundle):
    with temp_bootstrap_env(juju_home, client):
        client.quickstart(bundle)


@contextmanager
def maybe_jes(client, jes_enabled, try_jes):
    """If JES is desired and not enabled, try to enable it for this context.

    JES will be in its previous state after exiting this context.
    If jes_enabled is True or try_jes is False, the context is a no-op.
    If enable_jes() raises JESNotSupported, JES will not be enabled in the
    context.

    The with value is True if JES is enabled in the context.
    """

    class JESUnwanted(Exception):
        """Non-error.  Used to avoid enabling JES if not wanted."""

    try:
        if not try_jes or jes_enabled:
            raise JESUnwanted
        client.enable_jes()
    except (JESNotSupported, JESUnwanted):
        yield jes_enabled
        return
    else:
        try:
            yield True
        finally:
            client.disable_jes()


def tear_down(client, jes_enabled, try_jes=False):
    """Tear down a JES or non-JES environment.

    JES environments are torn down via 'controller kill' or 'system kill',
    and non-JES environments are torn down via 'destroy-environment --force.'
    """
    with maybe_jes(client, jes_enabled, try_jes) as jes_enabled:
        if jes_enabled:
            client.kill_controller()
        else:
            if client.destroy_environment(force=False) != 0:
                client.destroy_environment(force=True)


def uniquify_local(env):
    """Ensure that local environments have unique port settings.

    This allows local environments to be duplicated despite
    https://bugs.launchpad.net/bugs/1382131
    """
    if not env.local:
        return
    port_defaults = {
        'api-port': 17070,
        'state-port': 37017,
        'storage-port': 8040,
        'syslog-port': 6514,
    }
    for key, default in port_defaults.items():
        env.config[key] = env.config.get(key, default) + 1


def dump_environments_yaml(juju_home, config):
    """Dump yaml data to the environment file.

    :param juju_home: Path to the JUJU_HOME directory.
    :param config: Dictionary repersenting yaml data to dump."""
    environments_path = get_environments_path(juju_home)
    with open(environments_path, 'w') as config_file:
        yaml.safe_dump(config, config_file)


@contextmanager
def _temp_env(new_config, parent=None, set_home=True):
    """Use the supplied config as juju environment.

    This is not a fully-formed version for bootstrapping.  See
    temp_bootstrap_env.
    """
    with temp_dir(parent) as temp_juju_home:
        dump_environments_yaml(temp_juju_home, new_config)
        if set_home:
            context = scoped_environ()
        else:
            context = nested()
        with context:
            if set_home:
                os.environ['JUJU_HOME'] = temp_juju_home
                os.environ['JUJU_DATA'] = temp_juju_home
            yield temp_juju_home


def jes_home_path(juju_home, dir_name):
    return os.path.join(juju_home, 'jes-homes', dir_name)


def get_cache_path(juju_home, models=False):
    if models:
        root = os.path.join(juju_home, 'models')
    else:
        root = os.path.join(juju_home, 'environments')
    return os.path.join(root, 'cache.yaml')


def make_safe_config(client):
    config = dict(client.env.config)
    if 'agent-version' in client.bootstrap_replaces:
        config.pop('agent-version', None)
    else:
        config['agent-version'] = client.get_matching_agent_version()
    # AFAICT, we *always* want to set test-mode to True.  If we ever find a
    # use-case where we don't, we can make this optional.
    config['test-mode'] = True
    # Explicitly set 'name', which Juju implicitly sets to env.environment to
    # ensure MAASAccount knows what the name will be.
    config['name'] = unqualified_model_name(client.env.environment)
    if config['type'] == 'local':
        config.setdefault('root-dir', get_local_root(client.env.juju_home,
                          client.env))
        # MongoDB requires a lot of free disk space, and the only
        # visible error message is from "juju bootstrap":
        # "cannot initiate replication set" if disk space is low.
        # What "low" exactly means, is unclear, but 8GB should be
        # enough.
        ensure_dir(config['root-dir'])
        check_free_disk_space(config['root-dir'], 8000000, "MongoDB files")
        if client.env.kvm:
            check_free_disk_space(
                "/var/lib/uvtool/libvirt/images", 2000000,
                "KVM disk files")
        else:
            check_free_disk_space(
                "/var/lib/lxc", 2000000, "LXC containers")
    return config


@contextmanager
def temp_bootstrap_env(juju_home, client, set_home=True, permanent=False):
    """Create a temporary environment for bootstrapping.

    This involves creating a temporary juju home directory and returning its
    location.

    :param set_home: Set JUJU_HOME to match the temporary home in this
        context.  If False, juju_home should be supplied to bootstrap.
    """
    new_config = {
        'environments': {client.env.environment: make_safe_config(client)}}
    # Always bootstrap a matching environment.
    jenv_path = get_jenv_path(juju_home, client.env.environment)
    if permanent:
        context = client.env.make_jes_home(
            juju_home, client.env.environment, new_config)
    else:
        context = _temp_env(new_config, juju_home, set_home)
    with context as temp_juju_home:
        if os.path.lexists(jenv_path):
            raise Exception('%s already exists!' % jenv_path)
        new_jenv_path = get_jenv_path(temp_juju_home, client.env.environment)
        # Create a symlink to allow access while bootstrapping, and to reduce
        # races.  Can't use a hard link because jenv doesn't exist until
        # partway through bootstrap.
        ensure_dir(os.path.join(juju_home, 'environments'))
        # Skip creating symlink where not supported (i.e. Windows).
        if not permanent and getattr(os, 'symlink', None) is not None:
            os.symlink(new_jenv_path, jenv_path)
        old_juju_home = client.env.juju_home
        client.env.juju_home = temp_juju_home
        try:
            yield temp_juju_home
        finally:
            if not permanent:
                # replace symlink with file before deleting temp home.
                try:
                    os.rename(new_jenv_path, jenv_path)
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        raise
                    # Remove dangling symlink
                    try:
                        os.unlink(jenv_path)
                    except OSError as e:
                        if e.errno != errno.ENOENT:
                            raise
                client.env.juju_home = old_juju_home


def get_machine_dns_name(client, machine, timeout=600):
    """Wait for dns-name on a juju machine."""
    for status in client.status_until(timeout=timeout):
        try:
            return _dns_name_for_machine(status, machine)
        except KeyError:
            log.debug("No dns-name yet for machine %s", machine)


def _dns_name_for_machine(status, machine):
    host = status.status['machines'][machine]['dns-name']
    if is_ipv6_address(host):
        log.warning("Selected IPv6 address for machine %s: %r", machine, host)
    return host


class Controller:
    """Represents the controller for a model or models."""

    def __init__(self, name):
        self.name = name


class GroupReporter:

    def __init__(self, stream, expected):
        self.stream = stream
        self.expected = expected
        self.last_group = None
        self.ticks = 0
        self.wrap_offset = 0
        self.wrap_width = 79

    def _write(self, string):
        self.stream.write(string)
        self.stream.flush()

    def finish(self):
        if self.last_group:
            self._write("\n")

    def update(self, group):
        if group == self.last_group:
            if (self.wrap_offset + self.ticks) % self.wrap_width == 0:
                self._write("\n")
            self._write("." if self.ticks or not self.wrap_offset else " .")
            self.ticks += 1
            return
        value_listing = []
        for value, entries in sorted(group.items()):
            if value == self.expected:
                continue
            value_listing.append('%s: %s' % (value, ', '.join(entries)))
        string = ' | '.join(value_listing)
        lead_length = len(string) + 1
        if self.last_group:
            string = "\n" + string
        self._write(string)
        self.last_group = group
        self.ticks = 0
        self.wrap_offset = lead_length if lead_length < self.wrap_width else 0
