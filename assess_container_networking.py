#!/usr/bin/env python
from __future__ import print_function
import subprocess
from copy import (
    copy,
    deepcopy,
)
import logging
import time
import re
import tempfile
import os
import socket
from textwrap import dedent
from argparse import ArgumentParser

from jujuconfig import (
    get_juju_home,
)
from jujupy import (
    parse_new_state_server_from_error,
    temp_bootstrap_env,
    SimpleEnvironment,
    EnvJujuClient,
)
from utility import (
    print_now,
    add_basic_testing_arguments,
)
from deploy_stack import (
    update_env,
    dump_env_logs,
)

KVM_MACHINE = 'kvm'
LXC_MACHINE = 'lxc'

__metaclass__ = type


def parse_args(argv=None):
    """Parse all arguments."""

    description = dedent("""\
    Test container address allocation.
    For LXC and KVM, create machines of each type and test the network
    between LXC <--> LXC, KVM <--> KVM and LXC <--> KVM. Also test machine
    to outside world, DNS and that these tests still pass after a reboot. In
    case of failure pull logs and configuration files from the machine that
    we detected a problem on for later analysis.
    """)
    parser = add_basic_testing_arguments(ArgumentParser(
        description=description
    ))
    parser.add_argument(
        '--machine-type',
        help='Which virtual machine/container type to test. Defaults to all.',
        choices=[KVM_MACHINE, LXC_MACHINE])
    parser.add_argument(
        '--clean-environment', action='store_true', help=dedent("""\
        Attempts to re-use an existing environment rather than destroying it
        and creating a new one.

        On launch, if an environment exists, clean out services and machines
        from it rather than destroying it. If an environment doesn't exist,
        create one and use it.

        At termination, clean out services and machines from the environment
        rather than destroying it."""))
    return parser.parse_args(argv)


def ssh(client, machine, cmd):
    """Convenience function: run a juju ssh command and get back the output
    :param client: A Juju client
    :param machine: ID of the machine on which to run a command
    :param cmd: the command to run
    :return: text output of the command
    """
    try:
        return client.get_juju_output('ssh', machine, cmd)
    except subprocess.CalledProcessError as e:
        # If the connection to the host failed, try again in a couple of
        # seconds. This is usually due to heavy load.
        if e.returncode == 255:
            if re.search('ssh_exchange_identification: '
                         'Connection closed by remote host', e.stderr):
                time.sleep(2)
                return client.get_juju_output('ssh', machine, cmd)
        raise


def clean_environment(client, services_only=False):
    """Remove all the services and, optionally, machines from an environment.

    Use as an alternative to destroying an environment and creating a new one
    to save a bit of time.

    :param client: a Juju client
    """
    try:
        client.get_juju_output('status')
    except subprocess.CalledProcessError:
        # If we fail to get status then the environment doesn't exist. Since
        # there is nothing to clean, we return False to say that we failed.
        return False

    status = client.get_status()
    for service in status.status['services']:
        client.juju('remove-service', service)

    if not services_only:
        # First remove all containers; we can't remove a machine that is
        # hosting containers.
        for m, _ in status.iter_machines(containers=True, machines=False):
            client.juju('remove-machine', m)

        client.wait_for('containers', 'none')

        for m, _ in status.iter_machines(containers=False, machines=True):
            if m != '0':
                client.juju('remove-machine', m)

        client.wait_for('machines-not-0', 'none')

    client.wait_for_started()
    return True


