#!/usr/bin/env python
"""Tests for the autoload-credentials command."""

from __future__ import print_function

import argparse
from collections import (
    defaultdict,
    namedtuple,
    )
import itertools
import json
import logging
import os
import sys
import tempfile
from textwrap import dedent

import pexpect

from jujupy import (
    client_from_config,
    )
from utility import (
    add_basic_testing_arguments,
    configure_logging,
    ensure_dir,
    temp_dir,
    )


__metaclass__ = type


log = logging.getLogger("assess_autoload_credentials")


# Store details for querying the interactive command.
# cloud_listing: String response for choosing credential to save
# save_name: String response in which to save the credential under.
ExpectAnswers = namedtuple('ExpectAnswers', ['cloud_listing', 'save_name'])

# Store details for setting up a clouds credentials as well as what to compare
# during test.
# env_var_changes: dict
# expected_details: dict
# expect_answers: ExpectAnswers object
CloudDetails = namedtuple(
    'CloudDetails',
    ['env_var_changes', 'expected_details', 'expect_answers']
    )


class CredentialIdCounter:
    _counter = defaultdict(itertools.count)

    @classmethod
    def id(cls, provider_name):
        return cls._counter[provider_name].next()


def assess_autoload_credentials(args):
    test_scenarios = {
        'ec2': [('AWS using environment variables', aws_envvar_test_details),
                ('AWS using credentials file', aws_directory_test_details)],
        'openstack':
            [('OS using environment variables', openstack_envvar_test_details),
             ('OS using credentials file', openstack_directory_test_details)],
        'gce': [('GCE using envvar with credentials file',
                 gce_envvar_with_file_test_details),
                ('GCE using credentials file',
                 gce_file_test_details)],
        }

    client = client_from_config(args.env, args.juju_bin, False)
    provider = client.env.config['type']

    for scenario_name, scenario_setup in test_scenarios[provider]:
        log.info('* Starting test scenario: {}'.format(scenario_name))
        ensure_autoload_credentials_stores_details(client, scenario_setup)

    for scenario_name, scenario_setup in test_scenarios[provider]:
        log.info(
            '* Starting [overwrite] test, scenario: {}'.format(scenario_name))
        ensure_autoload_credentials_overwrite_existing(
            client, scenario_setup)


def ensure_autoload_credentials_stores_details(client, cloud_details_fn):
    """Test covering loading and storing credentials using autoload-credentials

    XXX
    :param juju_bin: The full path to the juju binary to use for the test run.
    :param cloud_details_fn: A callable that takes the 3 arguments `user`
      string, `tmp_dir` path string and client EnvJujuClient and will returns a
      `CloudDetails` object used to setup creation of credential details &
      comparison of the result.

    """
    user = 'testing_user'
    with temp_dir() as tmp_dir:
        tmp_juju_home = tempfile.mkdtemp(dir=tmp_dir)
        tmp_scratch_dir = tempfile.mkdtemp(dir=tmp_dir)
        client.env.juju_home = tmp_juju_home
        client.env.load_yaml()
        cloud_details = cloud_details_fn(user, tmp_scratch_dir, client)

        run_autoload_credentials(
            client,
            cloud_details.env_var_changes,
            cloud_details.expect_answers)

        client.env.load_yaml()

        assert_credentials_contains_expected_results(
            client.env.credentials,
            cloud_details.expected_details)


def ensure_autoload_credentials_overwrite_existing(client, cloud_details_fn):
    """Storing credentials using autoload-credentials must overwrite existing.

    XXX
    :param juju_bin: The full path to the juju binary to use for the test run.
    :param cloud_details_fn: A callable that takes the 3 arguments `user`
      string, `tmp_dir` path string and client EnvJujuClient and will returns a
      `CloudDetails` object used to setup creation of credential details &
      comparison of the result.

    """
    user = 'testing_user'
    with temp_dir() as tmp_dir:
        tmp_juju_home = tempfile.mkdtemp(dir=tmp_dir)
        tmp_scratch_dir = tempfile.mkdtemp(dir=tmp_dir)
        client.env.juju_home = tmp_juju_home
        client.env.load_yaml()

        initial_details = cloud_details_fn(
            user, tmp_scratch_dir, client)

        run_autoload_credentials(
            client,
            initial_details.env_var_changes,
            initial_details.expect_answers)

        # Now run again with a second lot of details.
        overwrite_details = cloud_details_fn(user, tmp_scratch_dir, client)

        if (
                overwrite_details.expected_details ==
                initial_details.expected_details):
            raise ValueError(
                'Attempting to use identical values for overwriting')

        run_autoload_credentials(
            client,
            overwrite_details.env_var_changes,
            overwrite_details.expect_answers)

        client.env.load_yaml()

        assert_credentials_contains_expected_results(
            client.env.credentials,
            overwrite_details.expected_details)


def assert_credentials_contains_expected_results(credentials, expected):
    if credentials != expected:
        raise ValueError(
            'Actual credentials do not match expected credentials.\n'
            'Expected: {expected}\nGot: {got}\n'.format(
                expected=expected,
                got=credentials))


