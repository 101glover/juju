// Copyright 2012, 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package provisioner_test

import (
	"fmt"
	"strings"
	stdtesting "testing"
	"time"

	"labix.org/v2/mgo/bson"
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/constraints"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/environs/dummy"
	"launchpad.net/juju-core/errors"
	"launchpad.net/juju-core/instance"
	"launchpad.net/juju-core/juju/testing"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/api/params"
	coretesting "launchpad.net/juju-core/testing"
	"launchpad.net/juju-core/utils"
	"launchpad.net/juju-core/utils/set"
	"launchpad.net/juju-core/worker"
	"launchpad.net/juju-core/worker/provisioner"
)

func TestPackage(t *stdtesting.T) {
	coretesting.MgoTestPackage(t)
}

type CommonProvisionerSuite struct {
	testing.JujuConnSuite
	op  <-chan dummy.Operation
	cfg *config.Config
}

type ProvisionerSuite struct {
	CommonProvisionerSuite
}

var _ = Suite(&ProvisionerSuite{})

var veryShortAttempt = utils.AttemptStrategy{
	Total: 1 * time.Second,
	Delay: 80 * time.Millisecond,
}

var _ worker.Worker = (*provisioner.Provisioner)(nil)

func (s *CommonProvisionerSuite) SetUpTest(c *C) {
	s.JujuConnSuite.SetUpTest(c)
	// Create the operations channel with more than enough space
	// for those tests that don't listen on it.
	op := make(chan dummy.Operation, 500)
	dummy.Listen(op)
	s.op = op

	cfg, err := s.State.EnvironConfig()
	c.Assert(err, IsNil)
	s.cfg = cfg
}

// breakDummyProvider changes the environment config in state in a way
// that causes the given environMethod of the dummy provider to return
// an error, which is also returned as a message to be checked.
func breakDummyProvider(c *C, st *state.State, environMethod string) string {
	oldCfg, err := st.EnvironConfig()
	c.Assert(err, IsNil)
	cfg, err := oldCfg.Apply(map[string]interface{}{"broken": environMethod})
	c.Assert(err, IsNil)
	err = st.SetEnvironConfig(cfg)
	c.Assert(err, IsNil)
	return fmt.Sprintf("dummy.%s is broken", environMethod)
}

// invalidateEnvironment alters the environment configuration
// so the Settings returned from the watcher will not pass
// validation.
func (s *CommonProvisionerSuite) invalidateEnvironment(c *C) error {
	admindb := s.Session.DB("admin")
	err := admindb.Login("admin", testing.AdminSecret)
	if err != nil {
		err = admindb.Login("admin", utils.PasswordHash(testing.AdminSecret))
	}
	c.Assert(err, IsNil)
	settings := s.Session.DB("juju").C("settings")
	return settings.UpdateId("e", bson.D{{"$unset", bson.D{{"type", 1}}}})
}

// fixEnvironment undoes the work of invalidateEnvironment.
func (s *CommonProvisionerSuite) fixEnvironment() error {
	return s.State.SetEnvironConfig(s.cfg)
}

// stopper is stoppable.
type stopper interface {
	Stop() error
}

// stop stops a stopper.
func stop(c *C, s stopper) {
	c.Assert(s.Stop(), IsNil)
}

func (s *CommonProvisionerSuite) checkStartInstance(c *C, m *state.Machine) instance.Instance {
	return s.checkStartInstanceCustom(c, m, "pork", constraints.Value{})
}

func (s *CommonProvisionerSuite) checkStartInstanceCustom(c *C, m *state.Machine, secret string, cons constraints.Value) (instance instance.Instance) {
	s.State.StartSync()
	for {
		select {
		case o := <-s.op:
			switch o := o.(type) {
			case dummy.OpStartInstance:
				instance = o.Instance
				s.waitInstanceId(c, m, instance.Id())

				// Check the instance was started with the expected params.
				c.Assert(o.MachineId, Equals, m.Id())
				nonceParts := strings.SplitN(o.MachineNonce, ":", 2)
				c.Assert(nonceParts, HasLen, 2)
				c.Assert(nonceParts[0], Equals, state.MachineTag("0"))
				c.Assert(utils.IsValidUUIDString(nonceParts[1]), Equals, true)
				c.Assert(o.Secret, Equals, secret)
				c.Assert(o.Constraints, DeepEquals, cons)

				// Check we can connect to the state with
				// the machine's entity name and password.
				info := s.StateInfo(c)
				info.Tag = m.Tag()
				c.Assert(o.Info.Password, Not(HasLen), 0)
				info.Password = o.Info.Password
				c.Assert(o.Info, DeepEquals, info)
				// Check we can connect to the state with
				// the machine's entity name and password.
				st, err := state.Open(o.Info, state.DefaultDialOpts())
				c.Assert(err, IsNil)

				st.Close()
				return
			default:
				c.Logf("ignoring unexpected operation %#v", o)
			}
		case <-time.After(2 * time.Second):
			c.Fatalf("provisioner did not start an instance")
			return
		}
	}
	return
}

