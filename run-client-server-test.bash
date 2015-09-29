#!/bin/bash
set -eu
SCRIPTS=$(readlink -f $(dirname $0))
export PATH=$HOME/workspace-runner:$PATH

usage() {
    echo "usage: $0 old-version candidate-version new-to-old client-os log-dir"
    exit 1
}
test $# -eq 5 || usage
old_version="$1"
candidate_version="$2"
new_to_old="$3"
client_os="$4"
log_dir="$5"

set -x

if [[ "$new_to_old" == "true"  && -d $HOME/candidate/$candidate_version ]]; then
    echo "Using weekly streams for unreleased version"
    agent_arg="--agent-url http://juju-dist.s3.amazonaws.com/weekly/tools"
else
    echo "Using official proposed (or released) streams"
    agent_arg="--agent-stream proposed"
fi


if [[ "$client_os" == "ubuntu" ]]; then
    if [[ -d $HOME/old-juju/$candidate_version ]]; then
        candidate_juju=$(find $HOME/old-juju/$candidate_version -name juju)
    else
        candidate_juju=$(find $HOME/candidate/$candidate_version -name juju)
    fi
    old_juju=$(find $HOME/old-juju/$old_version -name juju)
    server=$old_juju
    client=$candidate_juju
    if [[ "$new_to_old" == "true" ]]; then
        server=$candidate_juju
        client=$old_juju
    fi
    echo "Server: " `$server --version`
    echo "Client: " `$client --version`
elif [[ "$client_os" == "osx" ]]; then
    user_at_host="jenkins@osx-slave.vapour.ws"
    remote_script="run-client-server-test-remote.bash"
elif [[ "$client_os" == "windows" ]]; then
    remote_script="run-win-client-server-remote.bash"
    user_at_host="Administrator@win-slave.vapour.ws"
else
    echo "Unkown client OS."
    exit 1
fi

run_remote_script() {
    cat > temp-config.yaml <<EOT
install:
    remote:
        - $SCRIPTS/$remote_script
command: [remote/$remote_script, "$candidate_version", "$old_version", "$new_to_old", "$agent_arg"]
EOT
    workspace-run temp-config.yaml $user_at_host
}

set +e
for i in `seq 1 2`; do
    if [[ "$client_os" == "ubuntu" ]]; then
        $SCRIPTS/assess_heterogeneous_control.py $server $client test-reliability-aws $JOB_NAME $log_dir $agent_arg
    else
        run_remote_script
    fi
    RESULT=$?
    if [[ $RESULT == 0 ]]; then
        break
    fi
    if [[ $i == 1 ]]; then
        # Don't remove the log if it fails on the second try.
        rm -rf $log_dir/*
    fi
done
exit $RESULT