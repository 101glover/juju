#!/usr/bin/env python
# Backup and restore a stack.

from __future__ import print_function

from argparse import ArgumentParser
from contextlib import contextmanager
import logging
import re
from subprocess import CalledProcessError
import sys

from deploy_stack import (
    BootstrapManager,
    deploy_dummy_stack,
    get_token_from_status,
    wait_for_state_server_to_shutdown,
)
from jujupy import (
    parse_new_state_server_from_error,
)
from substrate import (
    convert_to_azure_ids,
    terminate_instances,
)
from utility import (
    add_basic_testing_arguments,
    configure_logging,
    JujuAssertionError,
    LoggedException,
)


__metaclass__ = type


running_instance_pattern = re.compile('\["([^"]+)"\]')


log = logging.getLogger("assess_recovery")


def check_token(client, token):
    found = get_token_from_status(client)
    if token not in found:
        raise JujuAssertionError('Token is not {}: {}'.format(
            token, found))


def deploy_stack(client, charm_series):
    """"Deploy a simple stack, state-server and ubuntu."""
    deploy_dummy_stack(client, charm_series)
    client.set_config('dummy-source', {'token': 'One'})
    client.wait_for_workloads()
    check_token(client, 'One')
    log.info("%s is ready to testing", client.env.environment)


def show_controller(client):
    controller_info = client.show_controller(format='yaml')
    log.info('Controller is:\n{}'.format(controller_info))


def restore_present_state_server(controller_client, backup_file):
    """juju-restore won't restore when the state-server is still present."""
    try:
        controller_client.restore_backup(backup_file)
    except CalledProcessError:
        log.info(
            "juju-restore correctly refused to restore "
            "because the state-server was still up.")
        return
    else:
        raise Exception(
            "juju-restore restored to an operational state-serve")


def delete_controller_members(client, leader_only=False):
    """Delete controller members.

    The all members are delete by default. The followers are deleted before the
    leader to simulates a total controller failure. When leader_only is true,
    the leader is deleted to trigger a new leader election.
    """
    if leader_only:
        leader = client.get_controller_leader()
        members = [leader]
    else:
        members = client.get_controller_members()
        members.reverse()
    deleted_machines = []
    for machine in members:
        instance_id = machine.info.get('instance-id')
        if client.env.config['type'] == 'azure':
            instance_id = convert_to_azure_ids(client, [instance_id])[0]
        host = machine.info.get('dns-name')
        log.info("Instrumenting node failure for member {}: {} at {}".format(
                 machine.machine_id, instance_id, host))
        terminate_instances(client.env, [instance_id])
        wait_for_state_server_to_shutdown(
            host, client, instance_id, timeout=120)
        deleted_machines.append(machine.machine_id)
    return deleted_machines


def restore_missing_state_server(client, controller_client, backup_file,
                                 check_controller=True):
    """juju-restore creates a replacement state-server for the services."""
    log.info("Starting restore.")
    try:
        controller_client.restore_backup(backup_file)
    except CalledProcessError as e:
        log.info('Call of juju restore exited with an error\n')
        log.info('Call:  %r\n', e.cmd)
        log.exception(e)
        raise LoggedException(e)
    if check_controller:
        controller_client.wait_for_started(600)
    show_controller(client)
    client.set_config('dummy-source', {'token': 'Two'})
    client.wait_for_started()
    client.wait_for_workloads()
    check_token(client, 'Two')
    log.info("%s restored", client.env.environment)
    log.info("PASS")


def parse_args(argv=None):
    parser = ArgumentParser(description='Test recovery strategies.')
    add_basic_testing_arguments(parser)
    parser.add_argument(
        '--charm-series', help='Charm series.', default='')
    strategy = parser.add_argument_group('test strategy')
    strategy.add_argument(
        '--ha', action='store_const', dest='strategy', const='ha',
        default='backup', help="Test HA.")
    strategy.add_argument(
        '--backup', action='store_const', dest='strategy', const='backup',
        help="Test backup/restore.")
    strategy.add_argument(
        '--ha-backup', action='store_const', dest='strategy',
        const='ha-backup', help="Test backup/restore of HA.")
    return parser.parse_args(argv)


@contextmanager
def detect_bootstrap_machine(bs_manager):
    try:
        yield
    except Exception as e:
        bs_manager.known_hosts['0'] = parse_new_state_server_from_error(e)
        raise


def assess_recovery(bs_manager, strategy, charm_series):
    log.info("Setting up test.")
    client = bs_manager.client
    deploy_stack(client, charm_series)
    log.info("Setup complete.")
    log.info("Test started.")
    controller_client = client.get_controller_client()
    if strategy in ('ha', 'ha-backup'):
        controller_client.enable_ha()
        controller_client.wait_for_ha()
    if strategy in ('ha-backup', 'backup'):
        backup_file = controller_client.backup()
        restore_present_state_server(controller_client, backup_file)
    if strategy == 'ha':
        leader_only = True
    else:
        leader_only = False
    deleted_machine_ids = delete_controller_members(
        controller_client, leader_only=leader_only)
    log.info("Deleted {}".format(deleted_machine_ids))
    for m_id in deleted_machine_ids:
        if bs_manager.known_hosts.get(m_id):
            del bs_manager.known_hosts[m_id]
    if strategy == 'ha':
        client.get_status(600)
        log.info("HA recovered from leader failure.")
        log.info("PASS")
    else:
        check_controller = strategy != 'ha-backup'
        restore_missing_state_server(client, controller_client, backup_file,
                                     check_controller=check_controller)
    log.info("Test complete.")


def main(argv):
    args = parse_args(argv)
    configure_logging(args.verbose)
    bs_manager = BootstrapManager.from_args(args)
    with bs_manager.booted_context(upload_tools=args.upload_tools):
        with detect_bootstrap_machine(bs_manager):
            assess_recovery(bs_manager, args.strategy, args.charm_series)


if __name__ == '__main__':
    main(sys.argv[1:])