def make_machines(client, container_types):
    """Make a test environment consisting of:
       Two host machines.
       Two of each container_type on one host machine.
       One of each container_type on one host machine.
    :param client: An EnvJujuClient
    :param container_types: list of containers to create
    :return: hosts (list), containers {container_type}{host}[containers]
    """
    # Find existing host machines
    old_hosts = client.get_status().status['machines']
    machines_to_add = 2 - len(old_hosts)

    # Allocate more hosts as needed
    if machines_to_add > 0:
        client.juju('add-machine', ('-n', str(machines_to_add)))
    status = client.wait_for_started()
    hosts = sorted(status.status['machines'].keys())[:2]

    # Find existing containers
    required = dict(zip(hosts, [copy(container_types) for h in hosts]))
    required[hosts[0]] += container_types
    for c in status.iter_machines(containers=True, machines=False):
        host, type, id = c[0].split('/')
        if type in required[host]:
            required[host].remove(type)

    # Start any new containers we need
    for host, containers in required.iteritems():
        for container in containers:
            client.juju('add-machine', ('{}:{}'.format(container, host)))

    status = client.wait_for_started()

    # Build a list of containers, now they have all started
    tmp = dict(zip(hosts, [[] for h in hosts]))
    containers = dict(zip(container_types,
                          [deepcopy(tmp) for t in container_types]))
    for c in status.iter_machines(containers=True, machines=False):
        host, type, id = c[0].split('/')
        if type in containers and host in containers[type]:
            containers[type][host].append(c[0])

    return hosts, containers


def find_network(client, machine, addr):
    """Find a connected subnet containing the given address.

    When using this to find the subnet of a container, don't use the container
    as the machine to run the ip route show command on ("machine"), use a real
    box because lxc will just send everything to its host machine, so it is on
    a subnet containing itself. Not much use.
    :param client: A Juju client
    :param machine: ID of the machine on which to run a command
    :param addr: find the connected subnet containing this address
    :return: CIDR containing the address if found, else, None
    """
    ip_cmd = ' '.join(['ip', 'route', 'show', 'to', 'match', addr])
    routes = ssh(client, machine, ip_cmd)

    for route in re.findall(r'^(\S+).*[\d\.]+/\d+', routes, re.MULTILINE):
        if route != 'default':
            return route

    raise ValueError("Unable to find route to %r" % addr)


def assess_network_traffic(client, targets):
    """Test that all containers in target can talk to target[0]
    :param client: Juju client
    :param targets: machine IDs of machines to test
    :return: None;
    """
    status = client.wait_for_started().status
    source = targets[0]
    dests = targets[1:]

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write('tmux new-session -d -s test "nc -l 6778 > nc_listen.out"')
    client.juju('scp', (f.name, source + ':/home/ubuntu/listen.sh'))
    os.remove(f.name)

    # Containers are named 'x/type/y' where x is the host of the container. We
    host = source.split('/')[0]
    address = status['machines'][host]['containers'][source]['dns-name']

    for dest in dests:
        ssh(client, source, 'rm nc_listen.out; bash ./listen.sh')
        ssh(client, dest, 'echo "hello" | nc ' + address + ' 6778')
        result = ssh(client, source, 'more nc_listen.out')
        if result.rstrip() != 'hello':
            raise ValueError("Wrong or missing message: %r" % result.rstrip())


def assess_address_range(client, targets):
    """Test that two containers are in the same subnet as their host
    :param client: Juju client
    :param targets: machine IDs of machines to test
    :return: None; will assert failures
    """
    status = client.wait_for_started().status

    host = targets[0].split('/')[0]
    host_address = socket.gethostbyname(status['machines'][host]['dns-name'])
    host_subnet = find_network(client, host, host_address)

    for target in targets:
        vm_host = target.split('/')[0]
        addr = status['machines'][vm_host]['containers'][target]['dns-name']
        subnet = find_network(client, host, addr)
        assert host_subnet == subnet, \
            '{} ({}) not on the same subnet as {} ({})'.format(
                target, subnet, host, host_subnet)


def assess_internet_connection(client, targets):
    """Test that targets can ping Google's DNS server, google.com
    :param client: Juju client
    :param targets: machine IDs of machines to test
    :return: None; will assert failures
    """

    for target in targets:
        routes = ssh(client, target, 'ip route show')

        d = re.search(r'^default\s+via\s+([\d\.]+)\s+', routes, re.MULTILINE)
        if d:
            rc = client.juju('ssh', (target, 'ping -c1 -q ' + d.group(1)),
                             check=False)
            if rc != 0:
                raise ValueError('%s unable to ping default route' % target)
        else:
            raise ValueError("Default route not found")


