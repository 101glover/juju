package jujutest

import (
	"bytes"
	"fmt"
	"io"
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/charm"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/juju"
	"launchpad.net/juju-core/juju/testing"
	"launchpad.net/juju-core/state"
	coretesting "launchpad.net/juju-core/testing"
	"launchpad.net/juju-core/trivial"
	"launchpad.net/juju-core/version"
	"time"
)

// LiveTests contains tests that are designed to run against a live server
// (e.g. Amazon EC2).  The Environ is opened once only for all the tests
// in the suite, stored in Env, and Destroyed after the suite has completed.
type LiveTests struct {
	coretesting.LoggingSuite

	// Config holds the configuration attributes for opening an environment.
	Config map[string]interface{}

	// Env holds the currently opened environment.
	Env environs.Environ

	// Attempt holds a strategy for waiting until the environment
	// becomes logically consistent.
	Attempt trivial.AttemptStrategy

	// CanOpenState should be true if the testing environment allows
	// the state to be opened after bootstrapping.
	CanOpenState bool

	// HasProvisioner should be true if the environment has
	// a provisioning agent.
	HasProvisioner bool

	bootstrapped bool
}

func (t *LiveTests) SetUpSuite(c *C) {
	t.LoggingSuite.SetUpSuite(c)
	e, err := environs.NewFromAttrs(t.Config)
	c.Assert(err, IsNil, Commentf("opening environ %#v", t.Config))
	c.Assert(e, NotNil)
	t.Env = e
	c.Logf("environment configuration: %#v", publicAttrs(e))
}

func publicAttrs(e environs.Environ) map[string]interface{} {
	cfg := e.Config()
	secrets, err := e.Provider().SecretAttrs(cfg)
	if err != nil {
		panic(err)
	}
	attrs := cfg.AllAttrs()
	for attr := range secrets {
		delete(attrs, attr)
	}
	return attrs
}

func (t *LiveTests) TearDownSuite(c *C) {
	if t.Env != nil {
		err := t.Env.Destroy(nil)
		c.Check(err, IsNil)
		t.Env = nil
	}
	t.LoggingSuite.TearDownSuite(c)
}

func (t *LiveTests) BootstrapOnce(c *C) {
	if t.bootstrapped {
		return
	}
	err := environs.Bootstrap(t.Env, true, panicWrite)
	c.Assert(err, IsNil)
	t.bootstrapped = true
}

func panicWrite(name string, cert, key []byte) error {
	panic("writeCertAndKey called unexpectedly")
}

func (t *LiveTests) Destroy(c *C) {
	err := t.Env.Destroy(nil)
	c.Assert(err, IsNil)
	t.bootstrapped = false
}

// TestStartStop is similar to Tests.TestStartStop except
// that it does not assume a pristine environment.
func (t *LiveTests) TestStartStop(c *C) {
	inst, err := t.Env.StartInstance("0", testing.InvalidStateInfo("0"), testing.InvalidAPIInfo("0"), nil)
	c.Assert(err, IsNil)
	c.Assert(inst, NotNil)
	id0 := inst.Id()

	insts, err := t.Env.Instances([]state.InstanceId{id0, id0})
	c.Assert(err, IsNil)
	c.Assert(insts, HasLen, 2)
	c.Assert(insts[0].Id(), Equals, id0)
	c.Assert(insts[1].Id(), Equals, id0)

	// Asserting on the return of AllInstances makes the test fragile,
	// as even comparing the before and after start values can be thrown
	// off if other instances have been created or destroyed in the same
	// time frame. Instead, just check the instance we created exists.
	insts, err = t.Env.AllInstances()
	c.Assert(err, IsNil)
	found := false
	for _, inst := range insts {
		if inst.Id() == id0 {
			c.Assert(found, Equals, false, Commentf("%v", insts))
			found = true
		}
	}
	c.Assert(found, Equals, true, Commentf("expected %v in %v", inst, insts))

	dns, err := inst.WaitDNSName()
	c.Assert(err, IsNil)
	c.Assert(dns, Not(Equals), "")

	insts, err = t.Env.Instances([]state.InstanceId{id0, ""})
	c.Assert(err, Equals, environs.ErrPartialInstances)
	c.Assert(insts, HasLen, 2)
	c.Check(insts[0].Id(), Equals, id0)
	c.Check(insts[1], IsNil)

	err = t.Env.StopInstances([]environs.Instance{inst})
	c.Assert(err, IsNil)

	// The machine may not be marked as shutting down
	// immediately. Repeat a few times to ensure we get the error.
	for a := t.Attempt.Start(); a.Next(); {
		insts, err = t.Env.Instances([]state.InstanceId{id0})
		if err != nil {
			break
		}
	}
	c.Assert(err, Equals, environs.ErrNoInstances)
	c.Assert(insts, HasLen, 0)
}

