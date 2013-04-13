package config_test

import (
	"io/ioutil"
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/testing"
	"launchpad.net/juju-core/version"
	"os"
	"path/filepath"
	stdtesting "testing"
)

func Test(t *stdtesting.T) {
	TestingT(t)
}

type ConfigSuite struct {
	testing.LoggingSuite
	home string
}

var _ = Suite(&ConfigSuite{})

type attrs map[string]interface{}

type configTest struct {
	about string
	attrs map[string]interface{}
	err   string
}

var configTests = []configTest{
	{
		about: "The minimum good configuration",
		attrs: attrs{
			"type": "my-type",
			"name": "my-name",
		},
	}, {
		about: "Explicit series",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"default-series": "my-series",
		},
	}, {
		about: "Implicit series with empty value",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"default-series": "",
		},
	}, {
		about: "Explicit authorized-keys",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
		},
	}, {
		about: "Load authorized-keys from path",
		attrs: attrs{
			"type":                 "my-type",
			"name":                 "my-name",
			"authorized-keys-path": "~/.ssh/authorized_keys2",
		},
	}, {
		about: "CA cert & key from path",
		attrs: attrs{
			"type":                "my-type",
			"name":                "my-name",
			"ca-cert-path":        "cacert2.pem",
			"ca-private-key-path": "cakey2.pem",
		},
	}, {
		about: "CA cert & key from path; cert attribute set too",
		attrs: attrs{
			"type":                "my-type",
			"name":                "my-name",
			"ca-cert-path":        "cacert2.pem",
			"ca-cert":             "ignored",
			"ca-private-key-path": "cakey2.pem",
		},
	}, {
		about: "CA cert & key from ~ path",
		attrs: attrs{
			"type":                "my-type",
			"name":                "my-name",
			"ca-cert-path":        "~/othercert.pem",
			"ca-private-key-path": "~/otherkey.pem",
		},
	}, {
		about: "CA cert only from ~ path",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert-path":   "~/othercert.pem",
			"ca-private-key": "",
		},
	}, {
		about: "CA cert only as attribute",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        caCert,
			"ca-private-key": "",
		},
	}, {
		about: "CA cert and key as attributes",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        caCert,
			"ca-private-key": caKey,
		},
	}, {
		about: "Mismatched CA cert and key",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        caCert,
			"ca-private-key": caKey2,
		},
		err: "bad CA certificate/key in configuration: crypto/tls: private key does not match public key",
	}, {
		about: "Invalid CA cert",
		attrs: attrs{
			"type":    "my-type",
			"name":    "my-name",
			"ca-cert": invalidCACert,
		},
		err: "bad CA certificate/key in configuration: ASN.1 syntax error:.*",
	}, {
		about: "Invalid CA key",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        caCert,
			"ca-private-key": invalidCAKey,
		},
		err: "bad CA certificate/key in configuration: crypto/tls:.*",
	}, {
		about: "No CA cert or key",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        "",
			"ca-private-key": "",
		},
	}, {
		about: "CA key but no cert",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        "",
			"ca-private-key": caKey,
		},
		err: "bad CA certificate/key in configuration: crypto/tls:.*",
	}, {
		about: "No CA key",
		attrs: attrs{
			"type":           "my-type",
			"name":           "my-name",
			"ca-cert":        "foo",
			"ca-private-key": "",
		},
		err: "bad CA certificate/key in configuration: no certificates found",
	}, {
		about: "CA cert specified as non-existent file",
		attrs: attrs{
			"type":         "my-type",
			"name":         "my-name",
			"ca-cert-path": "no-such-file",
		},
		err: `open .*\.juju/no-such-file: .*`,
	}, {
		about: "CA key specified as non-existent file",
		attrs: attrs{
			"type":                "my-type",
			"name":                "my-name",
			"ca-private-key-path": "no-such-file",
		},
		err: `open .*\.juju/no-such-file: .*`,
	}, {
		about: "Specified agent version",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"agent-version":   "1.2.3",
		},
	}, {
		about: "Specified development flag",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"development":     true,
		},
	}, {
		about: "Specified admin secret",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"development":     false,
			"admin-secret":    "pork",
		},
	}, {
		about: "Invalid development flag",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"development":     "true",
		},
		err: "development: expected bool, got \"true\"",
	}, {
		about: "Invalid agent version",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"agent-version":   "2",
		},
		err: `invalid agent version in environment configuration: "2"`,
	}, {
		about: "Missing type",
		attrs: attrs{
			"name": "my-name",
		},
		err: "type: expected string, got nothing",
	}, {
		about: "Empty type",
		attrs: attrs{
			"name": "my-name",
			"type": "",
		},
		err: "empty type in environment configuration",
	}, {
		about: "Missing name",
		attrs: attrs{
			"type": "my-type",
		},
		err: "name: expected string, got nothing",
	}, {
		about: "Bad name, no slash",
		attrs: attrs{
			"name": "foo/bar",
			"type": "my-type",
		},
		err: "environment name contains unsafe characters",
	}, {
		about: "Bad name, no backslash",
		attrs: attrs{
			"name": "foo\\bar",
			"type": "my-type",
		},
		err: "environment name contains unsafe characters",
	}, {
		about: "Empty name",
		attrs: attrs{
			"type": "my-type",
			"name": "",
		},
		err: "empty name in environment configuration",
	}, {
		about: "Default firewall mode",
		attrs: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"firewall-mode": config.FwDefault,
		},
	}, {
		about: "Instance firewall mode",
		attrs: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"firewall-mode": config.FwInstance,
		},
	}, {
		about: "Global firewall mode",
		attrs: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"firewall-mode": config.FwGlobal,
		},
	}, {
		about: "Illegal firewall mode",
		attrs: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"firewall-mode": "illegal",
		},
		err: "invalid firewall mode in environment configuration: .*",
	}, {
		about: "ssl-hostname-verification off",
		attrs: attrs{
			"type": "my-type",
			"name": "my-name",
			"ssl-hostname-verification": false,
		},
	}, {
		about: "ssl-hostname-verification incorrect",
		attrs: attrs{
			"type": "my-type",
			"name": "my-name",
			"ssl-hostname-verification": "yes please",
		},
		err: `ssl-hostname-verification: expected bool, got "yes please"`,
	},
}

