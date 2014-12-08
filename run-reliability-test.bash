#!/usr/bin/bash
set -eu
: ${SCRIPTS=$(readlink -f $(dirname $0))}
new_juju=$(find $new_juju_dir -name juju)
export JUJU_HOME=$HOME/cloud-city
build_id=${JOB_NAME}-${BUILD_NUMBER}
s3cfg=$JUJU_HOME/juju-qa.s3cfg
s3base=s3://juju-qa-data/industrial-test/${build_id}
if [ "$new_agent_url" != "" ]; then
  extra_args="--new-agent-url $new_agent_url"
else
  extra_args=""
fi
set -x
$SCRIPTS/write_industrial_test_metadata.py $new_juju_dir/buildvars.json \
  $environment metadata.json
s3cmd -c $s3cfg put metadata.json $s3base-metadata.json
timeout -sINT -k 10m 1d $SCRIPTS/industrial_test.py $environment $new_juju \
  $suite --attempts $attempts --json-file results.json $extra_args
s3cmd -c $s3cfg put results.json $s3base-results.json
