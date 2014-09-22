#!/bin/bash

# Assemble a source tree using git to checkout the revision,
# then go to get the package deps, then godeps to pin the versions/
# Lastly create a release tarball from the tree.
set -e

unset GOPATH
unset GOBIN

DEFAULT_GIT_JUJU_CORE="https://github.com/juju/juju.git"
PACKAGE="github.com/juju/juju"

SCRIPT_DIR=$(cd $(dirname "${BASH_SOURCE[0]}") && pwd )

usage() {
    echo "usage: $0 <GIT_REV> [GIT_REPO] [MERGE_REF] [MERGE_REPO] [MERGE_REV]"
    echo "  GIT_REV: The juju core git revision or branch to build"
    echo "  GIT_REPO: The juju core git repo; defaults to $DEFAULT_GIT_JUJU_CORE"
    echo "  MERGE_REF: The git branch or tag to merge"
    echo "  MERGE_REPO: The git repo that contains the MERGE_REF"
    echo "  MERGE_REV: The optional git specific commit to merge"
    exit 1
}


check_deps() {
    echo "Phase 0: Checking requirements."
    has_deps=1
    which bzr || has_deps=0
    which git || has_deps=0
    which hg || has_deps=0
    which go || has_deps=0
    if [[ $has_deps == 0 ]]; then
        echo "Install bzr, hg, git, and golang."
        exit 2
    fi
}


test $# -ge 1 ||  usage
check_deps

MERGE_REPO=""
MERGE_REF=""
MERGE_REV=""

REVISION=$1
JUJU_CORE_REPO=${2:-$DEFAULT_GIT_JUJU_CORE}
if [[ $# -ge 4 ]]; then
    MERGE_REF=$3
    MERGE_REPO=$4
    if [[ $# -eq 5 ]]; then
        MERGE_REV=$5
    fi
fi

HERE=$(pwd)
TMP_DIR=$(mktemp -d --tmpdir=$HERE)
mkdir $TMP_DIR/RELEASE
WORK=$TMP_DIR/RELEASE

echo "Getting juju core from $JUJU_CORE_REPO."
mkdir -p $WORK/src/$PACKAGE
git clone $JUJU_CORE_REPO $WORK/src/$PACKAGE
echo "Setting juju core tree to $REVISION."
cd $WORK/src/$PACKAGE
if git ls-remote ./  | grep origin/$REVISION; then
    git checkout origin/$REVISION
else
    git checkout $REVISION
fi
if [[ "$MERGE_REF" != "" && "$MERGE_REPO" != "" ]]; then
    if [[ "$MERGE_REV" != "" ]]; then
        echo "Merging $MERGE_REV in $MERGE_REF from $MERGE_REPO into $REVISION"
        git fetch $MERGE_REPO $MERGE_REF
        git merge $MERGE_REV
    else
        echo "Pulling $MERGE_REF from $MERGE_REPO into $REVISION"
        git pull $MERGE_REPO $MERGE_REF
    fi
fi
echo "Getting juju core's dependencies."
GOPATH=$WORK go get -v -d ./... || \
    GOPATH=$WORK go get -v -d ./... || \
    GOPATH=$WORK go get -v -d ./... 
cd $HERE

echo "Updating juju-core dependencies to the required versions."
GOPATH=$WORK go get -v launchpad.net/godeps
GODEPS=$WORK/bin/godeps
if [[ ! -f $GODEPS ]]; then
    echo "! Could not install godeps."
    exit 1
fi
GOPATH=$WORK $GODEPS -u "$WORK/src/$PACKAGE/dependencies.tsv"

# TODO(gz): Ideally just run ./scripts/pre-push.bash instead, but govet issues
BADFMT=$(find $WORK/src/$PACKAGE -name '*.go' -not -name '.#*' | xargs gofmt -l)
if [[ -n "$BADFMT" ]]; then
    BADFMT=$(echo "$BADFMT" | sed "s/^/  /")
    echo -e "gofmt is sad:\n\n$BADFMT"
    exit 1
fi

# Remove godeps, non-free data, and any binaries.
rm -r $WORK/src/launchpad.net/godeps
rm -r $WORK/src/github.com/kisielk/gotool
rm -r $WORK/src/code.google.com/p/go.net/html/charset/testdata/
rm $WORK/src/code.google.com/p/go.net/html/charset/*test.go
rm -r $WORK/bin
if [[ -d $WORK/pkg ]]; then
    rm -r $WORK/pkg
fi

# Validate the go src tree against dependencies.tsv
$SCRIPT_DIR/check_dependencies.py --ignore $PACKAGE \
    "$WORK/src/$PACKAGE/dependencies.tsv" "$WORK/src"

# Change the generic release to the proper juju-core version.
VERSION=$(sed -n 's/^const version = "\(.*\)"/\1/p' \
    $WORK/src/$PACKAGE/version/version.go)
mv $WORK $TMP_DIR/juju-core_${VERSION}/

# Tar it up.
TARFILE=$(pwd)/juju-core_${VERSION}.tar.gz
cd $TMP_DIR
tar cfz $TARFILE --exclude .hg --exclude .git --exclude .bzr juju-core_${VERSION}

echo "release tarball: $TARFILE"