type testFile struct {
	name, data string
}

func (*ConfigSuite) TestConfig(c *C) {
	files := []testFile{
		{".ssh/id_dsa.pub", "dsa"},
		{".ssh/id_rsa.pub", "rsa\n"},
		{".ssh/identity.pub", "identity"},
		{".ssh/authorized_keys", "auth0\n# first\nauth1\n\n"},
		{".ssh/authorized_keys2", "auth2\nauth3\n"},

		{".juju/my-name-cert.pem", caCert},
		{".juju/my-name-private-key.pem", caKey},
		{".juju/cacert2.pem", caCert2},
		{".juju/cakey2.pem", caKey2},
		{"othercert.pem", caCert3},
		{"otherkey.pem", caKey3},
	}
	h := makeFakeHome(c, files)
	defer h.restore()
	for i, test := range configTests {
		c.Logf("test %d. %s", i, test.about)
		test.check(c, h)
	}
}

var noCertFilesTests = []configTest{
	{
		about: "Unspecified certificate and key",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
		},
	}, {
		about: "Unspecified certificate, specified key",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"ca-private-key":  caKey,
		},
		err: "bad CA certificate/key in configuration: crypto/tls:.*",
	},
}

func (*ConfigSuite) TestConfigNoCertFiles(c *C) {
	h := makeFakeHome(c, nil)
	defer h.restore()
	for i, test := range noCertFilesTests {
		c.Logf("test %d. %s", i, test.about)
		test.check(c, h)
	}
}

var emptyCertFilesTests = []configTest{
	{
		about: "Cert unspecified; key specified",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"ca-private-key":  caKey,
		},
		err: `bad CA certificate/key in configuration: crypto/tls: .*`,
	}, {
		about: "Cert and key unspecified",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
		},
		err: `bad CA certificate/key in configuration: crypto/tls: .*`,
	}, {
		about: "Cert specified, key unspecified",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"ca-cert":         caCert,
		},
		err: "bad CA certificate/key in configuration: crypto/tls: .*",
	}, {
		about: "Cert and key specified as absent",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"ca-cert":         "",
			"ca-private-key":  "",
		},
	}, {
		about: "Cert specified as absent",
		attrs: attrs{
			"type":            "my-type",
			"name":            "my-name",
			"authorized-keys": "my-keys",
			"ca-cert":         "",
		},
		err: "bad CA certificate/key in configuration: crypto/tls: .*",
	},
}

