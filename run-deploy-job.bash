#!/bin/bash
# Script to run deploy_job against current binaries.
# usage: run-deploy-job.bash {deploy,upgrade} SERIES BASE_ENVIRONMENT TIMEOUT
set -eu
: ${SCRIPTS=$(readlink -f $(dirname $0))}
export SCRIPTS
export USER=jenkins
export JUJU_REPOSITORY=$HOME/repository
export JUJU_HOME=$HOME/cloud-city
export ENV=$3
source $JUJU_HOME/juju-qa.jujuci
set -x
if [ "$1" = "upgrade" ]; then
  extra_args="--upgrade"
elif [ "$1" = "deploy" ]; then
  extra_args=""
else
  echo "Unknown action $1"
  exit 1
fi
series=$2
timeout=$4
shift 4
$SCRIPTS/jujuci.py -v setup-workspace --clean-env $JOB_NAME $WORKSPACE
VERSION=$($SCRIPTS/jujuci.py get-build-vars --version $revision_build)
package=$($SCRIPTS/jujuci.py get-package-name $VERSION)
s3cmd --config $JUJU_HOME/juju-qa.s3cfg sync \
  s3://juju-qa-data/juju-ci/products/version-$revision_build . \
  --exclude '*' --include $package
# Find the deb with the highest build- number.
deb=$(find . -name $package|sed -r 's/.*build-([0-9]+)\/.*/\1 \0/'|sort -g|\
      tail -n1| cut -f 2 -d ' ')
dpkg -x $deb extracted-bin
JUJU_BIN=$(dirname $(find extracted-bin -name 'juju'))
$SCRIPTS/jujuci.py get-build-vars --summary --env $ENV $revision_build
if [[ $VERSION =~ ^1\.2[1-2].*$ ]]; then
    echo "Setting the defaul juju to 1.20.11."
    export PATH="$HOME/old-juju/1.20.11/usr/lib/juju-1.20.11/bin:$PATH"
fi

timeout -s INT $timeout $SCRIPTS/deploy_job.py --series $series\
   $ENV $JUJU_BIN/juju $WORKSPACE/artifacts $JOB_NAME $extra_args "$@"
