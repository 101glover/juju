// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package azure

import (
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/testing"
	"sync"
)

type EnvironSuite struct {
	ProviderSuite
}

var _ = Suite(new(EnvironSuite))

func makeEnviron(c *C) *azureEnviron {
	attrs := makeAzureConfigMap(c)
	cfg, err := config.New(attrs)
	c.Assert(err, IsNil)
	ecfg, err := azureEnvironProvider{}.newConfig(cfg)
	c.Assert(err, IsNil)
	return &azureEnviron{
		name: "env",
		ecfg: ecfg,
	}
}

func (EnvironSuite) TestGetSnapshot(c *C) {
	original := azureEnviron{name: "this-env", ecfg: new(azureEnvironConfig)}
	snapshot := original.getSnapshot()

	// The snapshot is identical to the original.
	c.Check(*snapshot, DeepEquals, original)

	// However, they are distinct objects.
	c.Check(snapshot, Not(Equals), &original)

	// It's a shallow copy; they still share pointers.
	c.Check(snapshot.ecfg, Equals, original.ecfg)

	// Neither object is locked at the end of the copy.
	c.Check(original.Mutex, Equals, sync.Mutex{})
	c.Check(snapshot.Mutex, Equals, sync.Mutex{})
}

func (EnvironSuite) TestGetSnapshotLocksEnviron(c *C) {
	original := azureEnviron{}
	testing.TestLockingFunction(&original.Mutex, func() { original.getSnapshot() })
}

func (EnvironSuite) TestName(c *C) {
	env := azureEnviron{name: "foo"}
	c.Check(env.Name(), Equals, env.name)
}

func (EnvironSuite) TestConfigReturnsConfig(c *C) {
	cfg := new(config.Config)
	ecfg := azureEnvironConfig{Config: cfg}
	env := azureEnviron{ecfg: &ecfg}
	c.Check(env.Config(), Equals, cfg)
}

func (EnvironSuite) TestConfigLocksEnviron(c *C) {
	env := azureEnviron{name: "env", ecfg: new(azureEnvironConfig)}
	testing.TestLockingFunction(&env.Mutex, func() { env.Config() })
}

func (EnvironSuite) TestGetManagementAPI(c *C) {
	env := makeEnviron(c)
	context, err := env.getManagementAPI()
	c.Assert(err, IsNil)
	defer env.releaseManagementAPI(context)
	c.Check(context, NotNil)
	c.Check(context.ManagementAPI, NotNil)
	c.Check(context.certFile, NotNil)
}

func (EnvironSuite) TestReleaseManagementAPIAcceptsNil(c *C) {
	env := makeEnviron(c)
	env.releaseManagementAPI(nil)
	// The real test is that this does not panic.
}

func (EnvironSuite) TestReleaseManagementAPIAcceptsIncompleteContext(c *C) {
	env := makeEnviron(c)
	context := azureManagementContext{
		ManagementAPI: nil,
		certFile:      nil,
	}
	env.releaseManagementAPI(&context)
	// The real test is that this does not panic.
}

func (EnvironSuite) TestStorage(c *C) {
	env := makeEnviron(c)
	baseStorage := env.Storage()
	storage, ok := baseStorage.(*azureStorage)
	c.Check(ok, Equals, true)
	c.Assert(storage, NotNil)
	c.Check(storage.storageContext.getContainer(), Equals, env.ecfg.StorageContainerName())
	context, err := storage.getStorageContext()
	c.Assert(err, IsNil)
	c.Check(context.Account, Equals, env.ecfg.StorageAccountName())
	c.Check(context.Key, Equals, env.ecfg.StorageAccountKey())
}

func (EnvironSuite) TestPublicStorage(c *C) {
	env := makeEnviron(c)
	baseStorage := env.PublicStorage()
	storage, ok := baseStorage.(*azureStorage)
	c.Assert(storage, NotNil)
	c.Check(ok, Equals, true)
	c.Check(storage.storageContext.getContainer(), Equals, env.ecfg.PublicStorageContainerName())
	context, err := storage.getStorageContext()
	c.Assert(err, IsNil)
	c.Check(context.Account, Equals, env.ecfg.PublicStorageAccountName())
	c.Check(context.Key, Equals, "")
}

func (EnvironSuite) TestPublicStorageReturnsEmptyStorageIfNoInfo(c *C) {
	env := makeEnviron(c)
	env.ecfg.attrs["public-storage-container-name"] = ""
	env.ecfg.attrs["public-storage-account-name"] = ""
	storage := env.PublicStorage()
	c.Check(storage, Equals, environs.EmptyStorage)
}

func (EnvironSuite) TestGetStorageContext(c *C) {
	env := makeEnviron(c)
	storage, err := env.getStorageContext()
	c.Assert(err, IsNil)
	c.Assert(storage, NotNil)
	c.Check(storage.Account, Equals, env.ecfg.StorageAccountName())
	c.Check(storage.Key, Equals, env.ecfg.StorageAccountKey())
}

func (EnvironSuite) TestGetPublicStorageContext(c *C) {
	env := makeEnviron(c)
	storage, err := env.getPublicStorageContext()
	c.Assert(err, IsNil)
	c.Assert(storage, NotNil)
	c.Check(storage.Account, Equals, env.ecfg.PublicStorageAccountName())
	c.Check(storage.Key, Equals, "")
}

func (EnvironSuite) TestSetConfigValidates(c *C) {
	env := makeEnviron(c)
	originalCfg := env.ecfg
	attrs := makeAzureConfigMap(c)
	// This config is not valid.  It lacks essential information.
	delete(attrs, "management-subscription-id")
	badCfg, err := config.New(attrs)
	c.Assert(err, IsNil)

	err = env.SetConfig(badCfg)

	// Since the config was not valid, SetConfig returns an error.  It
	// does not update the environment's config either.
	c.Check(err, NotNil)
	c.Check(
		err,
		ErrorMatches,
		"management-subscription-id: expected string, got nothing")
	c.Check(env.ecfg, Equals, originalCfg)
}

func (EnvironSuite) TestSetConfigUpdatesConfig(c *C) {
	env := makeEnviron(c)
	// We're going to set a new config.  It can be recognized by its
	// unusual default Ubuntu release series: 7.04 Feisty Fawn.
	attrs := makeAzureConfigMap(c)
	attrs["default-series"] = "feisty"
	cfg, err := config.New(attrs)
	c.Assert(err, IsNil)

	err = env.SetConfig(cfg)
	c.Assert(err, IsNil)

	c.Check(env.ecfg.Config.DefaultSeries(), Equals, "feisty")
}

func (EnvironSuite) TestSetConfigLocksEnviron(c *C) {
	env := makeEnviron(c)
	cfg, err := config.New(makeAzureConfigMap(c))
	c.Assert(err, IsNil)

	testing.TestLockingFunction(&env.Mutex, func() { env.SetConfig(cfg) })
}

func (EnvironSuite) TestSetConfigWillNotUpdateName(c *C) {
	// Once the environment's name has been set, it cannot be updated.
	// Global validation rejects such a change.
	// This matters because the attribute is not protected by a lock.
	env := makeEnviron(c)
	originalName := env.Name()
	attrs := makeAzureConfigMap(c)
	attrs["name"] = "new-name"
	cfg, err := config.New(attrs)
	c.Assert(err, IsNil)

	err = env.SetConfig(cfg)

	c.Assert(err, NotNil)
	c.Check(
		err,
		ErrorMatches,
		`cannot change name from ".*" to "new-name"`)
	c.Check(env.Name(), Equals, originalName)
}