func (*ConfigSuite) TestConfigEmptyCertFiles(c *C) {
	files := []testFile{
		{".juju/my-name-cert.pem", ""},
		{".juju/my-name-private-key.pem", ""},
	}
	h := makeFakeHome(c, files)
	defer h.restore()

	for i, test := range emptyCertFilesTests {
		c.Logf("test %d. %s", i, test.about)
		test.check(c, h)
	}
}

func (test configTest) check(c *C, h fakeHome) {
	cfg, err := config.New(test.attrs)
	if test.err != "" {
		c.Check(cfg, IsNil)
		c.Assert(err, ErrorMatches, test.err)
		return
	}
	c.Assert(err, IsNil)

	typ, _ := test.attrs["type"].(string)
	name, _ := test.attrs["name"].(string)
	c.Assert(cfg.Type(), Equals, typ)
	c.Assert(cfg.Name(), Equals, name)
	agentVersion, ok := cfg.AgentVersion()
	if s := test.attrs["agent-version"]; s != nil {
		c.Assert(ok, Equals, true)
		c.Assert(agentVersion, Equals, version.MustParse(s.(string)))
	} else {
		c.Assert(ok, Equals, false)
		c.Assert(agentVersion, Equals, version.Zero)
	}

	dev, _ := test.attrs["development"].(bool)
	c.Assert(cfg.Development(), Equals, dev)

	if series, _ := test.attrs["default-series"].(string); series != "" {
		c.Assert(cfg.DefaultSeries(), Equals, series)
	} else {
		c.Assert(cfg.DefaultSeries(), Equals, config.DefaultSeries)
	}

	if m, _ := test.attrs["firewall-mode"].(string); m != "" {
		c.Assert(cfg.FirewallMode(), Equals, config.FirewallMode(m))
	}

	if secret, _ := test.attrs["admin-secret"].(string); secret != "" {
		c.Assert(cfg.AdminSecret(), Equals, secret)
	}

	if path, _ := test.attrs["authorized-keys-path"].(string); path != "" {
		c.Assert(cfg.AuthorizedKeys(), Equals, h.fileContents(c, path))
		c.Assert(cfg.AllAttrs()["authorized-keys-path"], Equals, nil)
	} else if keys, _ := test.attrs["authorized-keys"].(string); keys != "" {
		c.Assert(cfg.AuthorizedKeys(), Equals, keys)
	} else {
		// Content of all the files that are read by default.
		want := "dsa\nrsa\nidentity\n"
		c.Assert(cfg.AuthorizedKeys(), Equals, want)
	}

	cert, certPresent := cfg.CACert()
	if path, _ := test.attrs["ca-cert-path"].(string); path != "" {
		c.Assert(certPresent, Equals, true)
		c.Assert(string(cert), Equals, h.fileContents(c, path))
	} else if v, ok := test.attrs["ca-cert"].(string); v != "" {
		c.Assert(certPresent, Equals, true)
		c.Assert(string(cert), Equals, v)
	} else if ok {
		c.Check(cert, HasLen, 0)
		c.Assert(certPresent, Equals, false)
	} else if h.fileExists(".juju/my-name-cert.pem") {
		c.Assert(certPresent, Equals, true)
		c.Assert(string(cert), Equals, h.fileContents(c, "my-name-cert.pem"))
	} else {
		c.Check(cert, HasLen, 0)
		c.Assert(certPresent, Equals, false)
	}

	key, keyPresent := cfg.CAPrivateKey()
	if path, _ := test.attrs["ca-private-key-path"].(string); path != "" {
		c.Assert(keyPresent, Equals, true)
		c.Assert(string(key), Equals, h.fileContents(c, path))
	} else if v, ok := test.attrs["ca-private-key"].(string); v != "" {
		c.Assert(keyPresent, Equals, true)
		c.Assert(string(key), Equals, v)
	} else if ok {
		c.Check(key, HasLen, 0)
		c.Assert(keyPresent, Equals, false)
	} else if h.fileExists(".juju/my-name-private-key.pem") {
		c.Assert(keyPresent, Equals, true)
		c.Assert(string(key), Equals, h.fileContents(c, "my-name-private-key.pem"))
	} else {
		c.Check(key, HasLen, 0)
		c.Assert(keyPresent, Equals, false)
	}

	if v, ok := test.attrs["ssl-hostname-verification"]; ok {
		c.Assert(cfg.SSLHostnameVerification(), Equals, v)
	}
}

