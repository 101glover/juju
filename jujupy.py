from __future__ import print_function

from collections import defaultdict
from contextlib import (
    contextmanager,
    nested,
)
from cStringIO import StringIO
from datetime import timedelta
import errno
from itertools import chain
import logging
import os
import re
from shutil import rmtree
import subprocess
import sys
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
    pause,
    scoped_environ,
    temp_dir,
    until_timeout,
)


__metaclass__ = type

AGENTS_READY = set(['started', 'idle'])
WIN_JUJU_CMD = os.path.join('\\', 'Progra~2', 'Juju', 'juju.exe')

JUJU_DEV_FEATURE_FLAGS = 'JUJU_DEV_FEATURE_FLAGS'
CONTROLLER = 'controller'
KILL_CONTROLLER = 'kill-controller'
SYSTEM = 'system'

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


def get_timeout_path():
    import timeout
    return os.path.abspath(timeout.__file__)


def get_timeout_prefix(duration, timeout_path=None):
    """Return extra arguments to run a command with a timeout."""
    if timeout_path is None:
        timeout_path = get_timeout_path()
    return (sys.executable, timeout_path, '%.2f' % duration, '--')


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


class JESNotSupported(Exception):

    def __init__(self):
        super(JESNotSupported, self).__init__(
            'This client does not support JES')


class JESByDefault(Exception):

    def __init__(self):
        super(JESByDefault, self).__init__(
            'This client does not need to enable JES')


def yaml_loads(yaml_str):
    return yaml.safe_load(StringIO(yaml_str))


def coalesce_agent_status(agent_item):
    """Return the machine agent-state or the unit agent-status."""
    state = agent_item.get('agent-state')
    if state is None and agent_item.get('agent-status') is not None:
        state = agent_item.get('agent-status').get('current')
    if state is None:
        state = 'no-agent'
    return state


def make_client(juju_path, debug, env_name, temp_env_name):
    env = SimpleEnvironment.from_config(env_name)
    if temp_env_name is not None:
        env.environment = temp_env_name
        env.config['name'] = temp_env_name
    return EnvJujuClient.by_version(env, juju_path, debug)


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


