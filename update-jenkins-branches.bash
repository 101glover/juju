#!/bin/bash
# As a member of juju-qa,  Visit each the jenkins master and slaves
# and update their branches.
# passing 'true' as an arg will driect the script to try to update cloud-city.
set -eux

MASTER="juju-ci.vapour.ws"
SLAVES="precise-slave.vapour.ws trusty-slave.vapour.ws \
    vivid-slave.vapour.ws wily-slave.vapour.ws \
    ppc64el-slave.vapour.ws i386-slave.vapour.ws kvm-slave.vapour.ws \
    canonistack-slave.vapour.ws juju-core-slave.vapour.ws \
    cloud-health.vapour.ws certification-slave.vapour.ws \
    charm-bundle-slave.vapour.ws osx-slave.vapour.ws"
KEY="staging-juju-rsa"
export JUJU_ENV="juju-ci3"

update_jenkins() {
    # Get the ip address which will most likely match historic ssh rules.
    hostname=$1
    if [[ $hostname == $MASTER ]]; then
        # Bypass DNS which points to the apache front-end.
        host="54.86.142.177"
    else
        host=$(host -4 -t A $hostname 8.8.8.8 | tail -1 | cut -d ' ' -f4)
    fi
    echo "updating $hostname at $host"
    if [[ "$CLOUD_CITY" == "true" ]]; then
        bzr branch lp:~juju-qa/+junk/cloud-city \
            bzr+ssh://jenkins@$host/var/lib/jenkins/cloud-city.new
    fi
    ssh jenkins@$host << EOT
#!/bin/bash
export PATH=/usr/local/bin:\$HOME/Bin:\$PATH
set -eux
if [[ "$CLOUD_CITY" == "true" ]]; then
    (cd ~/cloud-city; bzr revert; cd -)
    bzr pull -d ~/cloud-city ~/cloud-city.new
    rm -r ~/cloud-city.new
    chmod 700 ~/cloud-city
    chmod 700 ~/cloud-city/gnupg
    chmod 600 ~/cloud-city/staging-juju-rsa*
fi

bzr pull -d ~/juju-release-tools
bzr pull -d ~/repository
bzr pull -d ~/juju-ci-tools
if [[ ! -e ~/workspace-runner ]]; then
    bzr branch http://bazaar.launchpad.net/~juju-qa/workspace-runner/trunk/\
      ~/workspace-runner
fi
bzr pull -d ~/workspace-runner
if [[ \$(uname) == "Linux" ]]; then
    cd ~/juju-ci-tools
    make install-deps
fi
if [[ -d ~/ci-director ]]; then
    bzr pull -d ~/ci-director
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