func (*ConfigSuite) TestConfigAttrs(c *C) {
	attrs := map[string]interface{}{
		"type":                      "my-type",
		"name":                      "my-name",
		"authorized-keys":           "my-keys",
		"firewall-mode":             string(config.FwDefault),
		"admin-secret":              "foo",
		"unknown":                   "my-unknown",
		"ca-private-key":            "",
		"ca-cert":                   caCert,
		"ssl-hostname-verification": true,
	}
	cfg, err := config.New(attrs)
	c.Assert(err, IsNil)

	// These attributes are added if not set.
	attrs["development"] = false
	attrs["default-series"] = config.DefaultSeries
	// Default firewall mode is instance
	attrs["firewall-mode"] = string(config.FwInstance)
	c.Assert(cfg.AllAttrs(), DeepEquals, attrs)
	c.Assert(cfg.UnknownAttrs(), DeepEquals, map[string]interface{}{"unknown": "my-unknown"})

	newcfg, err := cfg.Apply(map[string]interface{}{
		"name":        "new-name",
		"new-unknown": "my-new-unknown",
	})

	attrs["name"] = "new-name"
	attrs["new-unknown"] = "my-new-unknown"
	c.Assert(newcfg.AllAttrs(), DeepEquals, attrs)
}

type validationTest struct {
	about string
	new   attrs
	old   attrs
	err   string
}

var validationTests = []validationTest{
	{
		about: "Can't change the type",
		new: attrs{
			"type": "type2",
			"name": "my-name",
		},
		old: attrs{
			"type": "my-type",
			"name": "my-name",
		},
		err: `cannot change type from "my-type" to "type2"`,
	}, {
		about: "Can't change the name",
		new: attrs{
			"type": "my-type",
			"name": "new-name",
		},
		old: attrs{
			"type": "my-type",
			"name": "my-name",
		},
		err: `cannot change name from "my-name" to "new-name"`,
	}, {
		about: "Can set agent version",
		new: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"agent-version": "1.9.13",
		},
		old: attrs{
			"type": "my-type",
			"name": "my-name",
		},
	}, {
		about: "Can't clear agent version",
		new: attrs{
			"type": "my-type",
			"name": "my-name",
		},
		old: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"agent-version": "1.9.13",
		},
		err: `cannot clear agent-version`,
	}, {
		about: "Can't change the firewall-mode",
		new: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"firewall-mode": config.FwInstance,
		},
		old: attrs{
			"type":          "my-type",
			"name":          "my-name",
			"firewall-mode": config.FwGlobal,
		},
		err: `cannot change firewall-mode from "global" to "instance"`,
	},
}

func (*ConfigSuite) TestValidateChange(c *C) {
	files := []testFile{
		{".ssh/identity.pub", "identity"},
	}
	h := makeFakeHome(c, files)
	defer h.restore()

	for i, test := range validationTests {
		c.Logf("test %d. %s", i, test.about)
		newConfig, err := config.New(test.new)
		c.Assert(err, IsNil)
		oldConfig, err := config.New(test.old)
		c.Assert(err, IsNil)

		err = config.Validate(newConfig, oldConfig)
		if test.err == "" {
			c.Assert(err, IsNil)
		} else {
			c.Assert(err, ErrorMatches, test.err)
		}
	}
}

type fakeHome struct {
	oldHome     string
	oldJujuHome string
	files       []testFile
}

func makeFakeHome(c *C, files []testFile) fakeHome {
	oldHome := os.Getenv("HOME")
	homeDir := filepath.Join(c.MkDir(), "me")
	for _, f := range files {
		path := filepath.Join(homeDir, f.name)
		err := os.MkdirAll(filepath.Dir(path), 0700)
		c.Assert(err, IsNil)
		err = ioutil.WriteFile(path, []byte(f.data), 0666)
		c.Assert(err, IsNil)
	}
	os.Setenv("HOME", homeDir)
	oldJujuHome := config.SetJujuHome(filepath.Join(homeDir, ".juju"))
	return fakeHome{oldHome, oldJujuHome, files}
}

func (h fakeHome) restore() {
	config.SetJujuHome(h.oldJujuHome)
	os.Setenv("HOME", h.oldHome)
}

