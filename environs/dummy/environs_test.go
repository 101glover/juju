package dummy_test

import (
	. "launchpad.net/gocheck"
	_ "launchpad.net/juju-core/environs/dummy"
	"launchpad.net/juju-core/environs/jujutest"
	"launchpad.net/juju-core/testing"
	stdtesting "testing"
)

func init() {
	attrs := map[string]interface{}{
		"name":            "only",
		"type":            "dummy",
		"state-server":    true,
		"secret":          "pork",
		"admin-secret":    "fish",
		"authorized-keys": "foo",
		"ca-cert":         testing.CACert,
		"ca-private-key":  testing.CAKeyPEM,
	}
	Suite(&jujutest.LiveTests{
		Config:         attrs,
		CanOpenState:   true,
		HasProvisioner: false,
	})
	Suite(&jujutest.Tests{
		Config: attrs,
	})
}

func TestSuite(t *stdtesting.T) {
	testing.MgoTestPackage(t)
}
