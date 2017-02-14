"""Tests for assess_juju_status module."""

import logging
import StringIO

from mock import (
    call,
    Mock,
    patch,
    )
from assess_juju_status import (
    deploy_charm_with_subordinate_charm,
    verify_subordinate_status,
    verify_charm_status,
    assess_juju_status,
    parse_args,
    main,
    )
from jujupy import fake_juju_client
from tests import (
    parse_error,
    TestCase,
    )
from jujupy.client import (
    Status,
    )
from assess_min_version import (
    JujuAssertionError
)


class TestParseArgs(TestCase):

    def test_common_args(self):
        args = parse_args(["an-env", "/bin/juju", "/tmp/logs", "an-env-mod"])
        self.assertEqual("an-env", args.env)
        self.assertEqual("/bin/juju", args.juju_bin)
        self.assertEqual("/tmp/logs", args.logs)
        self.assertEqual("an-env-mod", args.temp_env_name)
        self.assertEqual(False, args.debug)

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
        with patch("assess_juju_status.configure_logging",
                   autospec=True) as mock_cl:
            with patch("assess_juju_status.BootstrapManager.booted_context",
                       autospec=True) as mock_bc:
                with patch('deploy_stack.client_from_config',
                           return_value=client) as mock_cfc:
                    with patch("assess_juju_status.assess_juju_status",
                               autospec=True) as mock_assess:
                        main(argv)
        mock_cl.assert_called_once_with(logging.DEBUG)
        mock_cfc.assert_called_once_with('an-env', "/bin/juju", debug=False,
                                         soft_deadline=None)
        self.assertEqual(mock_bc.call_count, 1)
        mock_assess.assert_called_once_with(client, "xenial")


class TestAssess(TestCase):

    def test_juju_status(self):
        fake_client = Mock(wraps=fake_juju_client())
        fake_client.bootstrap()
        deploy_charm_with_subordinate_charm(fake_client, 'xenial')
        fake_client.deploy.assert_has_calls([call('dummy-sink'),
                                             call('dummy-subordinate')])
        fake_client.wait_for_started.assert_has_calls([call()] * 2)

    def test_verify_charm_status(self):
        fake_client = Mock(wraps=fake_juju_client())
        app_status = Status({
            'applications': {
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {
                            'juju-status': {
                                'current': 'idle',
                                'since': 'DD MM YYYY hh:mm:ss',
                                'version': '2.0.0',
                            }
                        }
                    }
                }
            }
        }, '')
        fake_client.bootstrap()
        with patch.object(fake_client, 'get_status', autospec=True) as ags:
            ags.return_value = app_status
            charm_details = fake_client.get_status().get_applications()[
                'dummy-sink']
            verify_charm_status(charm_details)
            self.assertIn("verified charm app status successfully",
                          self.log_stream.getvalue())

    def test_verify_charm_status_to_fail(self):
        fake_client = Mock(wraps=fake_juju_client())
        app_status = Status({
            'applications': {
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {
                            'juju-status': {
                            }
                        }
                    }
                }
            }
        }, '')
        fake_client.bootstrap()
        with patch.object(fake_client, 'get_status', autospec=True) as ags:
            ags.return_value = app_status
            charm_details = fake_client.get_status().get_applications()[
                'dummy-sink']
            with self.assertRaisesRegexp(
                    JujuAssertionError, "charm app status not found"):
                verify_charm_status(charm_details)

    def test_verify_subordinate_app_status(self):
        fake_client = Mock(wraps=fake_juju_client())
        app_status = Status({
            'applications': {
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {
                            'juju-status': {
                                'current': 'idle',
                                'since': 'DD MM YYYY hh:mm:ss',
                                'version': '2.0.0',
                            },
                            'subordinates': {
                                'dummy-subordinate/0': {
                                    'juju-status': {
                                        'current': 'idle',
                                        'since': 'DD MM YYYY hh:mm:ss',
                                        'version': '2.0.0'
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }, '')
        fake_client.bootstrap()
        with patch.object(fake_client, 'get_status', autospec=True) as ags:
            ags.return_value = app_status
            charm_details = fake_client.get_status().get_applications()[
                'dummy-sink']
            verify_subordinate_status(charm_details)
            self.assertIn("verified charm subordinate status successfully",
                          self.log_stream.getvalue())

    def test_verify_subordinate_app_status_to_fail(self):
        fake_client = Mock(wraps=fake_juju_client())
        app_status = Status({
            'applications': {
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {
                            'juju-status': {
                                'current': 'idle',
                                'since': 'DD MM YYYY hh:mm:ss',
                                'version': '2.0.0',
                            },
                            'subordinates': {
                                'dummy-subordinate/0': {
                                    'juju-status': {
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }, '')
        fake_client.bootstrap()
        with patch.object(fake_client, 'get_status', autospec=True) as ags:
            ags.return_value = app_status
            charm_details = fake_client.get_status().get_applications()[
                'dummy-sink']
            with self.assertRaisesRegexp(
                    JujuAssertionError, "charm subordinate status not found"):
                verify_subordinate_status(charm_details)

    def test_assess_juju_status(self):
        fake_client = Mock(wraps=fake_juju_client())
        app_status = Status({
            'applications': {
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {
                            'juju-status': {
                                'current': 'idle',
                                'since': 'DD MM YYYY hh:mm:ss',
                                'version': '2.0.0',
                            },
                            'subordinates': {
                                'dummy-subordinate/0': {
                                    'juju-status': {
                                        'current': 'idle',
                                        'since': 'DD MM YYYY hh:mm:ss',
                                        'version': '2.0.0'
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }, '')
        fake_client.bootstrap()
        with patch.object(fake_client, 'get_status', autospec=True) as ags:
            ags.return_value = app_status
            assess_juju_status(fake_client, "xenial")
            self.assertIn('assess juju-status done successfully',
                          self.log_stream.getvalue())