// fileContents returns the test file contents for the
// given specified path (which may be relative, so
// we compare with the base filename only).
func (h fakeHome) fileContents(c *C, path string) string {
	for _, f := range h.files {
		if filepath.Base(f.name) == filepath.Base(path) {
			return f.data
		}
	}
	c.Fatalf("path attribute holds unknown test file: %q", path)
	panic("unreachable")
}

// fileExists returns if the given relative file path exists
// in the fake home.
func (h fakeHome) fileExists(path string) bool {
	for _, f := range h.files {
		if f.name == path {
			return true
		}
	}
	return false
}

var caCert = `
-----BEGIN CERTIFICATE-----
MIIBjDCCATigAwIBAgIBADALBgkqhkiG9w0BAQUwHjENMAsGA1UEChMEanVqdTEN
MAsGA1UEAxMEcm9vdDAeFw0xMjExMDkxNjQwMjhaFw0yMjExMDkxNjQ1MjhaMB4x
DTALBgNVBAoTBGp1anUxDTALBgNVBAMTBHJvb3QwWTALBgkqhkiG9w0BAQEDSgAw
RwJAduA1Gnb2VJLxNGfG4St0Qy48Y3q5Z5HheGtTGmti/FjlvQvScCFGCnJG7fKA
Knd7ia3vWg7lxYkIvMPVP88LAQIDAQABo2YwZDAOBgNVHQ8BAf8EBAMCAKQwEgYD
VR0TAQH/BAgwBgEB/wIBATAdBgNVHQ4EFgQUlvKX8vwp0o+VdhdhoA9O6KlOm00w
HwYDVR0jBBgwFoAUlvKX8vwp0o+VdhdhoA9O6KlOm00wCwYJKoZIhvcNAQEFA0EA
LlNpevtFr8gngjAFFAO/FXc7KiZcCrA5rBfb/rEy297lIqmKt5++aVbLEPyxCIFC
r71Sj63TUTFWtRZAxvn9qQ==
-----END CERTIFICATE-----
`[1:]

var caKey = `
-----BEGIN RSA PRIVATE KEY-----
MIIBOQIBAAJAduA1Gnb2VJLxNGfG4St0Qy48Y3q5Z5HheGtTGmti/FjlvQvScCFG
CnJG7fKAKnd7ia3vWg7lxYkIvMPVP88LAQIDAQABAkEAsFOdMSYn+AcF1M/iBfjo
uQWJ+Zz+CgwuvumjGNsUtmwxjA+hh0fCn0Ah2nAt4Ma81vKOKOdQ8W6bapvsVDH0
6QIhAJOkLmEKm4H5POQV7qunRbRsLbft/n/SHlOBz165WFvPAiEAzh9fMf70std1
sVCHJRQWKK+vw3oaEvPKvkPiV5ui0C8CIGNsvybuo8ald5IKCw5huRlFeIxSo36k
m3OVCXc6zfwVAiBnTUe7WcivPNZqOC6TAZ8dYvdWo4Ifz3jjpEfymjid1wIgBIJv
ERPyv2NQqIFQZIyzUP7LVRIWfpFFOo9/Ww/7s5Y=
-----END RSA PRIVATE KEY-----
`[1:]

var caCert2 = `
-----BEGIN CERTIFICATE-----
MIIBjTCCATmgAwIBAgIBADALBgkqhkiG9w0BAQUwHjENMAsGA1UEChMEanVqdTEN
MAsGA1UEAxMEcm9vdDAeFw0xMjExMDkxNjQxMDhaFw0yMjExMDkxNjQ2MDhaMB4x
DTALBgNVBAoTBGp1anUxDTALBgNVBAMTBHJvb3QwWjALBgkqhkiG9w0BAQEDSwAw
SAJBAJkSWRrr81y8pY4dbNgt+8miSKg4z6glp2KO2NnxxAhyyNtQHKvC+fJALJj+
C2NhuvOv9xImxOl3Hg8fFPCXCtcCAwEAAaNmMGQwDgYDVR0PAQH/BAQDAgCkMBIG
A1UdEwEB/wQIMAYBAf8CAQEwHQYDVR0OBBYEFOsX/ZCqKzWCAaTTVcWsWKT5Msow
MB8GA1UdIwQYMBaAFOsX/ZCqKzWCAaTTVcWsWKT5MsowMAsGCSqGSIb3DQEBBQNB
AAVV57jetEzJQnjgBzhvx/UwauFn78jGhXfV5BrQmxIb4SF4DgSCFstPwUQOAr8h
XXzJqBQH92KYmp+y3YXDoMQ=
-----END CERTIFICATE-----
`[1:]

