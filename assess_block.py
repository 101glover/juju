#!/usr/bin/env python
"""Assess juju blocks prevent users from making changes and models"""

from __future__ import print_function

import argparse
import logging
import sys

import yaml

from assess_min_version import (
    JujuAssertionError
)
from deploy_stack import (
    BootstrapManager,
    deploy_dummy_stack,
)
from utility import (
    add_basic_testing_arguments,
    configure_logging,
    wait_for_removed_services,
)


__metaclass__ = type


log = logging.getLogger("assess_block")


def get_block_list(client):
    """Return a list of disabled commands and their status."""
    return yaml.safe_load(client.get_juju_output(
        'list-disabled-commands', '--format', 'yaml'))


class DisableCommandTypes:
    destroy_mode = 'destroy-model'
    remove_object = 'remove-object'
    all = 'all'


def make_block_list(disabled_commands):
    """Return a manually made list of blocks and their status

    :param disabled_commands: list of DisableCommandTypes elements to include
      in simulated output.

    """
    if not disabled_commands:
        return []

    return [{'command-set': ','.join(disabled_commands)}]


def test_disabled(client, command, args, include_e=True):
    """Test if a command is disabled as expected"""
    try:
        if command == 'deploy':
            client.deploy(args)
        elif command == 'remove-service':
            client.remove_service(args)
        else:
            client.juju(command, args, include_e=include_e)
        raise JujuAssertionError()
    except Exception:
        pass


def assess_block_destroy_model(client, charm_series):
    """Test disabling the destroy-model command.

    When "disable-command destroy-model" is set,
    the model cannot be destroyed, but objects
    can be added, related, and removed.
    """
    client.juju(client.disable_command, (client.destroy_model_command))
    block_list = get_block_list(client)
    if block_list != make_block_list([DisableCommandTypes.destroy_mode]):
        raise JujuAssertionError(block_list)
    test_disabled(
        client, client.destroy_model_command,
        ('-y', client.env.environment), include_e=False)
    # Adding, relating, and removing are not blocked.
    deploy_dummy_stack(client, charm_series)


def assess_block_remove_object(client, charm_series):
    """Test block remove-object

    When "disable-command remove-object" is set,
    objects can be added and related, but they
    cannot be removed or the model/environment deleted.
    """
    client.juju(client.disable_command, (DisableCommandTypes.remove_object))
    block_list = get_block_list(client)
    if block_list != make_block_list([DisableCommandTypes.remove_object]):
        raise JujuAssertionError(block_list)
    test_disabled(
        client, client.destroy_model_command,
        ('-y', client.env.environment), include_e=False)
    # Adding and relating are not blocked.
    deploy_dummy_stack(client, charm_series)
    test_disabled(client, 'remove-service', 'dummy-source')
    test_disabled(client, 'remove-unit', ('dummy-source/1',))
    test_disabled(client, 'remove-relation', ('dummy-source', 'dummy-sink'))


def assess_block_all_changes(client, charm_series):
    """Test Block Functionality: block all-changes"""
    client.juju('remove-relation', ('dummy-source', 'dummy-sink'))
    client.juju(client.disable_command, (DisableCommandTypes.all))
    block_list = get_block_list(client)
    if block_list != make_block_list([DisableCommandTypes.all]):
        raise JujuAssertionError(block_list)
    test_disabled(client, 'add-relation', ('dummy-source', 'dummy-sink'))
    test_disabled(client, 'unexpose', ('dummy-sink',))
    test_disabled(client, 'remove-service', 'dummy-sink')
    client.juju(client.enable_command, (DisableCommandTypes.all))
    client.juju('unexpose', ('dummy-sink',))
    client.juju(client.disable_command, (DisableCommandTypes.all))
    test_disabled(client, 'expose', ('dummy-sink',))
    client.juju(client.enable_command, (DisableCommandTypes.all))
    client.remove_service('dummy-sink')
    wait_for_removed_services(client, 'dummy-sink')
    client.juju(client.disable_command, (DisableCommandTypes.all))
    test_disabled(client, 'deploy', ('dummy-sink',))
    test_disabled(
        client, client.destroy_model_command,
        ('-y', client.env.environment), include_e=False)


def assess_unblock(client, type):
    """Test Block Functionality
    unblock destroy-model/remove-object/all-changes."""
    client.juju(client.enable_command, (type))
    block_list = get_block_list(client)
    if block_list != make_block_list([]):
        raise JujuAssertionError(block_list)
    if type == client.destroy_model_command:
        client.remove_service('dummy-source')
        wait_for_removed_services(client, 'dummy-source')
        client.remove_service('dummy-sink')
        wait_for_removed_services(client, 'dummy-sink')


def assess_block(client, charm_series):
    """Test Block Functionality:
    block/unblock destroy-model/remove-object/all-changes.
    """
    block_list = get_block_list(client)
    client.wait_for_started()
    expected_none_blocked = make_block_list([])
    if block_list != expected_none_blocked:
        raise JujuAssertionError(block_list)
    assess_block_destroy_model(client, charm_series)
    assess_unblock(client, client.destroy_model_command)
    assess_block_remove_object(client, charm_series)
    assess_unblock(client, DisableCommandTypes.remove_object)
    assess_block_all_changes(client, charm_series)
    assess_unblock(client, DisableCommandTypes.all)


def parse_args(argv):
    """Parse all arguments."""
    parser = argparse.ArgumentParser(description="Test Block Functionality")
    add_basic_testing_arguments(parser)
    parser.set_defaults(series='trusty')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)
    bs_manager = BootstrapManager.from_args(args)
    with bs_manager.booted_context(args.upload_tools):
        assess_block(bs_manager.client, bs_manager.series)
    return 0


if __name__ == '__main__':
    sys.exit(main())
