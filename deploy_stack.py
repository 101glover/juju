#!/usr/bin/env python
from __future__ import print_function
__metaclass__ = type


from argparse import ArgumentParser
import sys

from jujupy import (
    check_wordpress,
    Environment,
    until_timeout,
)


def deploy_stack(environments, charm_prefix):
    """"Deploy a Wordpress stack in the specified environment.

    :param environment: The name of the desired environment.
    """
    envs = [Environment.from_config(e) for e in environments]
    for env in envs:
        env.bootstrap()
    for env in envs:
        agent_version = env.get_matching_agent_version()
        status = env.get_status()
        for ignored in until_timeout(30):
            agent_versions = env.get_status().get_agent_versions()
            if 'unknown' not in agent_versions and len(agent_versions) == 1:
                break
            status = env.get_status()
        if agent_versions.keys() != [agent_version]:
            print("Current versions: %s" % ', '.join(agent_versions.keys()))
            env.juju('upgrade-juju', '--version', agent_version)
    if sys.platform == 'win32':
        # The win client tests only verify the client to the state-server.
        return
    for env in envs:
        env.wait_for_version(env.get_matching_agent_version())
        env.juju('deploy', charm_prefix + 'wordpress')
        env.juju('deploy', charm_prefix + 'mysql')
        env.juju('add-relation', 'mysql', 'wordpress')
        env.juju('expose', 'wordpress')
    for env in envs:
        status = env.wait_for_started().status
        wp_unit_0 = status['services']['wordpress']['units']['wordpress/0']
        check_wordpress(env.environment, wp_unit_0['public-address'])


def main():
    parser = ArgumentParser('Deploy a WordPress stack')
    parser.add_argument('--charm-prefix', help='A prefix for charm urls.',
                        default='')
    parser.add_argument('env', nargs='*')
    args = parser.parse_args()
    try:
        deploy_stack(args.env, args.charm_prefix)
    except Exception as e:
        print(e)
        sys.exit(1)


if __name__ == '__main__':
    main()
