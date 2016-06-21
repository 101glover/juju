// Copyright 2016 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package controller

import (
	"crypto/tls"
	"fmt"
	"io/ioutil"
	"net/url"
	"os"
	"path/filepath"

	"github.com/juju/errors"
	"github.com/juju/loggo"
	"github.com/juju/utils"
	"gopkg.in/juju/environschema.v1"
	"gopkg.in/macaroon-bakery.v1/bakery"

	"github.com/juju/juju/cert"
	"github.com/juju/juju/juju/osenv"
)

var logger = loggo.GetLogger("juju.controller")

const (
	// ApiPort is the port used for api connections.
	ApiPort = "api-port"

	// StatePort is the port used for mongo connections.
	StatePort = "state-port"

	// CACertKey is the key for the controller's CA certificate attribute.
	CACertKey = "ca-cert"

	// CAPrivateKey is the key for the controller's CA certificate private key.
	CAPrivateKey = "ca-private-key"

	// ControllerUUIDKey is the key for the controller UUID attribute.
	ControllerUUIDKey = "controller-uuid"

	// IdentityURL sets the url of the identity manager.
	IdentityURL = "identity-url"

	// IdentityPublicKey sets the public key of the identity manager.
	IdentityPublicKey = "identity-public-key"

	// NumaControlPolicyKey stores the value for this setting
	SetNumaControlPolicyKey = "set-numa-control-policy"

	// Attribute Defaults

	// DefaultNumaControlPolicy should not be used by default.
	// Only use numactl if user specifically requests it
	DefaultNumaControlPolicy = false

	// DefaultStatePort is the default port the controller is listening on.
	DefaultStatePort int = 37017

	// DefaultApiPort is the default port the API server is listening on.
	DefaultAPIPort int = 17070
)

// ControllerOnlyConfigAttributes are attributes which are only relevant
// for a controller, never a model.
var ControllerOnlyConfigAttributes = []string{
	ApiPort,
	StatePort,
	CACertKey,
	CAPrivateKey,
	ControllerUUIDKey,
	IdentityURL,
	IdentityPublicKey,
	SetNumaControlPolicyKey,
}

// ControllerOnlyAttribute returns true if the specified attribute name
// is only relevant for a controller.
func ControllerOnlyAttribute(attr string) bool {
	for _, a := range ControllerOnlyConfigAttributes {
		if attr == a {
			return true
		}
	}
	return false
}

type Config map[string]interface{}

// ControllerConfig returns the controller config attributes from cfg.
func ControllerConfig(cfg map[string]interface{}) Config {
	controllerCfg := make(map[string]interface{})
	for _, attr := range ControllerOnlyConfigAttributes {
		if val, ok := cfg[attr]; ok {
			controllerCfg[attr] = val
		}
	}
	return controllerCfg
}

// mustInt returns the named attribute as an integer, panicking if
// it is not found or is zero. Zero values should have been
// diagnosed at Validate time.
func (c Config) mustInt(name string) int {
	value, _ := c[name].(int)
	if value == 0 {
		panic(fmt.Errorf("empty value for %q found in configuration", name))
	}
	return value
}

// asString is a private helper method to keep the ugly string casting
// in once place. It returns the given named attribute as a string,
// returning "" if it isn't found.
func (c Config) asString(name string) string {
	value, _ := c[name].(string)
	return value
}

// mustString returns the named attribute as an string, panicking if
// it is not found or is empty.
func (c Config) mustString(name string) string {
	value, _ := c[name].(string)
	if value == "" {
		panic(fmt.Errorf("empty value for %q found in configuration (type %T, val %v)", name, c[name], c[name]))
	}
	return value
}

// StatePort returns the controller port for the environment.
func (c Config) StatePort() int {
	return c.mustInt(StatePort)
}

// APIPort returns the API server port for the environment.
func (c Config) APIPort() int {
	return c.mustInt(ApiPort)
}

// ControllerUUID returns the uuid for the model's controller.
func (c Config) ControllerUUID() string {
	return c.mustString(ControllerUUIDKey)
}

// CACert returns the certificate of the CA that signed the controller
// certificate, in PEM format, and whether the setting is available.
func (c Config) CACert() (string, bool) {
	if s, ok := c[CACertKey]; ok {
		return s.(string), true
	}
	return "", false
}

// CAPrivateKey returns the private key of the CA that signed the state
// server certificate, in PEM format, and whether the setting is available.
func (c Config) CAPrivateKey() (key string, ok bool) {
	if s, ok := c[CAPrivateKey]; ok && s != "" {
		return s.(string), true
	}
	return "", false
}

// IdentityURL returns the url of the identity manager.
func (c Config) IdentityURL() string {
	return c.asString(IdentityURL)
}

// IdentityPublicKey returns the public key of the identity manager.
func (c Config) IdentityPublicKey() *bakery.PublicKey {
	key := c.asString(IdentityPublicKey)
	if key == "" {
		return nil
	}
	var pubKey bakery.PublicKey
	err := pubKey.UnmarshalText([]byte(key))
	if err != nil {
		// We check if the key string can be unmarshalled into a PublicKey in the
		// Validate function, so we really do not expect this to fail.
		panic(err)
	}
	return &pubKey
}

// NumaCtlPreference returns if numactl is preferred.
func (c Config) NumaCtlPreference() bool {
	if numa, ok := c[SetNumaControlPolicyKey]; ok {
		return numa.(bool)
	}
	return DefaultNumaControlPolicy
}

