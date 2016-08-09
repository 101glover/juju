"""Tests for assess_storage module."""

import logging
import StringIO

from mock import Mock, patch, call
import json

from assess_storage import (
    assess_storage,
    parse_args,
    main,
    storage_list_expected,
    storage_pool_1x,
    storage_list_expected_2,
    storage_list_expected_3,
    storage_list_expected_4,
    storage_list_expected_5,
    storage_list_expected_6,
    storage_pool_details,
)
from tests import (
    parse_error,
    TestCase,
)


class TestParseArgs(TestCase):

    def test_common_args(self):
        args = parse_args(["an-env", "/bin/juju", "/tmp/logs", "an-env-mod"])
        self.assertEqual("an-env", args.env)
        self.assertEqual("/bin/juju", args.juju_bin)
        self.assertEqual("/tmp/logs", args.logs)
        self.assertEqual("an-env-mod", args.temp_env_name)
        self.assertEqual(False, args.debug)
        self.assertEqual('trusty', args.series)

    def test_help(self):
        fake_stdout = StringIO.StringIO()
        with parse_error(self) as fake_stderr:
            with patch("sys.stdout", fake_stdout):
                parse_args(["--help"])
        self.assertEqual("", fake_stderr.getvalue())


class TestMain(TestCase):

    def test_main(self):
        argv = ["an-env", "/bin/juju", "/tmp/logs", "an-env-mod", "--verbose"]
        client = Mock(spec=["is_jes_enabled"])
        with patch("assess_storage.configure_logging",
                   autospec=True) as mock_cl:
            with patch("assess_storage.BootstrapManager.booted_context",
                       autospec=True) as mock_bc:
                with patch("deploy_stack.client_from_config",
                           return_value=client) as mock_c:
                    with patch("assess_storage.assess_storage",
                               autospec=True) as mock_assess:
                        main(argv)
        mock_cl.assert_called_once_with(logging.DEBUG)
        mock_c.assert_called_once_with('an-env', "/bin/juju", debug=False)
        self.assertEqual(mock_bc.call_count, 1)
        mock_assess.assert_called_once_with(client, 'trusty')


class TestAssess(TestCase):

    def test_storage_1x(self):
        mock_client = Mock(spec=["juju", "wait_for_started",
                                 "create_storage_pool",
                                 "list_storage_pool", "deploy",
                                 "get_juju_output", "add_storage",
                                 "list_storage"])
        mock_client.series = 'trusty'
        mock_client.version = '1.25'
        mock_client.list_storage_pool.side_effect = [
            json.dumps(storage_pool_1x)
        ]
        mock_client.list_storage.side_effect = [
            json.dumps(storage_list_expected),
            json.dumps(storage_list_expected_2),
            json.dumps(storage_list_expected_3),
            json.dumps(storage_list_expected_4),
            json.dumps(storage_list_expected_5),
            json.dumps(storage_list_expected_6)
        ]
        assess_storage(mock_client, mock_client.series)
        self.assertEqual(
            [
                call('ebsy', 'ebs', '1G'),
                call('loopy', 'loop', '1G'),
                call('rooty', 'rootfs', '1G'),
                call('tempy', 'tmpfs', '1G')
            ],
            mock_client.create_storage_pool.mock_calls)
        self.assertEqual(
            [
                call('dummy-storage-lp/0', 'disks', '1')
            ],
            mock_client.add_storage.mock_calls
        )

    def test_storage_2x(self):
        mock_client = Mock(spec=["juju", "wait_for_started",
                                 "create_storage_pool",
                                 "list_storage_pool", "deploy",
                                 "get_juju_output", "add_storage",
                                 "list_storage"])
        mock_client.series = 'trusty'
        mock_client.version = '2.0'
        mock_client.list_storage_pool.side_effect = [
            json.dumps(storage_pool_details)
        ]
        mock_client.list_storage.side_effect = [
            json.dumps(storage_list_expected),
            json.dumps(storage_list_expected_2),
            json.dumps(storage_list_expected_3),
            json.dumps(storage_list_expected_4),
            json.dumps(storage_list_expected_5),
            json.dumps(storage_list_expected_6)
        ]
        assess_storage(mock_client, mock_client.series)
        self.assertEqual(
            [
                call('ebsy', 'ebs', '1G'),
                call('loopy', 'loop', '1G'),
                call('rooty', 'rootfs', '1G'),
                call('tempy', 'tmpfs', '1G')
            ],
            mock_client.create_storage_pool.mock_calls)
        self.assertEqual(
            [
                call('dummy-storage-lp/0', 'disks', '1')
            ],
            mock_client.add_storage.mock_calls
        )
