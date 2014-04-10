#!/usr/bin/env bash
# Release public tools.
#
# Publish to Canonistack, HP, AWS, and Azure.
# This script requires that the user has credentials to upload the tools
# to Canonistack, HP Cloud, AWS, and Azure

set -e

SCRIPT_DIR=$(cd $(dirname "${BASH_SOURCE[0]}") && pwd )


usage() {
    echo "usage: $0 PURPOSE DIST_DIRECTORY"
    echo "  PURPOSE: 'RELEASE' or  'TESTING'"
    echo "    RELEASE installs tools/ at the top of juju-dist/tools."
    echo "    TESTING installs tools/ at juju-dist/testing/tools."
    echo "  DIST_DIRECTORY: The directory to the assembled tools."
    echo "    This is the juju-dist dir created by assemble-public-tools.bash."
    exit 1
}


check_deps() {
    echo "Phase 0: Checking requirements."
    has_deps=1
    which swift || has_deps=0
    which s3cmd || has_deps=0
    test -f $JUJU_DIR/canonistacktoolsrc || has_deps=0
    test -f $JUJU_DIR/hptoolsrc || has_deps=0
    test -f $JUJU_DIR/s3cfg || has_deps=0
    test -f $JUJU_DIR/azuretoolsrc || has_deps=0
    if [[ $has_deps == 0 ]]; then
        echo "Install python-swiftclient, and s3cmd"
        echo "Your $JUJU_DIR dir must contain rc files to publish:"
        echo "  canonistacktoolsrc, hptoolsrc, s3cfg, azuretoolsrc"
        exit 2
    fi
}


publish_to_canonistack() {
    [[ $JT_IGNORE_CANONISTACK == '1' ]] && return 0
    echo "Phase 1: Publish to canonistack."
    source $JUJU_DIR/canonistacktoolsrc
    cd $JUJU_DIST/tools/releases/
    ${SCRIPT_DIR}/swift-sync.bash tools/releases/ *.tgz
    cd $JUJU_DIST/tools/streams/v1
    ${SCRIPT_DIR}/swift-sync.bash tools/streams/v1/ {index,com}*
    # This needed to allow old deployments upgrade.
    cd ${JUJU_DIST}/tools
}


testing_to_canonistack() {
    [[ $JT_IGNORE_CANONISTACK == '1' ]] && return 0
    echo "Phase 1: Testing to canonistack."
    source $JUJU_DIR/canonistacktoolsrc
    cd $JUJU_DIST/tools/releases/
    ${SCRIPT_DIR}/swift-sync.bash testing/tools/releases/ *.tgz
    cd $JUJU_DIST/tools/streams/v1
    ${SCRIPT_DIR}/swift-sync.bash testing/tools/streams/v1/ {index,com}*
}


publish_to_hp() {
    [[ $JT_IGNORE_HP == '1' ]] && return 0
    echo "Phase 2: Publish to HP Cloud."
    source $JUJU_DIR/hptoolsrc
    cd $JUJU_DIST/tools/releases/
    ${SCRIPT_DIR}/swift-sync.bash tools/releases/ *.tgz
    cd $JUJU_DIST/tools/streams/v1
    ${SCRIPT_DIR}/swift-sync.bash tools/streams/v1/ {index,com}*
    # Support old tools location so that deployments can upgrade to new tools.
    cd ${JUJU_DIST}/tools
}


testing_to_hp() {
    # sync-tools cannot place the tools in a testing location, so swift is
    # used.
    [[ $JT_IGNORE_HP == '1' ]] && return 0
    echo "Phase 2: Testing to HP Cloud."
    source $JUJU_DIR/hptoolsrc
    cd $JUJU_DIST/tools/releases/
    ${SCRIPT_DIR}/swift-sync.bash testing/tools/releases/ *.tgz
    cd $JUJU_DIST/tools/streams/v1
    ${SCRIPT_DIR}/swift-sync.bash testing/tools/streams/v1/ {index,com}*
}


publish_to_aws() {
    [[ $JT_IGNORE_AWS == '1' ]] && return 0
    echo "Phase 3: Publish to AWS."
    s3cmd -c $JUJU_DIR/s3cfg sync --exclude '*mirror*' \
        ${JUJU_DIST}/tools s3://juju-dist/
}


testing_to_aws() {
    # this is the same as the publishing command except that the
    # destination is juju-dist/testing/
    [[ $JT_IGNORE_AWS == '1' ]] && return 0
    echo "Phase 3.1: Testing to AWS juju-dist."
    s3cmd -c $JUJU_DIR/s3cfg sync --exclude '*mirror*' \
        ${JUJU_DIST}/tools s3://juju-dist/testing/
}

shim_creds() {
    # The azure library uses different vars than was defined for gwacl.
    export AZURE_STORAGE_ACCOUNT=${AZURE_STORAGE_ACCOUNT:-$AZURE_ACCOUNT}
    AZURE_STORAGE_ACCESS_KEY=${AZURE_STORAGE_ACCESS_KEY:-$AZURE_JUJU_TOOLS_KEY}
    export AZURE_STORAGE_ACCOUNT AZURE_STORAGE_ACCESS_KEY
}

publish_to_azure() {
    [[ $JT_IGNORE_AZURE == '1' ]] && return 0
    echo "Phase 4: Publish to Azure."
    source $JUJU_DIR/azuretoolsrc
    shim_creds
    ${SCRIPT_DIR}/azure_publish_tools.py publish release ${JUJU_DIST}
}


testing_to_azure() {
    # This command is like the publish command expcept tht -container is
    # different.
    [[ $JT_IGNORE_AZURE == '1' ]] && return 0
    echo "Phase 4: Testing to Azure."
    source $JUJU_DIR/azuretoolsrc
    shim_creds
    ${SCRIPT_DIR}/azure_publish_tools.py publish testing ${JUJU_DIST}
}


publish_to_streams() {
    [[ -f $JUJU_DIR/streamsrc ]] || return 0
    [[ $JT_IGNORE_STREAMS == '1' ]] && return 0
    echo "Phase 5: Published to streams.canonical.com."
    source $JUJU_DIR/streamsrc
    rsync -avzh $JUJU_DIST/ $STREAMS_OFFICIAL_DEST
}


testing_to_streams() {
    [[ -f $JUJU_DIR/streamsrc ]] || return 0
    [[ $JT_IGNORE_STREAMS == '1' ]] && return 0
    echo "Phase 5: Testing to streams.canonical.com."
    source $JUJU_DIR/streamsrc
    rsync -avzh $JUJU_DIST/ $STREAMS_TESTING_DEST
}


# The location of environments.yaml and rc files.
JUJU_DIR=${JUJU_HOME:-$HOME/.juju}

test $# -eq 2 || usage

PURPOSE=$1
if [[ $PURPOSE != "RELEASE" && $PURPOSE != "TESTING" ]]; then
    usage
fi

JUJU_DIST=$(cd $2; pwd)
if [[ ! -d $JUJU_DIST/tools/releases && ! -d $JUJU_DIST/tools/streams ]]; then
    usage
fi


check_deps
if [[ $PURPOSE == "RELEASE" ]]; then
    publish_to_streams
    publish_to_canonistack
    publish_to_hp
    publish_to_aws
    publish_to_azure
    echo "Release data published to all CPCs."
else
    testing_to_streams
    testing_to_canonistack
    testing_to_hp
    testing_to_aws
    testing_to_azure
    echo "Testing data published to all CPCs."
fi