def run_autoload_credentials(client, envvars, answers):
    """Execute the command 'juju autoload-credentials'.

    Simple interaction, calls juju autoload-credentials selects the first
    option and then quits.

    :param client: EnvJujuClient from which juju will be called.
    :param envvars: Dictionary containing environment variables to be used
      during execution.
    :param answers: ExpectAnswers object containing answers for the interactive
      command

    """
    process = client.expect(
        'autoload-credentials', extra_env=envvars, include_e=False)
    process.expect('.*1. {} \(.*\).*'.format(answers.cloud_listing))
    process.sendline('1')

    process.expect(
        '(Select the cloud it belongs to|Enter cloud to which the credential)'
        '.* Q to quit.*')
    process.sendline(answers.save_name)
    process.expect(
        'Saved {listing_display} to cloud {save_name}'.format(
            listing_display=answers.cloud_listing,
            save_name=answers.save_name))
    process.sendline('q')
    process.expect(pexpect.EOF)

    if process.isalive():
        log.debug('juju process is still running: {}'.format(str(process)))
        process.terminate(force=True)
        raise AssertionError('juju process failed to terminate')


def aws_envvar_test_details(user, tmp_dir, client, credential_details=None):
    """client is un-used for AWS"""
    credential_details = credential_details or aws_credential_dict_generator()
    access_key = credential_details['access_key']
    secret_key = credential_details['secret_key']
    env_var_changes = get_aws_environment(user, access_key, secret_key)

    answers = ExpectAnswers(
        cloud_listing='aws credential "{}"'.format(user),
        save_name='aws')

    expected_details = get_aws_expected_details_dict(
        user, access_key, secret_key)

    return CloudDetails(env_var_changes, expected_details, answers)


def aws_directory_test_details(user, tmp_dir, client, credential_details=None):
    """client is un-used for AWS"""
    credential_details = credential_details or aws_credential_dict_generator()
    access_key = credential_details['access_key']
    secret_key = credential_details['secret_key']
    expected_details = get_aws_expected_details_dict(
        'default', access_key, secret_key)

    write_aws_config_file(tmp_dir, access_key, secret_key)

    answers = ExpectAnswers(
        cloud_listing='aws credential "{}"'.format('default'),
        save_name='aws')

    env_var_changes = dict(HOME=tmp_dir)

    return CloudDetails(env_var_changes, expected_details, answers)


def get_aws_expected_details_dict(cloud_name, access_key, secret_key):
    # Build credentials yaml file-like datastructure.
    return {
        'credentials': {
            'aws': {
                cloud_name: {
                    'auth-type': 'access-key',
                    'access-key': access_key,
                    'secret-key': secret_key,
                    }
                }
            }
        }


def get_aws_environment(user, access_key, secret_key):
    """Return a dictionary containing keys suitable for AWS env vars."""
    return dict(
        USER=user,
        AWS_ACCESS_KEY_ID=access_key,
        AWS_SECRET_ACCESS_KEY=secret_key)


def write_aws_config_file(tmp_dir, access_key, secret_key):
    """Write aws credentials file to tmp_dir

    :return: String path of created credentials file.

    """
    config_dir = os.path.join(tmp_dir, '.aws')
    config_file = os.path.join(config_dir, 'credentials')
    ensure_dir(config_dir)

    config_contents = dedent("""\
    [default]
    aws_access_key_id={}
    aws_secret_access_key={}
    """.format(access_key, secret_key))

    with open(config_file, 'w') as f:
        f.write(config_contents)

    return config_file


def aws_credential_dict_generator():
    call_id = CredentialIdCounter.id('aws')
    creds = 'aws-credentials-{}'.format(call_id)
    return dict(
        access_key=creds,
        secret_key=creds)


def openstack_envvar_test_details(
        user, tmp_dir, client, credential_details=None):
    if credential_details is None:
        credential_details = openstack_credential_dict_generator()

    expected_details, answers = setup_basic_openstack_test_details(
        client, user, credential_details)

    env_var_changes = get_openstack_envvar_changes(user, credential_details)

    return CloudDetails(env_var_changes, expected_details, answers)


def get_openstack_envvar_changes(user, credential_details):
    return dict(
        USER=user,
        OS_USERNAME=user,
        OS_PASSWORD=credential_details['os_password'],
        OS_TENANT_NAME=credential_details['os_tenant_name'])


def openstack_directory_test_details(
        user, tmp_dir, client, credential_details=None
):
    if credential_details is None:
        credential_details = openstack_credential_dict_generator()

    expected_details, answers = setup_basic_openstack_test_details(
        client, user, credential_details)

    write_openstack_config_file(tmp_dir, user, credential_details)
    env_var_changes = dict(HOME=tmp_dir)

    return CloudDetails(env_var_changes, expected_details, answers)