class EnvJujuClient:

    # The environments.yaml options that are replaced by bootstrap options.
    #
    # As described in bug #1538735, default-series and --bootstrap-series must
    # match.  'default-series' should be here, but is omitted so that
    # default-series is always forced to match --bootstrap-series.
    bootstrap_replaces = frozenset(['agent-version'])

    _show_status = 'show-status'

    @classmethod
    def get_version(cls, juju_path=None):
        if juju_path is None:
            juju_path = 'juju'
        return subprocess.check_output((juju_path, '--version')).strip()

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

    @classmethod
    def by_version(cls, env, juju_path=None, debug=False):
        version = cls.get_version(juju_path)
        if juju_path is None:
            full_path = cls.get_full_path()
        else:
            full_path = os.path.abspath(juju_path)
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
        elif re.match('^2\.0-alpha2-fake-wrapper', version):
            client_class = EnvJujuClient
        elif re.match('^2\.0-alpha[12]', version):
            client_class = EnvJujuClient2A1
        else:
            client_class = EnvJujuClient
        return client_class(env, version, full_path, debug=debug)

    def clone(self, env=None, version=None, full_path=None, debug=None,
              cls=None):
        """Create a clone of this EnvJujuClient.

        By default, the class, environment, version, full_path, and debug
        settings will match the original, but each can be overridden.
        """
        if env is None:
            env = self.env
        if version is None:
            version = self.version
        if full_path is None:
            full_path = self.full_path
        if debug is None:
            debug = self.debug
        if cls is None:
            cls = self.__class__
        return cls(env, version, full_path, debug=debug)

    def get_cache_path(self):
        return get_cache_path(self.env.juju_home, models=True)

    def _full_args(self, command, sudo, args, timeout=None, include_e=True):
        # sudo is not needed for devel releases.
        if self.env is None or not include_e:
            e_arg = ()
        else:
            e_arg = ('-m', self.env.environment)
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
        return prefix + ('juju', logging,) + tuple(command) + e_arg + args

    def __init__(self, env, version, full_path, juju_home=None, debug=False):
        self.env = env
        if version == '2.0-alpha2-fake-wrapper':
            version = '2.0-alpha1-juju-wrapped'
        self.version = version
        self.full_path = full_path
        self.debug = debug
        if env is not None:
            if juju_home is None:
                if env.juju_home is None:
                    env.juju_home = get_juju_home()
            else:
                env.juju_home = juju_home
        self.juju_timings = {}
        self._timeout_path = get_timeout_path()

    def _shell_environ(self):
        """Generate a suitable shell environment.

        Juju's directory must be in the PATH to support plugins.
        """
        env = dict(os.environ)
        if self.full_path is not None:
            env['PATH'] = '{}{}{}'.format(os.path.dirname(self.full_path),
                                          os.pathsep, env['PATH'])
        env['JUJU_HOME'] = self.env.juju_home
        return env

    def add_ssh_machines(self, machines):
        for machine in machines:
            self.juju('add-machine', ('ssh:' + machine,))

    def get_bootstrap_args(self, upload_tools, bootstrap_series=None):
        """Return the bootstrap arguments for the substrate."""
        if self.env.maas:
            constraints = 'mem=2G arch=amd64'
        elif self.env.joyent:
            # Only accept kvm packages by requiring >1 cpu core, see lp:1446264
            constraints = 'mem=2G cpu-cores=1'
        else:
            constraints = 'mem=2G'
        args = ('--constraints', constraints,
                '--agent-version', self.get_matching_agent_version())
        if upload_tools:
            args = ('--upload-tools',) + args
        if bootstrap_series is not None:
            args = args + ('--bootstrap-series', bootstrap_series)
        return args

    def bootstrap(self, upload_tools=False, bootstrap_series=None):
        """Bootstrap a controller."""
        args = self.get_bootstrap_args(upload_tools, bootstrap_series)
        self.juju('bootstrap', args, self.env.needs_sudo())

    @contextmanager
    def bootstrap_async(self, upload_tools=False):
        args = self.get_bootstrap_args(upload_tools)
        with self.juju_async('bootstrap', args):
            yield
            log.info('Waiting for bootstrap of {}.'.format(
                self.env.environment))

    def create_environment(self, controller_client, config_file):
        self.juju('create-model', (
            '-c', controller_client.env.environment, self.env.environment,
            '--config', config_file), include_e=False)

    def destroy_model(self):
        exit_status = self.juju(
            'destroy-model', (self.env.environment, '-y',),
            include_e=False, timeout=timedelta(minutes=10).total_seconds())
        return exit_status

    def kill_controller(self):
        """Kill a controller and its environments."""
        seen_cmd = self.get_jes_command()
        self.juju(
            _jes_cmds[seen_cmd]['kill'], (self.env.environment, '-y'),
            include_e=False, check=False, timeout=600)

    def get_juju_output(self, command, *args, **kwargs):
        """Call a juju command and return the output.

        Sub process will be called as 'juju <command> <args> <kwargs>'. Note
        that <command> may be a space delimited list of arguments. The -e
        <environment> flag will be placed after <command> and before args.
        """
        args = self._full_args(command, False, args,
                               timeout=kwargs.get('timeout'),
                               include_e=kwargs.get('include_e', True))
        env = self._shell_environ()
        log.debug(args)
        # Mutate os.environ instead of supplying env parameter so
        # Windows can search env['PATH']
        with scoped_environ(env):
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                stderr=subprocess.PIPE)
            sub_output, sub_error = proc.communicate()
            log.debug(sub_output)
            if proc.returncode != 0:
                log.debug(sub_error)
                e = subprocess.CalledProcessError(
                    proc.returncode, args[0], sub_error)
                e.stderr = sub_error
                if (
                    'Unable to connect to environment' in sub_error or
                        'MissingOrIncorrectVersionHeader' in sub_error or
                        '307: Temporary Redirect' in sub_error):
                    raise CannotConnectEnv(e)
                raise e
        return sub_output

    def show_status(self):
        """Print the status to output."""
        self.juju(self._show_status, ('--format', 'yaml'))

    def get_status(self, timeout=60, raw=False, *args):
        """Get the current status as a dict."""
        # GZ 2015-12-16: Pass remaining timeout into get_juju_output call.
        for ignored in until_timeout(timeout):
            try:
                if raw:
                    return self.get_juju_output(self._show_status, *args)
                return Status.from_text(
                    self.get_juju_output(
                        self._show_status, '--format', 'yaml'))
            except subprocess.CalledProcessError:
                pass
        raise Exception(
            'Timed out waiting for juju status to succeed')

    def set_config(self, service, options):
        option_strings = ['{}={}'.format(*item) for item in options.items()]
        self.juju('set-config', (service,) + tuple(option_strings))

    def get_config(self, service):
        return yaml_loads(self.get_juju_output('get-config', service))

    def get_service_config(self, service, timeout=60):
        for ignored in until_timeout(timeout):
            try:
                return self.get_config(service)
            except subprocess.CalledProcessError:
                pass
        raise Exception(
            'Timed out waiting for juju get %s' % (service))

    def get_model_config(self):
        """Return the value of the environment's configured option."""
        return yaml.safe_load(self.get_juju_output('get-model-config'))

    def get_env_option(self, option):
        """Return the value of the environment's configured option."""
        return self.get_juju_output('get-model-config', option)

    def set_env_option(self, option, value):
        """Set the value of the option in the environment."""
        option_value = "%s=%s" % (option, value)
        return self.juju('set-model-config', (option_value,))

    def set_testing_tools_metadata_url(self):
        url = self.get_env_option('tools-metadata-url')
        if 'testing' not in url:
            testing_url = url.replace('/tools', '/testing/tools')
            self.set_env_option('tools-metadata-url', testing_url)

    def juju(self, command, args, sudo=False, check=True, include_e=True,
             timeout=None, extra_env=None):
        """Run a command under juju for the current environment."""
        args = self._full_args(command, sudo, args, include_e=include_e,
                               timeout=timeout)
        log.info(' '.join(args))
        env = self._shell_environ()
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
            rval = call_func(args)
        self.juju_timings.setdefault(args, []).append(
            (time.time() - start_time))
        return rval

    def get_juju_timings(self):
        stringified_timings = {}
        for command, timings in self.juju_timings.items():
            stringified_timings[' '.join(command)] = timings
        return stringified_timings

    @contextmanager
    def juju_async(self, command, args, include_e=True, timeout=None):
        full_args = self._full_args(command, False, args, include_e=include_e,
                                    timeout=timeout)
        log.info(' '.join(args))
        env = self._shell_environ()
        # Mutate os.environ instead of supplying env parameter so Windows can
        # search env['PATH']
        with scoped_environ(env):
            proc = subprocess.Popen(full_args)
        yield proc
        retcode = proc.wait()
        if retcode != 0:
            raise subprocess.CalledProcessError(retcode, full_args)

    def deploy(self, charm, repository=None, to=None, service=None):
        args = [charm]
        if repository is not None:
            args.extend(['--repository', repository])
        if to is not None:
            args.extend(['--to', to])
        if service is not None:
            args.extend([service])
        return self.juju('deploy', tuple(args))

    def remove_service(self, service):
        self.juju('remove-service', (service,))

    def deployer(self, bundle, name=None, deploy_delay=10, timeout=3600):
        """deployer, using sudo if necessary."""
        args = (
            '--debug',
            '--deploy-delay', str(deploy_delay),
            '--timeout', str(timeout),
            '--config', bundle,
        )
        if name:
            args += (name,)
        self.juju('deployer', args, self.env.needs_sudo())

    def quickstart(self, bundle, upload_tools=False):
        """quickstart, using sudo if necessary."""
        if self.env.maas:
            constraints = 'mem=2G arch=amd64'
        else:
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
            for _ in chain([None], until_timeout(timeout, start=start)):
                try:
                    status = self.get_status()
                except CannotConnectEnv:
                    log.info('Suppressing "Unable to connect to environment"')
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

    def wait_for_ha(self, timeout=1200):
        desired_state = 'has-vote'
        reporter = GroupReporter(sys.stdout, desired_state)
        try:
            for remaining in until_timeout(timeout):
                status = self.get_status()
                states = {}
                for machine, info in status.iter_machines():
                    status = info.get('state-server-member-status')
                    if status is None:
                        continue
                    states.setdefault(status, []).append(machine)
                if states.keys() == [desired_state]:
                    if len(states.get(desired_state, [])) >= 3:
                        # XXX sinzui 2014-12-04: bug 1399277 happens because
                        # juju claims HA is ready when the monogo replica sets
                        # are not. Juju is not fully usable. The replica set
                        # lag might be 5 minutes.
                        pause(300)
                        return
                reporter.update(states)
            else:
                raise Exception('Timed out waiting for voting to be enabled.')
        finally:
            reporter.finish()

    def wait_for_deploy_started(self, service_count=1, timeout=1200):
        """Wait until service_count services are 'started'.

        :param service_count: The number of services for which to wait.
        :param timeout: The number of seconds to wait.
        """
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

    def upgrade_juju(self, force_version=True):
        args = ()
        if force_version:
            version = self.get_matching_agent_version(no_build=True)
            args += ('--version', version)
        if self.env.local:
            args += ('--upload-tools',)
        self.juju('upgrade-juju', args)

    def backup(self):
        environ = self._shell_environ()
        try:
            # Mutate os.environ instead of supplying env parameter so Windows
            # can search env['PATH']
            with scoped_environ(environ):
                args = self._full_args(
                    'create-backup', False, (), include_e=True)
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
        return self.get_juju_output('restore-backup', '-b', '--constraints',
                                    'mem=2G', '--file', backup_file)

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

    def list_space(self):
        return yaml.safe_load(self.get_juju_output('list-space'))

    def add_space(self, space):
        self.juju('add-space', (space),)

    def add_subnet(self, subnet, space):
        self.juju('add-subnet', (subnet, space))


