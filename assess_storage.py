#!/usr/bin/env python
"""Assess juju charm storage."""

from __future__ import print_function
import os

import argparse
import copy
import json
import logging
import sys

from deploy_stack import (
    BootstrapManager,
)
from jujucharm import (
    Charm,
    local_charm_path,
)
from utility import (
    add_basic_testing_arguments,
    configure_logging,
    JujuAssertionError,
    temp_dir,
)


__metaclass__ = type


log = logging.getLogger("assess_storage")


DEFAULT_STORAGE_POOL_DETAILS = {}


AWS_DEFAULT_STORAGE_POOL_DETAILS = {
    'ebs': {
        'provider': 'ebs'},
    'ebs-ssd': {
        'attrs': {
            'volume-type': 'ssd'},
        'provider': 'ebs'},
    'tmpfs': {
        'provider': 'tmpfs'},
    'loop': {
        'provider': 'loop'},
    'rootfs': {
        'provider': 'rootfs'}
}


storage_pool_details = {
    "loopy":
        {
            "provider": "loop",
            "attrs":
                {
                    "size": "1G"
                }},
    "rooty":
        {
            "provider": "rootfs",
            "attrs":
                {
                    "size": "1G"
                }},
    "tempy":
        {
            "provider": "tmpfs",
            "attrs":
                {
                    "size": "1G"
                }},
    "ebsy":
        {
            "provider": "ebs",
            "attrs":
                {
                    "size": "1G"
                }}
}

storage_pool_1x = copy.deepcopy(storage_pool_details)
storage_pool_1x["ebs-ssd"] = {
    "provider": "ebs",
    "attrs":
        {
            "volume-type": "ssd"
        }
}

storage_list_expected = {
    "storage":
        {"data/0":
            {
                "kind": "filesystem",
                "attachments":
                    {
                        "units":
                            {
                                "dummy-storage-fs/0":
                                    {"location": "/srv/data"}}}}}}

storage_list_expected_2 = copy.deepcopy(storage_list_expected)
storage_list_expected_2["storage"]["disks/1"] = {
    "kind": "block",
    "attachments":
        {
            "units":
                {"dummy-storage-lp/0":
                    {
                        "location": ""}}}}
storage_list_expected_3 = copy.deepcopy(storage_list_expected_2)
storage_list_expected_3["storage"]["disks/2"] = {
    "kind": "block",
    "attachments":
        {
            "units":
                {"dummy-storage-lp/0":
                    {
                        "location": ""}}}}
storage_list_expected_4 = copy.deepcopy(storage_list_expected_3)
storage_list_expected_4["storage"]["data/3"] = {
    "kind": "filesystem",
    "attachments":
        {
            "units":
                {"dummy-storage-tp/0":
                    {
                        "location": "/srv/data"}}}}
storage_list_expected_5 = copy.deepcopy(storage_list_expected_4)
storage_list_expected_5["storage"]["data/4"] = {
    "kind": "filesystem",
    "attachments":
        {
            "units":
                {"dummy-storage-np/0":
                    {
                        "location": "/srv/data"}}}}
storage_list_expected_6 = copy.deepcopy(storage_list_expected_5)
storage_list_expected_6["storage"]["data/5"] = {
    "kind": "filesystem",
    "attachments":
        {
            "units":
                {"dummy-storage-mp/0":
                    {
                        "location": "/srv/data"}}}}


def storage_list(client):
    """Return the storage list."""
    list_storage = json.loads(client.list_storage())
    for instance in list_storage["storage"].keys():
        try:
            list_storage["storage"][instance].pop("status")
            list_storage["storage"][instance].pop("persistent")
            attachments = list_storage["storage"][instance]["attachments"]
            unit = attachments["units"].keys()[0]
            attachments["units"][unit].pop("machine")
            if instance.startswith("disks"):
                attachments["units"][unit]["location"] = ""
        except Exception:
            pass
    return list_storage


def storage_pool_list(client):
    """Return the list of storage pool."""
    return json.loads(client.list_storage_pool())


def create_storage_charm(charm_dir, name, summary, storage):
    """Manually create a temporary charm to test with storage."""
    storage_charm = Charm(name, summary, storage=storage, series=['trusty'])
    charm_root = storage_charm.to_repo_dir(charm_dir)
    return charm_root


def assess_create_pool(client):
    """Test creating storage pool."""
    for name, pool in storage_pool_details.iteritems():
        client.create_storage_pool(name, pool["provider"],
                                   pool["attrs"]["size"])


def assess_add_storage(client, unit, storage_type, amount="1"):
    """Test adding storage instances to service.

    Only type 'disk' is able to add instances.
    """
    client.add_storage(unit, storage_type, amount)


def deploy_storage(client, charm, series, pool, amount="1G", charm_repo=None):
    """Test deploying charm with storage."""
    if pool == "loop":
        client.deploy(charm, series=series, repository=charm_repo,
                      storage="disks=" + pool + "," + amount)
    elif pool is None:
        client.deploy(charm, series=series, repository=charm_repo,
                      storage="data=" + amount)
    else:
        client.deploy(charm, series=series, repository=charm_repo,
                      storage="data=" + pool + "," + amount)
    client.wait_for_started()


