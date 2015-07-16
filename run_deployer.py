#!/usr/bin/env python
from argparse import ArgumentParser
import logging
import subprocess

from deploy_stack import (
    boot_context,
)
from jujupy import (
    EnvJujuClient,
    SimpleEnvironment,
)
from utility import (
    add_basic_testing_arguments,
    configure_logging,
)


def parse_args(argv=None):
    parser = ArgumentParser()
    parser.add_argument('bundle_path',
                        help='URL or path to a bundle')
    add_basic_testing_arguments(parser)
    parser.add_argument('--bundle-name', default=None,
                        help='Name of the bundle to deploy.')
    parser.add_argument('--health-cmd', default=None,
                        help='A binary for checking the health of the'
                        ' deployed bundle.')
    return parser.parse_args(argv)


def check_health(cmd_path, env_name=''):
    """Run the health checker command and raise on error."""
    try:
        cmd = (cmd_path, env_name)
        logging.debug('Calling {}'.format(cmd))
        sub_output = subprocess.check_output(cmd)
        logging.info('Health check output: {}'.format(sub_output))
    except OSError as e:
        logging.error(
            'Failed to execute {}: {}'.format(
                cmd, e))
        raise
    except subprocess.CalledProcessError as e:
        logging.error('Non-zero exit code returned from {}: {}'.format(
            cmd, e))
        logging.error(e.output)
        raise


def run_deployer():
    args = parse_args()
    configure_logging(args.verbose)
    env = SimpleEnvironment.from_config(args.env)
    client = EnvJujuClient.by_version(env, args.juju_bin, debug=args.debug)
    with boot_context(args.temp_env_name, client, None, [], args.series,
                      args.agent_url, args.agent_stream, args.logs,
                      args.keep_env, False):
        client.deployer(args.bundle_path, args.bundle_name)
        if args.health_cmd:
            check_health(args.health_cmd, args.temp_env_name)
if __name__ == '__main__':
    run_deployer()