class EnvJujuClient2A1(EnvJujuClient):
    """Drives Juju 2.0-alpha1 clients."""

    _show_status = 'status'

    def get_cache_path(self):
        return get_cache_path(self.env.juju_home, models=False)

    def _full_args(self, command, sudo, args, timeout=None, include_e=True):
        # sudo is not needed for devel releases.
        if self.env is None or not include_e:
            e_arg = ()
        else:
            e_arg = ('-e', self.env.environment)
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
        return prefix + ('juju', logging,) + tuple(command) + e_arg + args

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
                output = subprocess.check_output(['juju', 'backup'])
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

    def get_bootstrap_args(self, upload_tools, bootstrap_series=None):
        """Return the bootstrap arguments for the substrate."""
        if self.env.maas:
            constraints = 'mem=2G arch=amd64'
        elif self.env.joyent:
            # Only accept kvm packages by requiring >1 cpu core, see lp:1446264
            constraints = 'mem=2G cpu-cores=1'
        else:
            constraints = 'mem=2G'
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

    def create_environment(self, controller_client, config_file):
        seen_cmd = self.get_jes_command()
        if seen_cmd == SYSTEM:
            controller_option = ('-s', controller_client.env.environment)
        else:
            controller_option = ('-c', controller_client.env.environment)
        self.juju(_jes_cmds[seen_cmd]['create'], controller_option + (
            self.env.environment, '--config', config_file), include_e=False)

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
            timeout=timedelta(minutes=10).total_seconds())
        if delete_jenv:
            jenv_path = get_jenv_path(self.env.juju_home, self.env.environment)
            ensure_deleted(jenv_path)
        return exit_status


