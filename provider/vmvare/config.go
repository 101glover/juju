// Copyright 2014 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package vmware

import (
	"fmt"
	"net/url"

	"github.com/juju/errors"
	"github.com/juju/schema"

	"github.com/juju/juju/environs/config"
)

// The vmware-specific config keys.
const (
	cfgDatastore    = "datastore"
	cfgDatacenter   = "datacenter"
	cfgResourcePool = "resource-pool"
	cfgHost         = "host"
	cfgUser         = "user"
	cfgPassword     = "password"
)

// boilerplateConfig will be shown in help output, so please keep it up to
// date when you change environment configuration below.
var boilerplateConfig = `
vmware:
//  Some description
`[1:]

// configFields is the spec for each GCE config value's type.
var configFields = schema.Fields{
	cfgHost:         schema.String(),
	cfgUser:         schema.String(),
	cfgPassword:     schema.String(),
	cfgDatastore:    schema.String(),
	cfgDatacenter:   schema.String(),
	cfgResourcePool: schema.String(),
}

var configDefaults = schema.Defaults{}

var configSecretFields = []string{
	cfgPassword,
}

var configImmutableFields = []string{
	cfgHost,
	cfgDatacenter,
	cfgDatastore,
	cfgResourcePool,
}

type environConfig struct {
	*config.Config
	attrs map[string]interface{}
}

// newConfig builds a new environConfig from the provided Config and
// returns it.
func newConfig(cfg *config.Config) *environConfig {
	return &environConfig{
		Config: cfg,
		attrs:  cfg.UnknownAttrs(),
	}
}

// prepareConfig builds a new environConfig from the provided Config and
// returns it. This includes some GCE-specific updates (including OS
// environment variables) and applying default values. The resulting
// config values are validated.
func prepareConfig(cfg *config.Config) (*environConfig, error) {
	// Finish the config.
	ecfg, err := newValidConfig(cfg, configDefaults)
	return ecfg, errors.Trace(err)
}

// newValidConfig builds a new environConfig from the provided Config
// and returns it. This includes applying the provided defaults
// values, if any. The resulting config values are validated.
func newValidConfig(cfg *config.Config, defaults map[string]interface{}) (*environConfig, error) {
	// Ensure that the provided config is valid.
	if err := config.Validate(cfg, nil); err != nil {
		return nil, errors.Trace(err)
	}

	// Apply the defaults and coerce/validate the custom config attrs.
	validated, err := cfg.ValidateUnknownAttrs(configFields, defaults)
	if err != nil {
		return nil, errors.Trace(err)
	}
	validCfg, err := cfg.Apply(validated)
	if err != nil {
		return nil, errors.Trace(err)
	}

	// Build the config.
	ecfg := newConfig(validCfg)

	// Do final validation.
	if err := ecfg.validate(); err != nil {
		return nil, errors.Trace(err)
	}

	return ecfg, nil
}

func (c *environConfig) datastore() string {
	return c.attrs[cfgDatastore].(string)
}

func (c *environConfig) datacenter() string {
	return c.attrs[cfgDatacenter].(string)
}

func (c *environConfig) resourcePool() string {
	return c.attrs[cfgResourcePool].(string)
}

func (c *environConfig) host() string {
	return c.attrs[cfgHost].(string)
}

func (c *environConfig) user() string {
	return c.attrs[cfgUser].(string)
}

func (c *environConfig) password() string {
	return c.attrs[cfgPassword].(string)
}

func (c *environConfig) url() (*url.URL, error) {
	return url.Parse(fmt.Sprintf("https://%s:%s@%s/sdk", c.user(), c.password(), c.host()))
}

// secret gathers the "secret" config values and returns them.
func (c *environConfig) secret() map[string]string {
	secretAttrs := make(map[string]string, len(configSecretFields))
	for _, key := range configSecretFields {
		secretAttrs[key] = c.attrs[key].(string)
	}
	return secretAttrs
}

// validate checks vmware-specific config values.
func (c environConfig) validate() error {
	// All fields must be populated, even with just the default.
	for field := range configFields {
		if c.attrs[field].(string) == "" {
			return errors.Errorf("%s: must not be empty", field)
		}
	}
	if _, err := c.url(); err != nil {
		return errors.Trace(err)
	}

	return nil
}

// update applies changes from the provided config to the env config.
// Changes to any immutable attributes result in an error.
func (c *environConfig) update(cfg *config.Config) error {
	// Validate the updates. newValidConfig does not modify the "known"
	// config attributes so it is safe to call Validate here first.
	if err := config.Validate(cfg, c.Config); err != nil {
		return errors.Trace(err)
	}

	updates, err := newValidConfig(cfg, configDefaults)
	if err != nil {
		return errors.Trace(err)
	}

	// Check that no immutable fields have changed.
	attrs := updates.UnknownAttrs()
	for _, field := range configImmutableFields {
		if attrs[field] != c.attrs[field] {
			return errors.Errorf("%s: cannot change from %v to %v", field, c.attrs[field], attrs[field])
		}
	}

	// Apply the updates.
	c.Config = cfg
	c.attrs = cfg.UnknownAttrs()
	return nil
}
