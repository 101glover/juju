// Copyright 2016 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

// Package modelmanager provides the business logic for
// model management operations in the controller.
package modelmanager

import (
	"github.com/juju/errors"
	"github.com/juju/loggo"
	"github.com/juju/utils"

	"github.com/juju/juju/environs"
	"github.com/juju/juju/environs/config"
	"github.com/juju/juju/tools"
	"github.com/juju/juju/version"
)

var (
	logger = loggo.GetLogger("juju.controller.modelmanager")

	configValuesFromController = []string{
		"type",
		"ca-cert",
		"state-port",
		"api-port",
		config.ControllerUUIDKey,
	}
)

// ModelConfigCreator provides a method of creating a new model config.
//
// The zero value of ModelConfigCreator is usable with the limitations
// noted on each struct field.
type ModelConfigCreator struct {
	// FindTools, if non-nil, will be used to validate the agent-version
	// value in NewModelConfig if it differs from the base configuration.
	//
	// If FindTools is nil, agent-version may not be different to the
	// base configuration.
	FindTools func(version.Number) (tools.List, error)
}

// NewModelConfig returns a new model config given a base (controller) config
// and a set of attributes that will be specific to the new model, overriding
// any non-restricted attributes in the base configuration. The resulting
// config will be suitable for creating a new model in state.
//
// If "attrs" does not include a UUID, a new, random one will be generated
// and added to the config.
//
// The config will be validated with the provider before being returned.
func (c ModelConfigCreator) NewModelConfig(
	base *config.Config,
	attrs map[string]interface{},
) (*config.Config, error) {

	if err := c.checkVersion(base, attrs); err != nil {
		return nil, errors.Trace(err)
	}

	// Before comparing any values, we need to push the config through
	// the provider validation code. One of the reasons for this is that
	// numbers being serialized through JSON get turned into float64. The
	// schema code used in config will convert these back into integers.
	// However, before we can create a valid config, we need to make sure
	// we copy across fields from the main config that aren't there.
	baseAttrs := base.AllAttrs()
	restrictedFields, err := RestrictedProviderFields(base.Type())
	if err != nil {
		return nil, errors.Trace(err)
	}
	for _, field := range restrictedFields {
		if _, ok := attrs[field]; !ok {
			if baseValue, ok := baseAttrs[field]; ok {
				attrs[field] = baseValue
			}
		}
	}

	// Generate a new UUID for the model as necessary,
	// and finalize the new config.
	if _, ok := attrs[config.UUIDKey]; !ok {
		uuid, err := utils.NewUUID()
		if err != nil {
			return nil, errors.Trace(err)
		}
		attrs[config.UUIDKey] = uuid.String()
	}
	cfg, err := finalizeConfig(attrs)
	if err != nil {
		return nil, errors.Trace(err)
	}
	attrs = cfg.AllAttrs()

	// Any values that would normally be copied from the controller
	// config can also be defined, but if they differ from the controller
	// values, an error is returned.
	for _, field := range restrictedFields {
		if value, ok := attrs[field]; ok {
			if serverValue := baseAttrs[field]; value != serverValue {
				return nil, errors.Errorf(
					"specified %s \"%v\" does not match controller \"%v\"",
					field, value, serverValue)
			}
		}
	}
	return cfg, nil
}

func (c *ModelConfigCreator) checkVersion(base *config.Config, attrs map[string]interface{}) error {
	baseVersion, ok := base.AgentVersion()
	if !ok {
		return errors.Errorf("agent-version not found in base config")
	}

	// If there is no agent-version specified, use the current version.
	// otherwise we need to check for tools
	value, ok := attrs["agent-version"]
	if !ok {
		attrs["agent-version"] = baseVersion.String()
		return nil
	}
	versionStr, ok := value.(string)
	if !ok {
		return errors.Errorf("agent-version must be a string but has type '%T'", value)
	}
	versionNumber, err := version.Parse(versionStr)
	if err != nil {
		return errors.Trace(err)
	}

	n := versionNumber.Compare(baseVersion)
	switch {
	case n > 0:
		return errors.Errorf(
			"agent-version (%s) cannot be greater than the controller (%s)",
			versionNumber, baseVersion,
		)
	case n == 0:
		// If the version is the same as the base config,
		// then assume tools are available.
		return nil
	case n < 0:
		if c.FindTools == nil {
			return errors.New(
				"agent-version does not match base config, " +
					"and no tools-finder is supplied",
			)
		}
	}

	// Look to see if we have tools available for that version.
	list, err := c.FindTools(versionNumber)
	if err != nil {
		return errors.Trace(err)
	}
	if len(list) == 0 {
		return errors.Errorf("no tools found for version %s", versionNumber)
	}
	logger.Tracef("found tools: %#v", list)
	return nil
}

// RestrictedProviderFields returns the set of config fields that may not be
// overridden.
func RestrictedProviderFields(providerType string) ([]string, error) {
	provider, err := environs.Provider(providerType)
	if err != nil {
		return nil, errors.Trace(err)
	}
	var fields []string
	fields = append(fields, configValuesFromController...)
	fields = append(fields, provider.RestrictedConfigAttributes()...)
	return fields, nil
}

// finalizeConfig creates the config object from attributes, calls
// PrepareForCreateEnvironment, and then finally validates the config
// before returning it.
func finalizeConfig(attrs map[string]interface{}) (*config.Config, error) {
	cfg, err := config.New(config.UseDefaults, attrs)
	if err != nil {
		return nil, errors.Annotate(err, "creating config from values failed")
	}
	provider, err := environs.Provider(cfg.Type())
	if err != nil {
		return nil, errors.Trace(err)
	}
	cfg, err = provider.PrepareForCreateEnvironment(cfg)
	if err != nil {
		return nil, errors.Trace(err)
	}
	cfg, err = provider.Validate(cfg, nil)
	if err != nil {
		return nil, errors.Annotate(err, "provider validation failed")
	}
	return cfg, nil
}
