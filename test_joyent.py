from datetime import (
    datetime,
    timedelta,
)
import json
from mock import patch
from textwrap import dedent
from unittest import TestCase

from joyent import (
    Client,
    ISO_8601_FORMAT,
    parse_args,
)


def make_machine(state='running', hours=2):
    then = datetime.utcnow() - timedelta(hours=hours)
    return {
        'id': 'id',
        'state': state,
        'created': then.strftime(ISO_8601_FORMAT)}


def fake_list_machines(machine):
    def list_machines(machine_id=None):
        if machine_id:
            return machine
        else:
            return [machine]
    return list_machines


class JoyentTestCase(TestCase):

    def test_parse_args(self):
        args = parse_args(
            ['-d', '-v', '-u', 'sdc_url', '-a', 'account', '-k', 'key_id',
             '-p', 'key/path', 'list-machines'])
        self.assertEqual('sdc_url', args.sdc_url)
        self.assertEqual('account', args.account)
        self.assertEqual('key_id', args.key_id)
        self.assertEqual('key/path', args.key_path)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.verbose)


class ClientTestCase(TestCase):

    def test_init(self):
        client = Client(
            'sdc_url', 'account', 'key_id', './key', 'manta_url',
            dry_run=True, verbose=True)
        self.assertEqual('sdc_url', client.sdc_url)
        self.assertEqual('account', client.account)
        self.assertEqual('key_id', client.key_id)
        self.assertEqual('./key', client.key_path)
        self.assertEqual(3, client.pause)
        self.assertTrue(client.dry_run)
        self.assertTrue(client.verbose)

    def test_list_machine_tags(self):
        client = Client(
            'sdc_url', 'account', 'key_id', './key', 'manta_url', pause=0)
        headers = {}
        content = json.dumps({'env': 'foo'})
        with patch.object(client, '_request', autospec=True,
                          return_value=(headers, content)) as mock:
            tags = client._list_machine_tags('bar')
        mock.assert_called_once_with('/machines/bar/tags')
        self.assertEqual({'env': 'foo'}, tags)

    def test_kill_old_procs(self):
        return_values = [dedent("""\
            123    45:30 /test.py -e joyent ./juju
            456 03:45:30 /test.py -e joyent ./juju
            789 13:45:30 /test.py -e joyent ./juju
            """), '']
        client = Client(
            'sdc_url', 'account', 'key_id', './key', 'manta_url', pause=0)
        with patch('subprocess.check_output',
                   side_effect=return_values) as co_mock:
            old_procs = client._kill_old_procs(12)
        self.assertEqual(
            [('789', '13:45:30', ['/test.py', '-e', 'joyent', './juju'])],
            old_procs)
        co_mock.assert_call_any(
            'bash', '-c',
            'ps ax -eo pid,etime,command | grep joyent | grep juju')
        co_mock.assert_call_any('kill', '-9', '789')
        self.assertEqual(2, co_mock.call_count)

    def test_delete_old_machines(self):
        machine = make_machine('stopped')
        client = Client(
            'sdc_url', 'account', 'key_id', './key', 'manta_url', pause=0)
        with patch.object(client, '_list_machines',
                          side_effect=fake_list_machines(machine)) as lm_mock:
            with patch.object(client, '_list_machine_tags', autospec=True,
                              return_value={}) as lmt_mock:
                with patch.object(client, '_delete_running_machine',
                                  autospec=True) as drm_mock:
                    with patch.object(client, 'request_deletion',
                                      autospec=True) as rd_mock:
                        with patch.object(client, '_kill_old_procs',
                                          autospec=True) as kp_mock:
                            client.delete_old_machines(1, 'foo@bar')
        lm_mock.assert_call_any(None)
        lm_mock.assert_call_any('id')
        lmt_mock.assert_called_once_with('id')
        drm_mock.assert_called_once_with('id')
        self.assertEqual(0, rd_mock.call_count)
        kp_mock.assert_called_once_with(1)

    def test_delete_old_machines_stuck_provisioning(self):
        machine = make_machine('provisioning')
        client = Client(
            'sdc_url', 'account', 'key_id', './key', 'manta_url', pause=0)
        with patch.object(client, '_list_machines', autospec=True,
                          side_effect=fake_list_machines(machine)):
            with patch.object(client, '_list_machine_tags', autospec=True):
                with patch.object(client, '_delete_running_machine',
                                  autospec=True) as drm_mock:
                    with patch.object(client, 'request_deletion',
                                      autospec=True) as rd_mock:
                        client.delete_old_machines(1, 'foo@bar')
        self.assertEqual(0, drm_mock.call_count)
        rd_mock.assert_called_once_with([machine], 'foo@bar')

    def test_delete_old_machines_permanent(self):
        machine = make_machine('provisioning')
        client = Client(
            'sdc_url', 'account', 'key_id', './key', 'manta_url', pause=0)
        with patch.object(client, '_list_machines', autospec=True,
                          side_effect=fake_list_machines(machine)):
            with patch.object(client, '_list_machine_tags', autospec=True,
                              return_value={'permanent': 'true'}) as lmt_mock:
                with patch.object(client,  '_delete_running_machine',
                                  autospec=True) as drm_mock:
                    with patch.object(client, 'request_deletion',
                                      autospec=True) as rd_mock:
                        client.delete_old_machines(1, 'foo@bar')
        lmt_mock.assert_called_once_with('id')
        self.assertEqual(0, drm_mock.call_count)
        self.assertEqual(0, rd_mock.call_count)
