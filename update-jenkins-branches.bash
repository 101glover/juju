#!/bin/bash
# As a member of juju-qa,  Visit each the jenkins master and slaves
# and update their branches.
# passing 'true' as an arg will driect the script to try to update cloud-city.
set -eux

MASTER="juju-ci.vapour.ws"
SLAVES="precises-slave.vapour.ws trusty-slave.vapour.ws \
    utopic-slave-a.vapour.ws utopic-slave-b.vapour.ws vivid-slave.vapour.ws \
    ppc64el-slave.vapour.ws i386-slave.vapour.ws kvm-slave.vapour.ws \
    canonistack-slave.vapour.ws juju-core-slave.vapour.ws \
    charm-bundle-slave.vapour.ws"
KEY="staging-juju-rsa"
export JUJU_ENV="juju-ci3"


update_jenkins() {
    # Get the ip address which will most likely match historic ssh rules.
    host=$(host -t A $1 | cut -d ' ' -f4)
    echo "updating $host"
    if [[ "$CLOUD_CITY" == "true" ]]; then
        bzr branch lp:~juju-qa/+junk/cloud-city \
            bzr+ssh://jenkins@$host/var/lib/jenkins/cloud-city.new
    fi
    ssh jenkins@$host << EOT
#!/bin/bash
set -eux
if [[ "$CLOUD_CITY" == "true" ]]; then
    (cd ~/cloud-city; bzr revert; cd -)
    bzr pull -d ~/cloud-city ~/cloud-city.new
    rm -r ~/cloud-city.new
fi
cd ~/juju-release-tools
bzr pull
cd ~/repository
bzr pull
cd ~/juju-ci-tools
bzr pull
if [[ \$(uname) == "Linux" ]]; then
    make install-deps
fi
if [[ -d ~/ci-director ]]; then
    cd ~/ci-director
    bzr pull
fi
EOT
}


CLOUD_CITY="false"
while [[ "${1-}" != "" ]]; do
    case $1 in
        --cloud-city)
            CLOUD_CITY="true"
            ;;
    esac
    shift
done

SKIPPED=""
for host in $MASTER $SLAVES; do
    update_jenkins $host || SKIPPED="$SKIPPED $host"
done

if [[ -n "$SKIPPED" ]]; then
    set +x
    echo
    echo "These hosts were skipped because there was an error"
    echo "$SKIPPED"
fi