class EnvJujuClient22(EnvJujuClient1X):

    def _shell_environ(self):
        """Generate a suitable shell environment.

        Juju's directory must be in the PATH to support plugins.
        """
        env = super(EnvJujuClient22, self)._shell_environ()
        env[JUJU_DEV_FEATURE_FLAGS] = 'actions'
        return env


class EnvJujuClient26(EnvJujuClient1X):
    """Drives Juju 2.6-series clients."""

    def __init__(self, *args, **kwargs):
        super(EnvJujuClient26, self).__init__(*args, **kwargs)
        self._use_jes = False
        self._use_container_address_allocation = False

    def clone(self, env=None, version=None, full_path=None, debug=None,
              cls=None):
        """Create a clone of this EnvJujuClient.

        By default, the class, environment, version, full_path, and debug
        settings will match the original, but each can be overridden.
        """
        client = super(EnvJujuClient26, self).clone(env, version, full_path,
                                                    debug, cls)
        client._use_jes = self._use_jes
        client._use_container_address_allocation = (
            self._use_container_address_allocation)
        return client

    def enable_jes(self):
        """Enable JES if JES is optional.

        :raises: JESByDefault when JES is always enabled; Juju has the
            'destroy-controller' command.
        :raises: JESNotSupported when JES is not supported; Juju does not have
            the 'system kill' command when the JES feature flag is set.
        """
        if self._use_jes:
            return
        if self.is_jes_enabled():
            raise JESByDefault()
        self._use_jes = True
        if not self.is_jes_enabled():
            self._use_jes = False
            raise JESNotSupported()

    def disable_jes(self):
        self._use_jes = False

    def enable_container_address_allocation(self):
        self._use_container_address_allocation = True

    def _get_feature_flags(self):
        if self.env.config.get('type') == 'cloudsigma':
            yield 'cloudsigma'
        if self._use_jes is True:
            yield 'jes'
        if self._use_container_address_allocation:
            yield 'address-allocation'

    def _shell_environ(self):
        """Generate a suitable shell environment.

        Juju's directory must be in the PATH to support plugins.
        """
        env = super(EnvJujuClient26, self)._shell_environ()
        feature_flags = self._get_feature_flags()
        env[JUJU_DEV_FEATURE_FLAGS] = ','.join(feature_flags)
        return env