// checkNoOperations checks that the environ was not operated upon.
func (s *CommonProvisionerSuite) checkNoOperations(c *C) {
	s.State.StartSync()
	select {
	case o := <-s.op:
		c.Fatalf("unexpected operation %#v", o)
	case <-time.After(200 * time.Millisecond):
		return
	}
}

// checkStopInstances checks that an instance has been stopped.
func (s *CommonProvisionerSuite) checkStopInstances(c *C, instances ...instance.Instance) {
	s.State.StartSync()
	instanceIds := set.NewStrings()
	for _, instance := range instances {
		instanceIds.Add(string(instance.Id()))
	}
	// Continue checking for stop instance calls until all the instances we
	// are waiting on to finish, actually finish, or we time out.
	for !instanceIds.IsEmpty() {
		select {
		case o := <-s.op:
			switch o := o.(type) {
			case dummy.OpStopInstances:
				for _, stoppedInstance := range o.Instances {
					instanceIds.Remove(string(stoppedInstance.Id()))
				}
			default:
				c.Fatalf("unexpected operation %#v", o)
				return
			}
		case <-time.After(2 * time.Second):
			c.Fatalf("provisioner did not stop an instance")
			return
		}
	}
}

func (s *CommonProvisionerSuite) waitMachine(c *C, m *state.Machine, check func() bool) {
	w := m.Watch()
	defer stop(c, w)
	timeout := time.After(500 * time.Millisecond)
	resync := time.After(0)
	for {
		select {
		case <-w.Changes():
			if check() {
				return
			}
		case <-resync:
			resync = time.After(50 * time.Millisecond)
			s.State.StartSync()
		case <-timeout:
			c.Fatalf("machine %v wait timed out", m)
		}
	}
}

// waitRemoved waits for the supplied machine to be removed from state.
func (s *CommonProvisionerSuite) waitRemoved(c *C, m *state.Machine) {
	s.waitMachine(c, m, func() bool {
		err := m.Refresh()
		if errors.IsNotFoundError(err) {
			return true
		}
		c.Assert(err, IsNil)
		c.Logf("machine %v is still %s", m, m.Life())
		return false
	})
}

// waitInstanceId waits until the supplied machine has an instance id, then
// asserts it is as expected.
func (s *CommonProvisionerSuite) waitInstanceId(c *C, m *state.Machine, expect instance.Id) {
	s.waitMachine(c, m, func() bool {
		err := m.Refresh()
		c.Assert(err, IsNil)
		if actual, ok := m.InstanceId(); ok {
			c.Assert(actual, Equals, expect)
			return true
		}
		c.Logf("machine %v is still unprovisioned", m)
		return false
	})
}

func (s *ProvisionerSuite) newEnvironProvisioner(machineId string) *provisioner.Provisioner {
	return provisioner.NewProvisioner(provisioner.ENVIRON, s.State, machineId, "")
}

func (s *ProvisionerSuite) TestProvisionerStartStop(c *C) {
	p := s.newEnvironProvisioner("0")
	c.Assert(p.Stop(), IsNil)
}

func (s *ProvisionerSuite) TestSimple(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// Check that an instance is provisioned when the machine is created...
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	instance := s.checkStartInstance(c, m)

	// ...and removed, along with the machine, when the machine is Dead.
	c.Assert(m.EnsureDead(), IsNil)
	s.checkStopInstances(c, instance)
	s.waitRemoved(c, m)
}

func (s *ProvisionerSuite) TestConstraints(c *C) {
	// Create a machine with non-standard constraints.
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	cons := constraints.MustParse("mem=4G arch=amd64")
	err = m.SetConstraints(cons)
	c.Assert(err, IsNil)

	// Start a provisioner and check those constraints are used.
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)
	s.checkStartInstanceCustom(c, m, "pork", cons)
}

func (s *ProvisionerSuite) TestProvisionerSetsErrorStatusWhenStartInstanceFailed(c *C) {
	brokenMsg := breakDummyProvider(c, s.State, "StartInstance")
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// Check that an instance is not provisioned when the machine is created...
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	s.checkNoOperations(c)

	// And check the machine status is set to error.
	status, info, err := m.Status()
	c.Assert(err, IsNil)
	c.Assert(status, Equals, params.StatusError)
	c.Assert(info, Equals, brokenMsg)

	// Unbreak the environ config.
	err = s.fixEnvironment()
	c.Assert(err, IsNil)

	// Restart the PA to make sure the machine is skipped again.
	stop(c, p)
	p = s.newEnvironProvisioner("0")
	defer stop(c, p)
	s.checkNoOperations(c)
}

