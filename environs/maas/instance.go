// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package maas

import (
	"fmt"
	"launchpad.net/gomaasapi"
	"launchpad.net/juju-core/instance"
	"launchpad.net/juju-core/log"
)

type maasInstance struct {
	maasObject *gomaasapi.MAASObject
	environ    *maasEnviron
}

var _ instance.Instance = (*maasInstance)(nil)

func (mi *maasInstance) Id() instance.Id {
	// Use the node's 'resource_uri' value.
	return instance.Id((*mi.maasObject).URI().String())
}

func (mi *maasInstance) Metadata() (*instance.Metadata, error) {
	log.Debugf("environs/maas: unimplemented Metadata() called")
	return nil, fmt.Errorf("environs/maas: unimplemented Metadata() called")
}

// refreshInstance refreshes the instance with the most up-to-date information
// from the MAAS server.
func (mi *maasInstance) refreshInstance() error {
	insts, err := mi.environ.Instances([]instance.Id{mi.Id()})
	if err != nil {
		return err
	}
	newMaasObject := insts[0].(*maasInstance).maasObject
	mi.maasObject = newMaasObject
	return nil
}

func (mi *maasInstance) DNSName() (string, error) {
	hostname, err := (*mi.maasObject).GetField("hostname")
	if err != nil {
		return "", err
	}
	return hostname, nil
}

func (mi *maasInstance) WaitDNSName() (string, error) {
	// A MAAS nodes gets his DNS name when it's created.  WaitDNSName,
	// (same as DNSName) just returns the hostname of the node.
	return mi.DNSName()
}

// MAAS does not do firewalling so these port methods do nothing.
func (mi *maasInstance) OpenPorts(machineId string, ports []instance.Port) error {
	log.Debugf("environs/maas: unimplemented OpenPorts() called")
	return nil
}

func (mi *maasInstance) ClosePorts(machineId string, ports []instance.Port) error {
	log.Debugf("environs/maas: unimplemented ClosePorts() called")
	return nil
}

func (mi *maasInstance) Ports(machineId string) ([]instance.Port, error) {
	log.Debugf("environs/maas: unimplemented Ports() called")
	return []instance.Port{}, nil
}