func (t *LiveTests) TestPorts(c *C) {
	inst1, err := t.Env.StartInstance("1", testing.InvalidStateInfo("1"), testing.InvalidAPIInfo("1"), nil)
	c.Assert(err, IsNil)
	c.Assert(inst1, NotNil)
	defer t.Env.StopInstances([]environs.Instance{inst1})
	ports, err := inst1.Ports("1")
	c.Assert(err, IsNil)
	c.Assert(ports, HasLen, 0)

	inst2, err := t.Env.StartInstance("2", testing.InvalidStateInfo("2"), testing.InvalidAPIInfo("2"), nil)
	c.Assert(err, IsNil)
	c.Assert(inst2, NotNil)
	ports, err = inst2.Ports("2")
	c.Assert(err, IsNil)
	c.Assert(ports, HasLen, 0)
	defer t.Env.StopInstances([]environs.Instance{inst2})

	// Open some ports and check they're there.
	err = inst1.OpenPorts("1", []state.Port{{"udp", 67}, {"tcp", 45}})
	c.Assert(err, IsNil)
	ports, err = inst1.Ports("1")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"udp", 67}})
	ports, err = inst2.Ports("2")
	c.Assert(err, IsNil)
	c.Assert(ports, HasLen, 0)

	err = inst2.OpenPorts("2", []state.Port{{"tcp", 89}, {"tcp", 45}})
	c.Assert(err, IsNil)

	// Check there's no crosstalk to another machine
	ports, err = inst2.Ports("2")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"tcp", 89}})
	ports, err = inst1.Ports("1")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"udp", 67}})

	// Check that opening the same port again is ok.
	oldPorts, err := inst2.Ports("2")
	c.Assert(err, IsNil)
	err = inst2.OpenPorts("2", []state.Port{{"tcp", 45}})
	c.Assert(err, IsNil)
	ports, err = inst2.Ports("2")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, oldPorts)

	// Check that opening the same port again and another port is ok.
	err = inst2.OpenPorts("2", []state.Port{{"tcp", 45}, {"tcp", 99}})
	c.Assert(err, IsNil)
	ports, err = inst2.Ports("2")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"tcp", 89}, {"tcp", 99}})

	err = inst2.ClosePorts("2", []state.Port{{"tcp", 45}, {"tcp", 99}})
	c.Assert(err, IsNil)

	// Check that we can close ports and that there's no crosstalk.
	ports, err = inst2.Ports("2")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 89}})
	ports, err = inst1.Ports("1")
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"udp", 67}})

	// Check that we can close multiple ports.
	err = inst1.ClosePorts("1", []state.Port{{"tcp", 45}, {"udp", 67}})
	c.Assert(err, IsNil)
	ports, err = inst1.Ports("1")
	c.Assert(ports, HasLen, 0)

	// Check that we can close ports that aren't there.
	err = inst2.ClosePorts("2", []state.Port{{"tcp", 111}, {"udp", 222}})
	c.Assert(err, IsNil)
	ports, err = inst2.Ports("2")
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 89}})

	// Check errors when acting on environment.
	err = t.Env.OpenPorts([]state.Port{{"tcp", 80}})
	c.Assert(err, ErrorMatches, `invalid firewall mode for opening ports on environment: "instance"`)

	err = t.Env.ClosePorts([]state.Port{{"tcp", 80}})
	c.Assert(err, ErrorMatches, `invalid firewall mode for closing ports on environment: "instance"`)

	_, err = t.Env.Ports()
	c.Assert(err, ErrorMatches, `invalid firewall mode for retrieving ports from environment: "instance"`)
}

