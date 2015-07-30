#!/usr/bin/env python
from time import sleep
from deploy_stack import (
    deploy_dummy_stack,
    SimpleEnvironment,
    get_random_string,
    EnvJujuClient,
    configure_logging,
    boot_context,
    sys,
    check_token,
)


def jes_setup(args):
    """jes_setup sets up the juju client and its environment.
       It returns return client, charm_prefix and base_env"""
    base_env = args.env
    configure_logging(args.verbose)
    series = args.series
    if series is None:
        series = 'precise'
    charm_prefix = 'local:{}/'.format(series)
    if sys.platform == 'win32':
        # Ensure OpenSSH is never in the path for win tests.
        sys.path = [p for p in sys.path if 'OpenSSH' not in p]
    client = EnvJujuClient.by_version(
        SimpleEnvironment.from_config(base_env),
        EnvJujuClient.get_full_path(),
        args.debug,
    )
    env = client._shell_environ()
    with boot_context(
            args.job_name,
            client,
            args.bootstrap_host,
            args.machine,
            args.series,
            args.agent_stream,
            args.agent_url,
            args.logs, args.keep_env,
            args.upload_tools,
            args.juju_home,
            extra_env=env,
            ):
        if args.machines is not None:
            client.add_ssh_machines(args.machines)
        if sys.platform in ('win32', 'darwin'):
            # The win and osx client tests only verify the client
            # can bootstrap and call the state-server.
            return
    return client, charm_prefix, base_env


def env_token(env_name):
    return env_name + get_random_string()


def deploy_dummy_stack_in_environ(client, charm_prefix, env_name):
    env = client._shell_environ()
    # first create the environment
    client.juju(
        "system create-environment", (env_name,),
        extra_env=env,
    )

    # switch to environment
    client.env.environment = env_name
    client.juju(
        "environment set",
        ("default-series=trusty", "-e", env_name),
        extra_env=env,
    )

    # then deploy a dummy stack in it
    token = env_token(env_name)
    return deploy_dummy_stack(client, charm_prefix, token)


def check_updated_token(client, token, timeout):
    wait = 5
    try:
        check_token(client, token)
    except ValueError as err:
        print("INFO: waiting for token to update: {}".format(str(err)))
        if timeout > 0:
            sleep(wait)
            check_updated_token(client, token, timeout - wait)


def check_services(client, env):
    token = env_token(env)
    client.env.environment = env
    client.juju('set', ('dummy-source', 'token=%s' % token))
    print("checking services in "+env)
    check_updated_token(client, token, 30)
