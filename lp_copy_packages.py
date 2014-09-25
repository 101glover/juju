#!/usr/bin/python

from __future__ import print_function

from argparse import ArgumentParser
import sys

from launchpadlib.launchpad import Launchpad


DEVEL = 'devel'
PROPOSED = 'proposed'
STABLE = 'stable'


def get_archives(to_archive_name):
    """Return the archives used in the copy.

    The build archives are private and owned by a different team than the
    public archives. The policy for building and copying is:
    1. Always build in private PPAs because the client cannot be made public
       before the tools made from the client package.
    2. Devel packages are built in a separate PPA from stable packages
       because devel can change the archive deps.
    3. Built devel packages can be copied to the public devel PPA.
    4. Built stable packages can be copied to the public proposed PPA.
    5. After evaluation, the stable packages in public proposed can be copied
       to the public stable PPA.
    """
    to_team_name = 'juju'
    if to_archive_name == DEVEL:
        from_archive_name = DEVEL
        from_team_name = 'juju-packaging'
    elif to_archive_name == PROPOSED:
        from_archive_name = STABLE
        from_team_name = 'juju-packaging'
    elif to_archive_name == STABLE:
        from_archive_name = PROPOSED
        from_team_name = 'juju'
    else:
        raise ValueError('{} is not a valid archive'.format(to_archive_name))
    from_team = lp.people[from_team_name]
    from_archive = from_team.getPPAByName(name=from_archive_name)
    to_team = lp.people[to_team_name]
    to_archive = to_team.getPPAByName(name=to_archive_name)
    return from_archive, to_archive


def copy_packages(lp, version, to_archive_name, dry_run=False):
    """Copy the juju-core source and binary packages to and archive."""
    from_archive, to_archive = get_archives(to_archive_name)
    package_histories = from_archive.getPublishedSources(
        source_name='juju-core', status='Published')
    package_histories = [
        package for package in package_histories
        if package.source_package_version.startswith(version)]
    if len(package_histories) == 0:
        raise ValueError(
            'No packages matching {} were found in {} to copy to {}.'.format(
                version, from_archive.web_link, to_archive.web_link))
    for package in package_histories:
        print(
            'Copying {} and its binaries to {}.'.format(
                package.display_name, to_archive_name))
        if not dry_run:
            to_archive.copyPackage(
                from_archive=from_archive,
                source_name=package.source_package_name,
                version=package.source_package_version,
                to_pocket='Release', include_binaries=True, unembargo=True)
    return 0


def get_argument_parser():
    """Return the option parser for this program."""
    parser = ArgumentParser('Copy juju-core from one archive to another')
    parser.add_argument(
        '--dry-run', action="store_true", default=False,
        help='Explain what will happen without making changes')
    parser.add_argument('version', help='The package version like 1.20.8')
    parser.add_argument(
        'to_archive_name',
        help='The archive to copy the source and binary packages to.')
    return parser


if __name__ == '__main__':
    parser = get_argument_parser()
    args = parser.parse_args()
    lp = Launchpad.login_with(
        'lp-copy-packages', service_root='https://api.launchpad.net',
        version='devel')
    ret_code = copy_packages(
        lp, args.version, args.to_archive_name, args.dry_run)
    sys.exit(ret_code)
