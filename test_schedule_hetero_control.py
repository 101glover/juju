from __future__ import print_function

__metaclass__ = type

from datetime import timedelta
import json
import os
from time import time
from unittest import TestCase

from mock import patch

from jujuci import Credentials
from schedule_hetero_control import (
    build_jobs,
    calculate_jobs,
    get_args,
    get_candidate_info,
    get_releases,
    )
from utility import temp_dir
from test_utility import write_config


class TestScheduleHeteroControl(TestCase):

    def test_get_args_credentials(self):
        args, credentials = get_args(['root', '--user', 'jrandom',
                                      '--password', 'password'])
        self.assertEqual(args.user, 'jrandom')
        self.assertEqual(args.password, 'password')
        self.assertEqual(credentials, Credentials('jrandom', 'password'))

    def test_get_releases(self):
        with temp_dir() as root:
            old_juju_dir = os.path.join(root, 'old-juju')
            supported_juju = os.path.join(old_juju_dir, '1.24.5')
            devel_juju = os.path.join(old_juju_dir, '1.25-alpha1')
            test_juju = os.path.join(old_juju_dir, '1.24.5~')
            for path in (old_juju_dir, supported_juju, devel_juju, test_juju):
                os.mkdir(path)
            found = get_releases(root)
            self.assertEqual(['1.24.5'], list(found))


class TestBuildJobs(TestCase):

    def test_build_jobs_credentials(self):
        credentials = Credentials('jrandom', 'password1')
        with temp_dir() as root:
            write_config(root, 'compatibility-control', 'asdf')
            with patch('schedule_hetero_control.Jenkins',
                       autospec=True) as jenkins_mock:
                build_jobs(credentials, root, [])
            jenkins_mock.assert_called_once_with(
                'http://localhost:8080', 'jrandom', 'password1')


class TestGetCandidateInfo(TestCase):
    def test_get_candidate_info(self):
        with temp_dir() as dir_path:
            make_build_var_file(dir_path, version='1.24.3')
            version, revision = get_candidate_info(dir_path)
        self.assertEqual(version, '1.24.3')
        self.assertEqual(revision, '2870')


class CalculateJobs(TestCase):
    def test_calculate_jobs(self):
        with temp_dir() as root:
            release_path = os.path.join(root, 'old-juju', '1.20.11')
            os.makedirs(release_path)
            candidate_path = os.path.join(root, 'candidate', '1.24')
            os.makedirs(candidate_path)
            make_build_var_file(candidate_path, version='1.24.3')
            jobs = list(calculate_jobs(root))
        expected = self.make_jobs('1.24.3', '1.20.11', '1.24')
        self.assertItemsEqual(jobs, expected)

    def test_calculate_jobs_schedule_all(self):
        with temp_dir() as root:
            release_path = os.path.join(root, 'old-juju', '1.20.11')
            os.makedirs(release_path)
            candidate_path = os.path.join(root, 'candidate', '1.24')
            os.makedirs(candidate_path)
            make_build_var_file(candidate_path, '1.24.3')
            candidate_path_2 = os.path.join(root, 'candidate', '1.23')
            os.makedirs(candidate_path_2)
            buildvars_path = make_build_var_file(candidate_path_2, '1.23.3')
            a_week_ago = time() - timedelta(days=7, seconds=1).total_seconds()
            os.utime(buildvars_path, (time(), a_week_ago))
            jobs = list(calculate_jobs(root, schedule_all=False))
            jobs_schedule_all = list(calculate_jobs(root, schedule_all=True))
        expected = self.make_jobs('1.24.3', '1.20.11', '1.24')
        expected.extend(self.make_jobs('1.23.3', '1.20.11', '1.23'))
        self.assertItemsEqual(jobs, expected[:6])
        self.assertItemsEqual(jobs_schedule_all, expected)

    def test_calculate_jobs_osx(self):
        with temp_dir() as root:
            release_path = os.path.join(root, 'old-juju', '1.20.11')
            os.makedirs(release_path)
            candidate_path = os.path.join(root, 'candidate', '1.24.4')
            os.makedirs(candidate_path)
            make_build_var_file(candidate_path, '1.24.4')
            jobs = list(calculate_jobs(root, schedule_all=False))
        expected = self.make_jobs('1.24.4', '1.20.11')
        self.assertItemsEqual(jobs, expected)

    def make_jobs(self, candidate, old_version, candidate_path=False):
        jobs = []
        for client_os in ('ubuntu', 'osx', 'windows'):
            for new_to_old in ('false', 'true'):
                jobs.append({
                    'candidate': candidate,
                    'candidate_path': candidate_path or candidate,
                    'client_os': client_os,
                    'new_to_old': new_to_old,
                    'old_version': old_version,
                    'revision_build': '2870'})
        return jobs


def make_build_var_file(dir_path, version, revision_build="2870"):
    build_vars = {"version": version, "revision_build": revision_build}
    file_path = os.path.join(dir_path, 'buildvars.json')
    with open(file_path, 'w') as json_file:
        json.dump(build_vars, json_file)
    return file_path