func (t *LiveTests) TestGlobalPorts(c *C) {
	// Change configuration.
	oldConfig := t.Env.Config()
	defer func() {
		err := t.Env.SetConfig(oldConfig)
		c.Assert(err, IsNil)
	}()

	attrs := t.Env.Config().AllAttrs()
	attrs["firewall-mode"] = "global"
	newConfig, err := t.Env.Config().Apply(attrs)
	c.Assert(err, IsNil)
	err = t.Env.SetConfig(newConfig)
	c.Assert(err, IsNil)

	// Create instances and check open ports on both instances.
	inst1, err := t.Env.StartInstance("1", testing.InvalidStateInfo("1"), testing.InvalidAPIInfo("1"), nil)
	c.Assert(err, IsNil)
	defer t.Env.StopInstances([]environs.Instance{inst1})
	ports, err := t.Env.Ports()
	c.Assert(err, IsNil)
	c.Assert(ports, HasLen, 0)

	inst2, err := t.Env.StartInstance("2", testing.InvalidStateInfo("2"), testing.InvalidAPIInfo("2"), nil)
	c.Assert(err, IsNil)
	ports, err = t.Env.Ports()
	c.Assert(err, IsNil)
	c.Assert(ports, HasLen, 0)
	defer t.Env.StopInstances([]environs.Instance{inst2})

	err = t.Env.OpenPorts([]state.Port{{"udp", 67}, {"tcp", 45}, {"tcp", 89}, {"tcp", 99}})
	c.Assert(err, IsNil)

	ports, err = t.Env.Ports()
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"tcp", 89}, {"tcp", 99}, {"udp", 67}})

	// Check closing some ports.
	err = t.Env.ClosePorts([]state.Port{{"tcp", 99}, {"udp", 67}})
	c.Assert(err, IsNil)

	ports, err = t.Env.Ports()
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"tcp", 89}})

	// Check that we can close ports that aren't there.
	err = t.Env.ClosePorts([]state.Port{{"tcp", 111}, {"udp", 222}})
	c.Assert(err, IsNil)

	ports, err = t.Env.Ports()
	c.Assert(err, IsNil)
	c.Assert(ports, DeepEquals, []state.Port{{"tcp", 45}, {"tcp", 89}})

	// Check errors when acting on instances.
	err = inst1.OpenPorts("1", []state.Port{{"tcp", 80}})
	c.Assert(err, ErrorMatches, `invalid firewall mode for opening ports on instance: "global"`)

	err = inst1.ClosePorts("1", []state.Port{{"tcp", 80}})
	c.Assert(err, ErrorMatches, `invalid firewall mode for closing ports on instance: "global"`)

	_, err = inst1.Ports("1")
	c.Assert(err, ErrorMatches, `invalid firewall mode for retrieving ports from instance: "global"`)
}

func (t *LiveTests) TestBootstrapMultiple(c *C) {
	t.BootstrapOnce(c)

	err := environs.Bootstrap(t.Env, false, panicWrite)
	c.Assert(err, ErrorMatches, "environment is already bootstrapped")

	c.Logf("destroy env")
	t.Destroy(c)
	t.Destroy(c) // Again, should work fine and do nothing.

	// check that we can bootstrap after destroy
	t.BootstrapOnce(c)
}