// maybeReadAttrFromFile sets defined[attr] to:
//
// 1) The content of the file defined[attr+"-path"], if that's set
// 2) The value of defined[attr] if it is already set.
// 3) The content of defaultPath if it exists and defined[attr] is unset
// 4) Preserves the content of defined[attr], otherwise
//
// The defined[attr+"-path"] key is always deleted.
func maybeReadAttrFromFile(c Config, attr, defaultPath string) error {
	if !osenv.IsJujuXDGDataHomeSet() {
		logger.Debugf("JUJU_DATA not set, not attempting to read file %q", defaultPath)
		return nil
	}
	pathAttr := attr + "-path"
	path, _ := c[pathAttr].(string)
	delete(c, pathAttr)
	hasPath := path != ""
	if !hasPath {
		// No path and attribute is already set; leave it be.
		if s, _ := c[attr].(string); s != "" {
			return nil
		}
		path = defaultPath
	}
	path, err := utils.NormalizePath(path)
	if err != nil {
		return err
	}
	if !filepath.IsAbs(path) {
		path = osenv.JujuXDGDataHomePath(path)
	}
	data, err := ioutil.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) && !hasPath {
			// If the default path isn't found, it's
			// not an error.
			return nil
		}
		return err
	}
	if len(data) == 0 {
		return fmt.Errorf("file %q is empty", path)
	}
	c[attr] = string(data)
	return nil
}

// FillInDefaults adds any default config attributes.
func (c Config) FillInDefaults(name string) error {
	err := maybeReadAttrFromFile(c, CACertKey, name+"-cert.pem")
	if err != nil {
		return err
	}
	err = maybeReadAttrFromFile(c, CAPrivateKey, name+"-private-key.pem")
	if err != nil {
		return err
	}
	return nil
}

// Validate ensures that config is a valid configuration.
func Validate(c Config) error {
	if v, ok := c[IdentityURL].(string); ok {
		u, err := url.Parse(v)
		if err != nil {
			return fmt.Errorf("invalid identity URL: %v", err)
		}
		if u.Scheme != "https" {
			return fmt.Errorf("URL needs to be https")
		}

	}

	if v, ok := c[IdentityPublicKey].(string); ok {
		var key bakery.PublicKey
		if err := key.UnmarshalText([]byte(v)); err != nil {
			return fmt.Errorf("invalid identity public key: %v", err)
		}
	}

	caCert, caCertOK := c.CACert()
	caKey, caKeyOK := c.CAPrivateKey()
	if caCertOK || caKeyOK {
		if err := verifyKeyPair(caCert, caKey); err != nil {
			return errors.Annotate(err, "bad CA certificate/key in configuration")
		}
	}

	if uuid, ok := c[ControllerUUIDKey].(string); ok && !utils.IsValidUUIDString(uuid) {
		return errors.Errorf("controller-uuid: expected UUID, got string(%q)", uuid)
	}

	return nil
}

// verifyKeyPair verifies that the certificate and key parse correctly.
// The key is optional - if it is provided, we also check that the key
// matches the certificate.
func verifyKeyPair(certb, key string) error {
	if key != "" {
		_, err := tls.X509KeyPair([]byte(certb), []byte(key))
		return err
	}
	_, err := cert.ParseCert(certb)
	return err
}

// GenerateControllerCertAndKey makes sure that the config has a CACert and
// CAPrivateKey, generates and returns new certificate and key.
func GenerateControllerCertAndKey(caCert, caKey string, hostAddresses []string) (string, string, error) {
	return cert.NewDefaultServer(caCert, caKey, hostAddresses)
}

var ConfigSchema = environschema.Fields{
	ApiPort: {
		Description: "The TCP port for the API servers to listen on",
		Type:        environschema.Tint,
		Group:       environschema.EnvironGroup,
		Immutable:   true,
	},
	CACertKey: {
		Description: `The certificate of the CA that signed the controller certificate, in PEM format`,
		Type:        environschema.Tstring,
		Group:       environschema.EnvironGroup,
	},
	"ca-cert-path": {
		Description: "Path to file containing CA certificate",
		Type:        environschema.Tstring,
	},
	CAPrivateKey: {
		Description: `The private key of the CA that signed the controller certificate, in PEM format`,
		Type:        environschema.Tstring,
		Group:       environschema.EnvironGroup,
	},
	"ca-private-key-path": {
		Description: "Path to file containing CA private key",
		Type:        environschema.Tstring,
	},
	StatePort: {
		Description: "Port for the API server to listen on.",
		Type:        environschema.Tint,
		Immutable:   true,
		Group:       environschema.EnvironGroup,
	},
	ControllerUUIDKey: {
		Description: "The UUID of the model's controller",
		Type:        environschema.Tstring,
		Group:       environschema.JujuGroup,
		Immutable:   true,
	},
	SetNumaControlPolicyKey: {
		Description: "Tune Juju controller to work with NUMA if present (default false)",
		Type:        environschema.Tbool,
		Group:       environschema.EnvironGroup,
	},
	IdentityURL: {
		Description: "IdentityURL specifies the URL of the identity manager",
		Type:        environschema.Tstring,
		Group:       environschema.JujuGroup,
		Immutable:   true,
	},
	IdentityPublicKey: {
		Description: "Public key of the identity manager. If this is omitted, the public key will be fetched from the IdentityURL.",
		Type:        environschema.Tstring,
		Group:       environschema.JujuGroup,
		Immutable:   true,
	},
}
