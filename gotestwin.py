#!/usr/bin/env python
from argparse import ArgumentParser
from json import dump
import os
from os.path import (
    basename,
    dirname,
    join,
)
import subprocess


def main():
    scripts = dirname(__file__)
    parser = ArgumentParser()
    parser.add_argument('host', help='The machine to test on.')
    parser.add_argument('revision', help='The revision-build to test.')
    parser.add_argument('package', nargs='?', default='github.com/juju/juju',
                        help='The package to test.')
    args = parser.parse_args()

    s3_ci_path = join(scripts, 's3ci.py')
    downloaded = subprocess.check_output([
        s3_ci_path, 'get', args.revision, 'build-revision',
        'juju_core.*.tar.gz', './'])
    tarfile = basename(downloaded)

    job_name = os.environ.get('job_name', 'GoTestWin')
    subprocess.check_call([s3_ci_path, 'get-summary', args.revision, job_name])
    with open('temp-config.yaml', 'w') as temp_file:
        dump({
            'install': {'ci': [
                tarfile,
                join(scripts, 'gotesttarfile.py'),
                join(scripts, 'jujucharm.py'),
                join(scripts, 'utility.py'),
                ]},
            'command': [
                'python', 'ci/gotesttarfile.py', '-v', '-g', 'go.exe', '-p',
                args.package, '--remove', 'ci/{}'.format(tarfile)
                ]},
             temp_file)
    juju_home = os.environ.get('JUJU_HOME',
                               join(dirname(scripts), 'cloud-city'))
    subprocess.check_call([
        'workspace-run', '-v', '-i', join(juju_home, 'staging-juju-rsa'),
        'temp-config.yaml', 'Administrator@{}'.format(args.host)
        ])


if __name__ == '__main__':
    main()