func (t *LiveTests) TestBootstrapAndDeploy(c *C) {
	if !t.CanOpenState || !t.HasProvisioner {
		c.Skip(fmt.Sprintf("skipping provisioner test, CanOpenState: %v, HasProvisioner: %v", t.CanOpenState, t.HasProvisioner))
	}
	t.BootstrapOnce(c)

	// TODO(niemeyer): Stop growing this kitchen sink test and split it into proper parts.

	c.Logf("opening connection")
	conn, err := juju.NewConn(t.Env)
	c.Assert(err, IsNil)
	defer conn.Close()

	c.Logf("opening API connection")
	apiConn, err := juju.NewAPIConn(t.Env)
	c.Assert(err, IsNil)
	defer conn.Close()

	// Check that the agent version has made it through the
	// bootstrap process (it's optional in the config.Config)
	cfg, err := conn.State.EnvironConfig()
	c.Assert(err, IsNil)
	c.Check(cfg.AgentVersion(), Equals, version.Current.Number)

	// Wait for machine agent to come up on the bootstrap
	// machine and find the deployed series from that.
	m0, err := conn.State.Machine("0")
	c.Assert(err, IsNil)

	instId0, ok := m0.InstanceId()
	c.Assert(ok, Equals, true)

	// Check that the API connection is working.
	status, err := apiConn.State.Client().Status()
	c.Assert(err, IsNil)
	c.Assert(status.Machines["0"].InstanceId, Equals, string(instId0))

	mw0 := newMachineToolWaiter(m0)
	defer mw0.Stop()

	mtools0 := waitAgentTools(c, mw0, version.Current)

	// Create a new service and deploy a unit of it.
	c.Logf("deploying service")
	repoDir := c.MkDir()
	url := coretesting.Charms.ClonedURL(repoDir, mtools0.Series, "dummy")
	sch, err := conn.PutCharm(url, &charm.LocalRepository{repoDir}, false)

	c.Assert(err, IsNil)
	svc, err := conn.State.AddService("dummy", sch)
	c.Assert(err, IsNil)
	units, err := conn.AddUnits(svc, 1)
	c.Assert(err, IsNil)
	unit := units[0]

	// Wait for the unit's machine and associated agent to come up
	// and announce itself.
	mid1, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	m1, err := conn.State.Machine(mid1)
	c.Assert(err, IsNil)
	mw1 := newMachineToolWaiter(m1)
	defer mw1.Stop()
	waitAgentTools(c, mw1, mtools0.Binary)

	err = m1.Refresh()
	c.Assert(err, IsNil)
	instId1, ok := m1.InstanceId()
	c.Assert(ok, Equals, true)
	uw := newUnitToolWaiter(unit)
	defer uw.Stop()
	utools := waitAgentTools(c, uw, version.Current)

	// Check that we can upgrade the environment.
	newVersion := utools.Binary
	newVersion.Patch++
	t.checkUpgrade(c, conn, newVersion, mw0, mw1, uw)

	// BUG(niemeyer): Logic below is very much wrong. Must be:
	//
	// 1. EnsureDying on the unit and EnsureDying on the machine
	// 2. Unit dies by itself
	// 3. Machine removes dead unit
	// 4. Machine dies by itself
	// 5. Provisioner removes dead machine
	//

	// Now remove the unit and its assigned machine and
	// check that the PA removes it.
	c.Logf("removing unit")
	err = unit.Destroy()
	c.Assert(err, IsNil)

	// Wait until unit is dead
	uwatch := unit.Watch()
	defer uwatch.Stop()
	for unit.Life() != state.Dead {
		c.Logf("waiting for unit change")
		<-uwatch.Changes()
		err := unit.Refresh()
		c.Logf("refreshed; err %v", err)
		if state.IsNotFound(err) {
			c.Logf("unit has been removed")
			break
		}
		c.Assert(err, IsNil)
	}
	for {
		c.Logf("destroying machine")
		err := m1.Destroy()
		if err == nil {
			break
		}
		c.Assert(err, Equals, state.ErrHasAssignedUnits)
		time.Sleep(5 * time.Second)
		err = m1.Refresh()
		if state.IsNotFound(err) {
			break
		}
		c.Assert(err, IsNil)
	}
	c.Logf("waiting for instance to be removed")
	t.assertStopInstance(c, conn.Environ, instId1)
}

type tooler interface {
	Life() state.Life
	AgentTools() (*state.Tools, error)
	Refresh() error
	String() string
}

type watcher interface {
	Stop() error
	Err() error
}

type toolsWaiter struct {
	lastTools *state.Tools
	// changes is a chan of struct{} so that it can
	// be used with different kinds of entity watcher.
	changes chan struct{}
	watcher watcher
	tooler  tooler
}

func newMachineToolWaiter(m *state.Machine) *toolsWaiter {
	w := m.Watch()
	waiter := &toolsWaiter{
		changes: make(chan struct{}, 1),
		watcher: w,
		tooler:  m,
	}
	go func() {
		for _ = range w.Changes() {
			waiter.changes <- struct{}{}
		}
		close(waiter.changes)
	}()
	return waiter
}

func newUnitToolWaiter(u *state.Unit) *toolsWaiter {
	w := u.Watch()
	waiter := &toolsWaiter{
		changes: make(chan struct{}, 1),
		watcher: w,
		tooler:  u,
	}
	go func() {
		for _ = range w.Changes() {
			waiter.changes <- struct{}{}
		}
		close(waiter.changes)
	}()
	return waiter
}

func (w *toolsWaiter) Stop() error {
	return w.watcher.Stop()
}

// NextTools returns the next changed tools, waiting
// until the tools are actually set.
func (w *toolsWaiter) NextTools(c *C) (*state.Tools, error) {
	for _ = range w.changes {
		err := w.tooler.Refresh()
		if err != nil {
			return nil, fmt.Errorf("cannot refresh: %v", err)
		}
		if w.tooler.Life() == state.Dead {
			return nil, fmt.Errorf("object is dead")
		}
		tools, err := w.tooler.AgentTools()
		if state.IsNotFound(err) {
			c.Logf("tools not yet set")
			continue
		}
		if err != nil {
			return nil, err
		}
		changed := w.lastTools == nil || *tools != *w.lastTools
		w.lastTools = tools
		if changed {
			return tools, nil
		}
		c.Logf("found same tools")
	}
	return nil, fmt.Errorf("watcher closed prematurely: %v", w.watcher.Err())
}