def setup_basic_openstack_test_details(client, user, credential_details):
    ensure_openstack_personal_cloud_exists(client)
    expected_details = get_openstack_expected_details_dict(
        user, credential_details)
    answers = ExpectAnswers(
        cloud_listing='openstack region ".*" project "{}" user "{}"'.format(
            credential_details['os_tenant_name'],
            user),
        save_name='testing_openstack')

    return expected_details, answers


def write_openstack_config_file(tmp_dir, user, credential_details):
    credentials_file = os.path.join(tmp_dir, '.novarc')
    with open(credentials_file, 'w') as f:
        credentials = dedent("""\
        export OS_USERNAME={user}
        export OS_PASSWORD={password}
        export OS_TENANT_NAME={tenant_name}
        """.format(
            user=user,
            password=credential_details['os_password'],
            tenant_name=credential_details['os_tenant_name'],
            ))
        f.write(credentials)
    return credentials_file


def ensure_openstack_personal_cloud_exists(client):
    os_cloud = {
        'testing_openstack': {
            'type': 'openstack',
            'regions': {
                'test1': {
                    'endpoint': 'https://example.com',
                    'auth-types': ['access-key', 'userpass']
                    }
                }
            }
        }
    client.env.clouds['clouds'] = os_cloud
    client.env.dump_yaml(client.env.juju_home, config=None)


def get_openstack_expected_details_dict(user, credential_details):
    return {
        'credentials': {
            'testing_openstack': {
                user: {
                    'auth-type': 'userpass',
                    'domain-name': '',
                    'password': credential_details['os_password'],
                    'tenant-name': credential_details['os_tenant_name'],
                    'username': user
                    }
                }
            }
        }


def openstack_credential_dict_generator():
    call_id = CredentialIdCounter.id('openstack')
    creds = 'openstack-credentials-{}'.format(call_id)
    return dict(
        os_tenant_name=creds,
        os_password=creds)


def gce_envvar_with_file_test_details(
        user, tmp_dir, client, credential_details=None
):
    if credential_details is None:
        credential_details = gce_credential_dict_generator()
    credentials_path = write_gce_config_file(tmp_dir, credential_details)

    answers = ExpectAnswers(
        cloud_listing='google credential "{}"'.format(
            credential_details['client_email']),
        save_name='google')

    expected_details = get_gce_expected_details_dict(user, credentials_path)

    env_var_changes = dict(
        USER=user,
        GOOGLE_APPLICATION_CREDENTIALS=credentials_path,
        )

    return CloudDetails(env_var_changes, expected_details, answers)


def gce_file_test_details(
        user, tmp_dir, client, credential_details=None
):
    if credential_details is None:
        credential_details = gce_credential_dict_generator()

    home_path, credentials_path = write_gce_home_config_file(
        tmp_dir, credential_details)

    answers = ExpectAnswers(
        cloud_listing='google credential "{}"'.format(
            credential_details['client_email']),
        save_name='google')

    expected_details = get_gce_expected_details_dict(user, credentials_path)

    env_var_changes = dict(USER=user, HOME=home_path)

    return CloudDetails(env_var_changes, expected_details, answers)


def write_gce_config_file(tmp_dir, credential_details, filename=None):

    details = dict(
        type='service_account',
        client_id=credential_details['client_id'],
        client_email=credential_details['client_email'],
        private_key=credential_details['private_key'])

    # Generate a unique filename if none provided as this is stored and used in
    # comparisons.
    filename = filename or 'gce-file-config-{}.json'.format(
        CredentialIdCounter.id('gce-fileconfig'))
    credential_file = os.path.join(tmp_dir, filename)
    with open(credential_file, 'w') as f:
        json.dump(details, f)

    return credential_file


def write_gce_home_config_file(tmp_dir, credential_details):
    """Returns a tuple contining a new HOME path and credential file path."""
    # Add a unique string for home dir so each file path is unique within the
    # stored credentials file.
    home_dir = os.path.join(tmp_dir, 'gce-homedir-{}'.format(
        CredentialIdCounter.id('gce-homedir')))
    credential_path = os.path.join(home_dir, '.config', 'gcloud')
    os.makedirs(credential_path)

    written_credentials_path = write_gce_config_file(
        credential_path,
        credential_details,
        'application_default_credentials.json')

    return home_dir, written_credentials_path


def get_gce_expected_details_dict(user, credentials_path):
    return {
        'credentials': {
            'google': {
                user: {
                    'auth-type': 'jsonfile',
                    'file': credentials_path,
                    }
                }
            }
        }


def gce_credential_dict_generator():
    call_id = CredentialIdCounter.id('gce')
    creds = 'gce-credentials-{}'.format(call_id)
    return dict(
        client_id=creds,
        client_email='{}@example.com'.format(creds),
        private_key=creds,
        )


def parse_args(argv):
    """Parse all arguments."""
    parser = argparse.ArgumentParser(
        description="Test autoload-credentials command.")
    add_basic_testing_arguments(parser)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)

    assess_autoload_credentials(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