def assess_deploy_storage(client, charm_series,
                          charm_name, provider_type, pool=None):
    """Set up the test for deploying charm with storage."""
    if provider_type == "block":
        storage = {
            "disks": {
                "type": provider_type,
                "multiple": {
                    "range": "0-10"
                }
            }
        }
    else:
        storage = {
            "data": {
                "type": provider_type,
                "location": "/srv/data"
            }
        }
    with temp_dir() as charm_dir:
        charm_root = create_storage_charm(charm_dir, charm_name,
                                          'Test charm for storage', storage)
        platform = 'ubuntu'
        charm = local_charm_path(charm=charm_name, juju_ver=client.version,
                                 series=charm_series,
                                 repository=os.path.dirname(charm_root),
                                 platform=platform)
        deploy_storage(client, charm, charm_series, pool, "1G", charm_dir)


def assess_multiple_provider(client, charm_series, amount, charm_name,
                             provider_1, provider_2, pool_1, pool_2):
    storage = {}
    for provider in [provider_1, provider_2]:
        if provider == "block":
            storage.update({
                "disks": {
                    "type": provider,
                    "multiple": {
                        "range": "0-10"
                    }
                }
            })
        else:
            storage.update({
                "data": {
                    "type": provider,
                    "location": "/srv/data"
                }
            })
    with temp_dir() as charm_dir:
        charm_root = create_storage_charm(charm_dir, charm_name,
                                          'Test charm for storage', storage)
        platform = 'ubuntu'
        charm = local_charm_path(charm=charm_name, juju_ver=client.version,
                                 series=charm_series,
                                 repository=os.path.dirname(charm_root),
                                 platform=platform)
        if pool_1 == "loop":
            command = "disks=" + pool_1 + "," + amount
        else:
            command = "data=" + pool_1 + "," + amount
        if pool_2 == "loop":
            command = command + ",disks=" + pool_2
        else:
            command = command + ",data=" + pool_2
        client.deploy(charm, series=charm_series, repository=charm_dir,
                      storage=command)
        client.wait_for_started()


def check_storage_list(client, expected):
    storage_list_derived = storage_list(client)
    if storage_list_derived != expected:
        raise JujuAssertionError(
            'Found: {} \nExpected: {}'.format(storage_list_derived, expected))


def assess_storage(client, charm_series):
    """Test the storage feature."""

    log.info('Assessing create-pool')
    assess_create_pool(client)
    log.info('create-pool PASSED')

    log.info('Assessing storage pool')
    if client.env.config['type'] == 'ec2':
        expected_pool = copy.deepcopy(AWS_DEFAULT_STORAGE_POOL_DETAILS)
    else:
        expected_pool = copy.deepcopy(DEFAULT_STORAGE_POOL_DETAILS)
    if client.version.startswith('1.'):
        expected_pool.update(storage_pool_1x)
    else:
        expected_pool.update(storage_pool_details)
    pool_list = storage_pool_list(client)
    if pool_list != expected_pool:
        raise JujuAssertionError(
            'Found: {} \nExpected: {}'.format(pool_list, expected_pool))
    log.info('Storage pool PASSED')

    log.info('Assessing filesystem rootfs')
    assess_deploy_storage(client, charm_series,
                          'dummy-storage-fs', 'filesystem', 'rootfs')
    check_storage_list(client, storage_list_expected)
    log.info('Filesystem rootfs PASSED')

    log.info('Assessing block loop')
    assess_deploy_storage(client, charm_series,
                          'dummy-storage-lp', 'block', 'loop')
    check_storage_list(client, storage_list_expected_2)
    log.info('Block loop PASSED')

    log.info('Assessing disk 1')
    assess_add_storage(client, 'dummy-storage-lp/0', 'disks', "1")
    check_storage_list(client, storage_list_expected_3)
    log.info('Disk 1 PASSED')

    log.info('Assessing filesystem tmpfs')
    assess_deploy_storage(client, charm_series,
                          'dummy-storage-tp', 'filesystem', 'tmpfs')
    check_storage_list(client, storage_list_expected_4)
    log.info('Filesystem tmpfs PASSED')

    log.info('Assessing filesystem')
    assess_deploy_storage(client, charm_series,
                          'dummy-storage-np', 'filesystem')
    check_storage_list(client, storage_list_expected_5)
    log.info('Filesystem tmpfs PASSED')

    log.info('Assessing multiple filesystem, block, rootfs, loop')
    assess_multiple_provider(client, charm_series, "1G", 'dummy-storage-mp',
                             'filesystem', 'block', 'rootfs', 'loop')
    check_storage_list(client, storage_list_expected_6)
    log.info('Multiple filesystem, block, rootfs, loop PASSED')
    # storage with provider 'ebs' is only available under 'aws'
    # assess_deploy_storage(client, charm_series,
    #                       'dummy-storage-eb', 'filesystem', 'ebs')


def parse_args(argv):
    """Parse all arguments."""
    parser = argparse.ArgumentParser(description="Test Storage Feature")
    add_basic_testing_arguments(parser)
    parser.set_defaults(series='trusty')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)
    bs_manager = BootstrapManager.from_args(args)
    with bs_manager.booted_context(args.upload_tools):
        assess_storage(bs_manager.client, bs_manager.series)
    return 0


if __name__ == '__main__':
    sys.exit(main())