// waitAgentTools waits for the given agent
// to start and returns the tools that it is running.
func waitAgentTools(c *C, w *toolsWaiter, expect version.Binary) *state.Tools {
	c.Logf("waiting for %v to signal agent version", w.tooler.String())
	tools, err := w.NextTools(c)
	c.Assert(err, IsNil)
	c.Check(tools.Binary, Equals, expect)
	return tools
}

// checkUpgrade sets the environment agent version and checks that
// all the provided watchers upgrade to the requested version.
func (t *LiveTests) checkUpgrade(c *C, conn *juju.Conn, newVersion version.Binary, waiters ...*toolsWaiter) {
	c.Logf("putting testing version of juju tools")
	upgradeTools, err := environs.PutTools(t.Env.Storage(), &newVersion.Number)
	c.Assert(err, IsNil)

	// Check that the put version really is the version we expect.
	c.Assert(upgradeTools.Binary, Equals, newVersion)
	err = setAgentVersion(conn.State, newVersion.Number)
	c.Assert(err, IsNil)

	for i, w := range waiters {
		c.Logf("waiting for upgrade of %d: %v", i, w.tooler.String())

		waitAgentTools(c, w, newVersion)
		c.Logf("upgrade %d successful", i)
	}
}

// setAgentVersion sets the current agent version in the state's
// environment configuration.
func setAgentVersion(st *state.State, vers version.Number) error {
	cfg, err := st.EnvironConfig()
	if err != nil {
		return err
	}
	attrs := cfg.AllAttrs()
	attrs["agent-version"] = vers.String()
	cfg, err = config.New(attrs)
	if err != nil {
		panic(fmt.Errorf("config refused agent-version: %v", err))
	}
	return st.SetEnvironConfig(cfg)
}

var waitAgent = trivial.AttemptStrategy{
	Total: 30 * time.Second,
	Delay: 1 * time.Second,
}

func (t *LiveTests) assertStartInstance(c *C, m *state.Machine) {
	// Wait for machine to get an instance id.
	for a := waitAgent.Start(); a.Next(); {
		err := m.Refresh()
		c.Assert(err, IsNil)
		instId, ok := m.InstanceId()
		if !ok {
			continue
		}
		_, err = t.Env.Instances([]state.InstanceId{instId})
		c.Assert(err, IsNil)
		return
	}
	c.Fatalf("provisioner failed to start machine after %v", waitAgent.Total)
}

func (t *LiveTests) assertStopInstance(c *C, env environs.Environ, instId state.InstanceId) {
	var err error
	for a := waitAgent.Start(); a.Next(); {
		_, err = t.Env.Instances([]state.InstanceId{instId})
		if err == nil {
			continue
		}
		if err == environs.ErrNoInstances {
			return
		}
		c.Logf("error from Instances: %v", err)
	}
	c.Fatalf("provisioner failed to stop machine after %v", waitAgent.Total)
}

// assertInstanceId asserts that the machine has an instance id
// that matches that of the given instance. If the instance is nil,
// It asserts that the instance id is unset.
func assertInstanceId(c *C, m *state.Machine, inst environs.Instance) {
	var wantId, gotId state.InstanceId
	var err error
	if inst != nil {
		wantId = inst.Id()
	}
	for a := waitAgent.Start(); a.Next(); {
		err := m.Refresh()
		c.Assert(err, IsNil)
		var ok bool
		gotId, ok = m.InstanceId()
		if !ok {
			if inst == nil {
				return
			}
			continue
		}
		break
	}
	c.Assert(err, IsNil)
	c.Assert(gotId, Equals, wantId)
}

// TODO check that binary data works ok?
var contents = []byte("hello\n")
var contents2 = []byte("goodbye\n\n")