def _assessment_iteration(client, containers):
    """Run the network tests on this collection of machines and containers
    :param client: Juju client
    :param hosts: list of hosts of containers
    :param containers: list of containers to run tests between
    :return: None
    """
    assess_internet_connection(client, containers)
    assess_address_range(client, containers)
    assess_network_traffic(client, containers)


def _assess_container_networking(client, types, hosts, containers):
    """Run _assessment_iteration on all useful combinations of containers
    :param client: Juju client
    :param args: Parsed command line arguments
    :return: None
    """
    for container_type in types:
        # Test with two containers on the same host
        _assessment_iteration(client, containers[container_type][hosts[0]])

        # Now test with two containers on two different hosts
        test_containers = [
            containers[container_type][hosts[0]][0],
            containers[container_type][hosts[1]][0],
            ]
        _assessment_iteration(client, test_containers)

    if KVM_MACHINE in types and LXC_MACHINE in types:
        test_containers = [
            containers[LXC_MACHINE][hosts[0]][0],
            containers[KVM_MACHINE][hosts[0]][0],
            ]
        _assessment_iteration(client, test_containers)

        # Test with an LXC and a KVM on different machines
        test_containers = [
            containers[LXC_MACHINE][hosts[0]][0],
            containers[KVM_MACHINE][hosts[1]][0],
            ]
        _assessment_iteration(client, test_containers)


def assess_container_networking(client, args):
    """Runs _assess_address_allocation, reboots hosts, repeat.
    :param client: Juju client
    :param args: Parsed command line arguments
    :return: None
    """
    # Only test the containers we were asked to test
    if args.machine_type:
        types = [args.machine_type]
    else:
        types = [KVM_MACHINE, LXC_MACHINE]

    hosts, containers = make_machines(client, types)
    _assess_container_networking(client, types, hosts, containers)
    for host in hosts:
        ssh(client, host, 'sudo reboot')
    client.wait_for_started()
    _assess_container_networking(client, types, hosts, containers)


def get_client(args):
    client = EnvJujuClient.by_version(
        SimpleEnvironment.from_config(args.env),
        os.path.join(args.juju_bin, 'juju'), args.debug)
    client.enable_container_address_allocation()
    update_env(client.env, args.temp_env_name)
    return client


def main():
    args = parse_args()
    client = get_client(args)
    juju_home = get_juju_home()
    bootstrap_host = None
    try:
        if args.clean_environment:
            try:
                if not clean_environment(client, services_only=True):
                    with temp_bootstrap_env(juju_home, client):
                        client.bootstrap(args.upload_tools)
            except Exception as e:
                logging.exception(e)
                with temp_bootstrap_env(juju_home, client):
                    client.bootstrap(args.upload_tools)
        else:
            client.destroy_environment()
            client = get_client(args)
            with temp_bootstrap_env(juju_home, client):
                client.bootstrap(args.upload_tools)

        logging.info('Waiting for the bootstrap machine agent to start.')
        status = client.wait_for_started()
        mid, data = list(status.iter_machines())[0]
        bootstrap_host = data['dns-name']

        assess_container_networking(client, args)

    except Exception as e:
        logging.exception(e)
        try:
            if bootstrap_host is None:
                parse_new_state_server_from_error(e)
            else:
                dump_env_logs(client, bootstrap_host, args.logs)
        except Exception as e:
            print_now('exception while dumping logs:\n')
            logging.exception(e)
        exit(1)
    finally:
        if args.clean_environment:
            clean_environment(client, services_only=True)
        else:
            client.destroy_environment()


if __name__ == '__main__':
    main()
