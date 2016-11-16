#!/usr/bin/env python

from argparse import ArgumentParser
from copy import deepcopy
import logging
import sys

import yaml

from jujupy import (
    EnvJujuClient,
    get_client_class,
    JujuData,
    )
from utility import (
    add_arg_juju_bin,
    JujuAssertionError,
    temp_dir,
    )


def assess_cloud(client, cloud_name, example_cloud):
    clouds = client.env.read_clouds()
    if len(clouds['clouds']) > 0:
        raise AssertionError('Clouds already present!')
    client.add_cloud_interactive(cloud_name, example_cloud)
    clouds = client.env.read_clouds()
    if len(clouds['clouds']) == 0:
        raise JujuAssertionError('Clouds missing!')
    if clouds['clouds'].keys() != [cloud_name]:
        raise JujuAssertionError('Name mismatch')
    if clouds['clouds'][cloud_name] != example_cloud:
        sys.stderr.write('\nExpected:\n')
        yaml.dump(example_cloud, sys.stderr)
        sys.stderr.write('\nActual:\n')
        yaml.dump(clouds['clouds'][cloud_name], sys.stderr)
        raise JujuAssertionError('Cloud mismatch')


def iter_clouds(clouds):
    for cloud_name, cloud in clouds.items():
        yield cloud_name, cloud_name, cloud

    for cloud_name, cloud in clouds.items():
        yield 'long-name-{}'.format(cloud_name), 'A' * 4096, cloud

        if 'endpoint' in cloud:
            variant = deepcopy(cloud)
            variant['endpoint'] = 'A' * 4096
            if variant['type'] == 'vsphere':
                for region in variant['regions'].values():
                    region['endpoint'] = variant['endpoint']
            variant_name = 'long-endpoint-{}'.format(cloud_name)
            yield variant_name, cloud_name, variant

        for region_name in variant.get('regions', {}).keys():
            if variant['type'] != 'vsphere':
                variant = deepcopy(cloud)
                region = variant['regions'][region_name]
                region['endpoint'] = 'A' * 4096
                variant_name = 'long-endpoint{}-{}'.format(cloud_name,
                                                           region_name)
                yield variant_name, cloud_name, variant


def assess_all_clouds(client, clouds):
    succeeded = set()
    failed = set()
    client.env.load_yaml()
    for cloud_label, cloud_name, cloud in iter_clouds(clouds):
        sys.stdout.write('Testing {}.\n'.format(cloud_label))
        try:
            assess_cloud(client, cloud_name, cloud)
        except Exception as e:
            logging.exception(e)
            failed.add(cloud_label)
        else:
            succeeded.add(cloud_label)
        finally:
            client.env.clouds = {'clouds': {}}
            client.env.dump_yaml(client.env.juju_home, {})
    return succeeded, failed


def write_status(status, tests):
    if len(tests) == 0:
        test_str = 'none'
    else:
        test_str = ', '.join(sorted(tests))
    sys.stdout.write('{}: {}\n'.format(status, test_str))


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('example_clouds',
                        help='A clouds.yaml file to use for testing.')
    add_arg_juju_bin(parser)
    return parser.parse_args()


def main():
    args = parse_args()
    juju_bin = args.juju_bin
    version = EnvJujuClient.get_version(juju_bin)
    client_class = get_client_class(version)
    if client_class.config_class is not JujuData:
        logging.warn('This test does not support old jujus.')
    with open(args.example_clouds) as f:
        clouds = yaml.safe_load(f)['clouds']
    with temp_dir() as juju_home:
        env = JujuData('foo', config=None, juju_home=juju_home)
        client = client_class(env, version, juju_bin)
        succeeded, failed = assess_all_clouds(client, clouds)
    write_status('Succeeded', succeeded)
    write_status('Failed', failed)


if __name__ == '__main__':
    main()