func (t *LiveTests) TestFile(c *C) {
	name := fmt.Sprint("testfile", time.Now().UnixNano())
	storage := t.Env.Storage()

	checkFileDoesNotExist(c, storage, name, t.Attempt)
	checkPutFile(c, storage, name, contents)
	checkFileHasContents(c, storage, name, contents, t.Attempt)
	checkPutFile(c, storage, name, contents2) // check that we can overwrite the file
	checkFileHasContents(c, storage, name, contents2, t.Attempt)

	// check that the listed contents include the
	// expected name.
	found := false
	var names []string
attempt:
	for a := t.Attempt.Start(); a.Next(); {
		var err error
		names, err = storage.List("")
		c.Assert(err, IsNil)
		for _, lname := range names {
			if lname == name {
				found = true
				break attempt
			}
		}
	}
	if !found {
		c.Errorf("file name %q not found in file list %q", name, names)
	}
	err := storage.Remove(name)
	c.Check(err, IsNil)
	checkFileDoesNotExist(c, storage, name, t.Attempt)
	// removing a file that does not exist should not be an error.
	err = storage.Remove(name)
	c.Check(err, IsNil)
}

// Check that we can't start an instance running tools
// that correspond with no available platform.
func (t *LiveTests) TestStartInstanceOnUnknownPlatform(c *C) {
	vers := version.Current
	// Note that we want this test to function correctly in the
	// dummy environment, so to avoid enumerating all possible
	// platforms in the dummy provider, it treats only series and/or
	// architectures with the "unknown" prefix as invalid.
	vers.Series = "unknownseries"
	vers.Arch = "unknownarch"
	name := environs.ToolsStoragePath(vers)
	storage := t.Env.Storage()
	checkPutFile(c, storage, name, []byte("fake tools on invalid series"))
	defer storage.Remove(name)

	url, err := storage.URL(name)
	c.Assert(err, IsNil)
	tools := &state.Tools{
		Binary: vers,
		URL:    url,
	}

	inst, err := t.Env.StartInstance("4", testing.InvalidStateInfo("4"), testing.InvalidAPIInfo("4"), tools)
	if inst != nil {
		err := t.Env.StopInstances([]environs.Instance{inst})
		c.Check(err, IsNil)
	}
	c.Assert(inst, IsNil)
	c.Assert(err, ErrorMatches, "cannot find image.*")
}

func (t *LiveTests) TestBootstrapWithDefaultSeries(c *C) {
	if !t.HasProvisioner {
		c.Skip("HasProvisioner is false; cannot test deployment")
	}

	current := version.Current
	other := current
	other.Series = "precise"
	if current == other {
		other.Series = "quantal"
	}

	cfg := t.Env.Config()
	cfg, err := cfg.Apply(map[string]interface{}{"default-series": other.Series})
	c.Assert(err, IsNil)
	env, err := environs.New(cfg)
	c.Assert(err, IsNil)

	dummyenv, err := environs.NewFromAttrs(map[string]interface{}{
		"type":         "dummy",
		"name":         "dummy storage",
		"secret":       "pizza",
		"state-server": false,
	})
	c.Assert(err, IsNil)
	defer dummyenv.Destroy(nil)

	// BUG: We destroy the environment, then write to its storage.
	// This is bogus, strictly speaking, but it works on
	// existing providers for the time being and means
	// this test does not fail when the environment is
	// already bootstrapped.
	t.Destroy(c)

	currentPath := environs.ToolsStoragePath(current)
	otherPath := environs.ToolsStoragePath(other)
	envStorage := env.Storage()
	dummyStorage := dummyenv.Storage()

	defer envStorage.Remove(otherPath)

	_, err = environs.PutTools(dummyStorage, &current.Number)
	c.Assert(err, IsNil)

	// This will only work while cross-compiling across releases is safe,
	// which depends on external elements. Tends to be safe for the last
	// few releases, but we may have to refactor some day.
	err = storageCopy(dummyStorage, currentPath, envStorage, otherPath)
	c.Assert(err, IsNil)

	err = environs.Bootstrap(env, false, panicWrite)
	c.Assert(err, IsNil)
	defer env.Destroy(nil)

	conn, err := juju.NewConn(env)
	c.Assert(err, IsNil)
	defer conn.Close()

	// Wait for machine agent to come up on the bootstrap
	// machine and ensure it deployed the proper series.
	m0, err := conn.State.Machine("0")
	c.Assert(err, IsNil)
	mw0 := newMachineToolWaiter(m0)
	defer mw0.Stop()

	waitAgentTools(c, mw0, other)
}

func storageCopy(source environs.Storage, sourcePath string, target environs.Storage, targetPath string) error {
	rc, err := source.Get(sourcePath)
	if err != nil {
		return err
	}
	var buf bytes.Buffer
	_, err = io.Copy(&buf, rc)
	rc.Close()
	if err != nil {
		return err
	}
	return target.Put(targetPath, &buf, int64(buf.Len()))
}