var caKey2 = `
-----BEGIN RSA PRIVATE KEY-----
MIIBOQIBAAJBAJkSWRrr81y8pY4dbNgt+8miSKg4z6glp2KO2NnxxAhyyNtQHKvC
+fJALJj+C2NhuvOv9xImxOl3Hg8fFPCXCtcCAwEAAQJATQNzO11NQvJS5U6eraFt
FgSFQ8XZjILtVWQDbJv8AjdbEgKMHEy33icsAKIUAx8jL9kjq6K9kTdAKXZi9grF
UQIhAPD7jccIDUVm785E5eR9eisq0+xpgUIa24Jkn8cAlst5AiEAopxVFl1auer3
GP2In3pjdL4ydzU/gcRcYisoJqwHpM8CIHtqmaXBPeq5WT9ukb5/dL3+5SJCtmxA
jQMuvZWRe6khAiBvMztYtPSDKXRbCZ4xeQ+kWSDHtok8Y5zNoTeu4nvDrwIgb3Al
fikzPveC5g6S6OvEQmyDz59tYBubm2XHgvxqww0=
-----END RSA PRIVATE KEY-----
`[1:]

var caCert3 = `
-----BEGIN CERTIFICATE-----
MIIBjTCCATmgAwIBAgIBADALBgkqhkiG9w0BAQUwHjENMAsGA1UEChMEanVqdTEN
MAsGA1UEAxMEcm9vdDAeFw0xMjExMDkxNjQxMjlaFw0yMjExMDkxNjQ2MjlaMB4x
DTALBgNVBAoTBGp1anUxDTALBgNVBAMTBHJvb3QwWjALBgkqhkiG9w0BAQEDSwAw
SAJBAIW7CbHFJivvV9V6mO8AGzJS9lqjUf6MdEPsdF6wx2Cpzr/lSFIggCwRA138
9MuFxflxb/3U8Nq+rd8rVtTgFMECAwEAAaNmMGQwDgYDVR0PAQH/BAQDAgCkMBIG
A1UdEwEB/wQIMAYBAf8CAQEwHQYDVR0OBBYEFJafrxqByMN9BwGfcmuF0Lw/1QII
MB8GA1UdIwQYMBaAFJafrxqByMN9BwGfcmuF0Lw/1QIIMAsGCSqGSIb3DQEBBQNB
AHq3vqNhxya3s33DlQfSj9whsnqM0Nm+u8mBX/T76TF5rV7+B33XmYzSyfA3yBi/
zHaUR/dbHuiNTO+KXs3/+Y4=
-----END CERTIFICATE-----
`[1:]

var caKey3 = `
-----BEGIN RSA PRIVATE KEY-----
MIIBOgIBAAJBAIW7CbHFJivvV9V6mO8AGzJS9lqjUf6MdEPsdF6wx2Cpzr/lSFIg
gCwRA1389MuFxflxb/3U8Nq+rd8rVtTgFMECAwEAAQJAaivPi4qJPrJb2onl50H/
VZnWKqmljGF4YQDWduMEt7GTPk+76x9SpO7W4gfY490Ivd9DEXfbr/KZqhwWikNw
LQIhALlLfRXLF2ZfToMfB1v1v+jith5onAu24O68mkdRc5PLAiEAuMJ/6U07hggr
Ckf9OT93wh84DK66h780HJ/FUHKcoCMCIDsPZaJBpoa50BOZG0ZjcTTwti3BGCPf
uZg+w0oCGz27AiEAsUCYKqEXy/ymHhT2kSecozYENdajyXvcaOG3EPkD3nUCICOP
zatzs7c/4mx4a0JBG6Za0oEPUcm2I34is50KSohz
-----END RSA PRIVATE KEY-----
`[1:]

var invalidCAKey = `
-----BEGIN RSA PRIVATE KEY-----
MIIBOgIBAAJAZabKgKInuOxj5vDWLwHHQtK3/45KB+32D15w94Nt83BmuGxo90lw
-----END RSA PRIVATE KEY-----
`[1:]

var invalidCACert = `
-----BEGIN CERTIFICATE-----
MIIBOgIBAAJAZabKgKInuOxj5vDWLwHHQtK3/45KB+32D15w94Nt83BmuGxo90lw
-----END CERTIFICATE-----
`[1:]
