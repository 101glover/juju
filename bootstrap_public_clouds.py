#!/usr/bin/env python
from argparse import ArgumentParser
import logging
import os
import sys

import yaml

from deploy_stack import (
    BootstrapManager,
    )
from jujuconfig import get_juju_home
from jujupy import (
    client_from_config,
    )
from utility import (
    _clean_dir,
    configure_logging,
    _generate_default_binary,
    _to_deadline,
    )


log = logging.getLogger('bootstrap_public_clouds')


def make_logging_dir(base_dir, config, region):
    log_dir = os.path.join(base_dir, config, region)
    os.mkdirs(log_dir)
    return log_dir


def bootstrap_cloud(config, region, args):
    client = client_from_config(
        config, args.juju_bin, args.debug, args.deadline)
    env_name = 'boot-cpc-{}-{}'.format(client.env.get_cloud(), region)[:30]
    client.env.environment = env_name
    client.env.controller.name = env_name
    client.env.set_region(region)
    logging_dir = make_logging_dir(args.logs, config, region)
    bs_manager = BootstrapManager(
        env_name, client, client, None, [], None, None, None, None,
        logging_dir, False, False, False)
    with bs_manager.booted_context(False):
        client.wait_for_started()


def iter_cloud_regions(public_clouds, credentials):
    configs = {
        'aws': 'default-aws',
        # sinzui: We may lose this access. No one remaining at Canonical can
        # access the account. There is talk of terminating it.
        'aws-china': 'default-aws-cn',
        'azure': 'default-azure',
        'google': 'default-gce',
        'joyent': 'default-joyent',
        'rackspace': 'default-rackspace',
        }
    for cloud, info in sorted(public_clouds.items()):
        if cloud not in credentials:
            logging.warning('No credentials for {}.  Skipping.'.format(cloud))
            continue
        config = configs[cloud]
        for region in sorted(info['regions']):
            yield config, region


def bootstrap_cloud_regions(public_clouds, credentials, args):
    cloud_regions = list(iter_cloud_regions(public_clouds, credentials))
    for num, (config, region) in enumerate(cloud_regions):
        if num < args.start:
            continue
        logging.info('Bootstrapping {} {} #{}'.format(config, region, num))
        try:
            bootstrap_cloud(config, region, args)
        except Exception as e:
            yield(config, region, e)


def parse_args(argv):
    """Parse all arguments."""
    parser = ArgumentParser(
        description="This is a quick hack to test 0052b26.  HERE BE DRAGONS!")
    parser.add_argument('juju_bin', nargs='?',
                        help='Full path to the Juju binary. By default, this'
                        ' will use $GOPATH/bin/juju or /usr/bin/juju in that'
                        ' order.', default=_generate_default_binary())
    parser.add_argument('logs', nargs='?', type=_clean_dir,
                        help='A directory in which to store logs. By default,'
                        ' this will use the current directory', default=None)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--debug', action='store_true', default=False,
                        help='Pass --debug to Juju.')
    parser.add_argument('--timeout', dest='deadline', type=_to_deadline,
                        help="The script timeout, in seconds.")
    return parser.parse_args(argv)


def yaml_file_load(file_name):
    with open(os.path.join(get_juju_home(), file_name)) as file:
        yaml_data = yaml.safe_load(file)
    return yaml_data


def default_log_dir(settings):
    if settings.logs is None:
        settings.logs = BootstrapManager._generate_default_clean_dir(
            'bootstrap_public_clouds')


def main():
    configure_logging(logging.INFO)
    args = parse_args(None)
    default_log_dir(args)
    logging.warning('This is a quick hack to test 0052b26.  HERE BE DRAGONS!')
    public_clouds = yaml_file_load('public-clouds.yaml')['clouds']
    credentials = yaml_file_load('credentials.yaml')['credentials']
    failures = []
    try:
        for failure in bootstrap_cloud_regions(public_clouds, credentials,
                                               args):
            failures.append(failure)
    finally:
        if len(failures) == 0:
            print('No failures!')
            return 0
        else:
            print('Failed:')
            for config, region, e in failures:
                print(' * {} {} {}'.format(config, region, e))
            return 1


if __name__ == '__main__':
    sys.exit(main())