class EnvJujuClient25(EnvJujuClient26):
    """Drives Juju 2.5-series clients."""


class EnvJujuClient24(EnvJujuClient25):
    """Similar to EnvJujuClient25, but lacking JES support."""

    def enable_jes(self):
        raise JESNotSupported()

    def _get_feature_flags(self):
        if self.env.config.get('type') == 'cloudsigma':
            yield 'cloudsigma'


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
            yield temp_juju_home


def jes_home_path(juju_home, dir_name):
    return os.path.join(juju_home, 'jes-homes', dir_name)


def get_cache_path(juju_home, models=False):
    if models:
        root = os.path.join(juju_home, 'models')
    else:
        root = os.path.join(juju_home, 'environments')
    return os.path.join(root, 'cache.yaml')


@contextmanager
def make_jes_home(juju_home, dir_name, config):
    home_path = jes_home_path(juju_home, dir_name)
    if os.path.exists(home_path):
        rmtree(home_path)
    os.makedirs(home_path)
    dump_environments_yaml(home_path, config)
    yield home_path


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
    config['name'] = client.env.environment
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
        context = make_jes_home(juju_home, client.env.environment, new_config)
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


class Status:

    def __init__(self, status, status_text):
        self.status = status
        self.status_text = status_text

    @classmethod
    def from_text(cls, text):
        status_yaml = yaml_loads(text)
        return cls(status_yaml, text)

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
        for service_name, service in sorted(self.status['services'].items()):
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
                raise ErroredUnit(entries[0], state)
        return states

    def get_service_count(self):
        return len(self.status.get('services', {}))

    def get_service_unit_count(self, service):
        return len(
            self.status.get('services', {}).get(service, {}).get('units', {}))

    def get_agent_versions(self):
        versions = defaultdict(set)
        for item_name, item in self.agent_items():
            versions[item.get('agent-version', 'unknown')].add(item_name)
        return versions

    def get_instance_id(self, machine_id):
        return self.status['machines'][machine_id]['instance-id']

    def get_unit(self, unit_name):
        """Return metadata about a unit."""
        for service in sorted(self.status['services'].values()):
            if unit_name in service.get('units', {}):
                return service['units'][unit_name]
        raise KeyError(unit_name)

    def service_subordinate_units(self, service_name):
        """Return subordinate metadata for a service_name."""
        services = self.status.get('services', {})
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


class SimpleEnvironment:

    def __init__(self, environment, config=None, juju_home=None):
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

    @classmethod
    def from_config(cls, name):
        config, selected = get_selected_environment(name)
        if name is None:
            name = selected
        return cls(name, config)

    def needs_sudo(self):
        return self.local


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
