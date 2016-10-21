#!/bin/bash
set -eux
TARFILE=$(find $(pwd) -name juju-core*.tar.gz)
echo Using build tarball $TARFILE
TARFILE_NAME=$(basename "$TARFILE")

export GOPATH=$(dirname $(find $WORKSPACE -type d -name src -regex '.*juju-core[^/]*/src'))
if ! go install github.com/juju/juju/...; then
    exit 127
fi
JUJU_BIN=$GOPATH/bin/juju

PRECISE_AMI=$($SCRIPTS/get_ami.py precise amd64)
TRUSTY_AMI=$($SCRIPTS/get_ami.py trusty amd64)
XENIAL_AMI=$($SCRIPTS/get_ami.py xenial amd64)

VERSION=$($JUJU_BIN version | cut -d '-' -f1)
if [[ $VERSION =~ 1\..*  ]]; then
    LXD="echo '1.x does not support lxd'"
    RACE="echo '1.x does not pass race unit tests'"
else
    mkdir -p $WORKSPACE/artifacts/lxd
    LXD="timeout -s INT 20m $SCRIPTS/deploy_job.py parallel-lxd $JUJU_BIN $WORKSPACE/artifacts/lxd merge-juju-lxd  --series xenial --debug"
    RACE="run-unit-tests m1.xlarge $XENIAL_AMI --force-archive --race --local $TARFILE_NAME --install-deps 'golang-1.6 juju-mongodb distro-info-data ca-certificates bzr git-core mercurial zip golang-1.6-race-detector-runtime'"
    RACE="echo 'Skipping race unit tests.'"
fi
timeout 180m concurrently.py -v -l $WORKSPACE/artifacts \
    trusty="$SCRIPTS/run-unit-tests c3.4xlarge $TRUSTY_AMI --local $TARFILE_NAME --use-tmpfs --force-archive" \
    windows="$SCRIPTS/gotestwin.py developer-win-unit-tester.vapour.ws $TARFILE_NAME github.com/juju/juju/cmd" \
    lxd="$LXD" \
    race="$RACE"\
    tarball="$SCRIPTS/check_tar.py $SCRIPTS/tested-public-clouds $TARFILE"