func (s *ProvisionerSuite) TestProvisioningDoesNotOccurForContainers(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// create a machine to host the container.
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	instance := s.checkStartInstance(c, m)

	// make a container on the machine we just created
	params := state.AddMachineParams{
		ParentId:      m.Id(),
		ContainerType: state.LXC,
		Series:        config.DefaultSeries,
		Jobs:          []state.MachineJob{state.JobHostUnits},
	}
	container, err := s.State.AddMachineWithConstraints(&params)
	c.Assert(err, IsNil)

	// the PA should not attempt to create it
	s.checkNoOperations(c)

	// cleanup
	c.Assert(container.EnsureDead(), IsNil)
	c.Assert(container.Remove(), IsNil)
	c.Assert(m.EnsureDead(), IsNil)
	s.checkStopInstances(c, instance)
	s.waitRemoved(c, m)
}

func (s *ProvisionerSuite) TestProvisioningDoesNotOccurWithAnInvalidEnvironment(c *C) {
	err := s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// try to create a machine
	_, err = s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should not create it
	s.checkNoOperations(c)
}

func (s *ProvisionerSuite) TestProvisioningOccursWithFixedEnvironment(c *C) {
	err := s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// try to create a machine
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should not create it
	s.checkNoOperations(c)

	err = s.fixEnvironment()
	c.Assert(err, IsNil)

	s.checkStartInstance(c, m)
}

func (s *ProvisionerSuite) TestProvisioningDoesOccurAfterInvalidEnvironmentPublished(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// place a new machine into the state
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)

	s.checkStartInstance(c, m)

	err = s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	// create a second machine
	m, err = s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should create it using the old environment
	s.checkStartInstance(c, m)
}

func (s *ProvisionerSuite) TestProvisioningDoesNotProvisionTheSameMachineAfterRestart(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// create a machine
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	s.checkStartInstance(c, m)

	// restart the PA
	stop(c, p)
	p = s.newEnvironProvisioner("0")
	defer stop(c, p)

	// check that there is only one machine known
	machines, err := p.AllMachines()
	c.Assert(err, IsNil)
	c.Check(len(machines), Equals, 1)
	c.Check(machines[0].Id(), Equals, "0")

	// the PA should not create it a second time
	s.checkNoOperations(c)
}

func (s *ProvisionerSuite) TestProvisioningStopsInstances(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// create a machine
	m0, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	i0 := s.checkStartInstance(c, m0)

	// create a second machine
	m1, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	i1 := s.checkStartInstance(c, m1)
	stop(c, p)

	// mark the first machine as dead
	c.Assert(m0.EnsureDead(), IsNil)

	// remove the second machine entirely
	c.Assert(m1.EnsureDead(), IsNil)
	c.Assert(m1.Remove(), IsNil)

	// start a new provisioner to shut them both down
	p = s.newEnvironProvisioner("0")
	defer stop(c, p)
	s.checkStopInstances(c, i0, i1)
	s.waitRemoved(c, m0)
}

func (s *ProvisionerSuite) TestDyingMachines(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// provision a machine
	m0, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	s.checkStartInstance(c, m0)

	// stop the provisioner and make the machine dying
	stop(c, p)
	err = m0.Destroy()
	c.Assert(err, IsNil)

	// add a new, dying, unprovisioned machine
	m1, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	err = m1.Destroy()
	c.Assert(err, IsNil)

	// start the provisioner and wait for it to reap the useless machine
	p = s.newEnvironProvisioner("0")
	defer stop(c, p)
	s.checkNoOperations(c)
	s.waitRemoved(c, m1)

	// verify the other one's still fine
	err = m0.Refresh()
	c.Assert(err, IsNil)
	c.Assert(m0.Life(), Equals, state.Dying)
}

func (s *ProvisionerSuite) TestProvisioningRecoversAfterInvalidEnvironmentPublished(c *C) {
	p := s.newEnvironProvisioner("0")
	defer stop(c, p)

	// place a new machine into the state
	m, err := s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)
	s.checkStartInstance(c, m)

	err = s.invalidateEnvironment(c)
	c.Assert(err, IsNil)
	s.State.StartSync()

	// create a second machine
	m, err = s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should create it using the old environment
	s.checkStartInstance(c, m)

	err = s.fixEnvironment()
	c.Assert(err, IsNil)

	// insert our observer
	cfgObserver := make(chan *config.Config, 1)
	p.SetObserver(cfgObserver)

	cfg, err := s.State.EnvironConfig()
	c.Assert(err, IsNil)
	attrs := cfg.AllAttrs()
	attrs["secret"] = "beef"
	cfg, err = config.New(attrs)
	c.Assert(err, IsNil)
	err = s.State.SetEnvironConfig(cfg)

	s.State.StartSync()

	// wait for the PA to load the new configuration
	select {
	case <-cfgObserver:
	case <-time.After(200 * time.Millisecond):
		c.Fatalf("PA did not action config change")
	}

	// create a third machine
	m, err = s.State.AddMachine(config.DefaultSeries, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should create it using the new environment
	s.checkStartInstanceCustom(c, m, "beef", constraints.Value{})
}
