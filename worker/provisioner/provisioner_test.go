package provisioner_test

import (
	"labix.org/v2/mgo/bson"
	"time"

	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/environs/dummy"
	"launchpad.net/juju-core/juju/testing"
	"launchpad.net/juju-core/state"
	coretesting "launchpad.net/juju-core/testing"
	"launchpad.net/juju-core/trivial"
	"launchpad.net/juju-core/version"
	"launchpad.net/juju-core/worker/provisioner"
	stdtesting "testing"
)

func TestPackage(t *stdtesting.T) {
	coretesting.MgoTestPackage(t)
}

type ProvisionerSuite struct {
	testing.JujuConnSuite
	op  <-chan dummy.Operation
	cfg *config.Config
}

var _ = Suite(&ProvisionerSuite{})

var veryShortAttempt = trivial.AttemptStrategy{
	Total: 1 * time.Second,
	Delay: 80 * time.Millisecond,
}

func (s *ProvisionerSuite) SetUpTest(c *C) {
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

// invalidateEnvironment alters the environment configuration
// so the Settings returned from the watcher will not pass
// validation.
func (s *ProvisionerSuite) invalidateEnvironment(c *C) error {
	admindb := s.Session.DB("admin")
	err := admindb.Login("admin", testing.AdminSecret)
	if err != nil {
		err = admindb.Login("admin", trivial.PasswordHash(testing.AdminSecret))
	}
	c.Assert(err, IsNil)
	settings := s.Session.DB("juju").C("settings")
	return settings.UpdateId("e", bson.D{{"$unset", bson.D{{"type", 1}}}})
}

// fixEnvironment undoes the work of invalidateEnvironment.
func (s *ProvisionerSuite) fixEnvironment() error {
	return s.State.SetEnvironConfig(s.cfg)
}

func (s *ProvisionerSuite) stopProvisioner(c *C, p *provisioner.Provisioner) {
	c.Assert(p.Stop(), IsNil)
}

// checkStartInstance checks that an instance has been started
// with a machine id the same as m's, and that the machine's
// instance id has been set appropriately.
func (s *ProvisionerSuite) checkStartInstance(c *C, m *state.Machine, secret string) {
	s.State.StartSync()
	for {
		select {
		case o := <-s.op:
			switch o := o.(type) {
			case dummy.OpStartInstance:
				info := s.StateInfo(c)
				info.EntityName = m.EntityName()
				c.Assert(o.Info.Password, Not(HasLen), 0)
				info.Password = o.Info.Password
				c.Assert(o.Info, DeepEquals, info)

				// Check we can connect to the state with
				// the machine's entity name and password.
				st, err := state.Open(o.Info, state.DefaultDialTimeout)
				c.Assert(err, IsNil)
				st.Close()

				c.Assert(o.MachineId, Equals, m.Id())
				c.Assert(o.Instance, NotNil)
				s.checkInstanceId(c, m, o.Instance)
				c.Assert(o.Secret, Equals, secret)
				return
			default:
				c.Logf("ignoring unexpected operation %#v", o)
			}
		case <-time.After(2 * time.Second):
			c.Errorf("provisioner did not start an instance")
			return
		}
	}
}

// checkNotStartInstance checks that an instance was not started
func (s *ProvisionerSuite) checkNotStartInstance(c *C) {
	s.State.StartSync()
	for {
		select {
		case o := <-s.op:
			switch o.(type) {
			case dummy.OpStartInstance:
				c.Errorf("instance started: %v", o)
				return
			default:
				// ignore
			}
		case <-time.After(200 * time.Millisecond):
			return
		}
	}
}

// checkStopInstance checks that an instance has been stopped.
func (s *ProvisionerSuite) checkStopInstance(c *C) {
	s.State.StartSync()
	// use the non fatal variants to avoid leaking provisioners.
	for {
		select {
		case o := <-s.op:
			switch o.(type) {
			case dummy.OpStopInstances:
				return
			default:
				//ignore
			}
		case <-time.After(2 * time.Second):
			c.Errorf("provisioner did not stop an instance")
			return
		}
	}
}

// checkInstanceIdSet checks that the machine has an instance id
// that matches that of the given instance. If the instance is nil,
// It checks that the instance id is unset.
func (s *ProvisionerSuite) checkInstanceId(c *C, m *state.Machine, inst environs.Instance) {
	// TODO(dfc) add machine.Watch() to avoid having to poll.
	s.State.StartSync()
	var instId state.InstanceId
	if inst != nil {
		instId = inst.Id()
	}
	for a := veryShortAttempt.Start(); a.Next(); {
		err := m.Refresh()
		c.Assert(err, IsNil)
		if _, ok := m.InstanceId(); ok {
			break
		}
		if inst == nil {
			return
		}
	}
	id, ok := m.InstanceId()
	c.Assert(ok, Equals, true)
	c.Assert(id, Equals, instId)
}

func (s *ProvisionerSuite) TestProvisionerStartStop(c *C) {
	p := provisioner.NewProvisioner(s.State)
	c.Assert(p.Stop(), IsNil)
}

// Start and stop one machine, watch the PA.
func (s *ProvisionerSuite) TestSimple(c *C) {
	p := provisioner.NewProvisioner(s.State)
	defer s.stopProvisioner(c, p)

	// place a new machine into the state
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	// now mark it as dying
	c.Assert(m.EnsureDead(), IsNil)

	// watch the PA remove it
	s.checkStopInstance(c)
}

func (s *ProvisionerSuite) TestProvisioningDoesNotOccurWithAnInvalidEnvironment(c *C) {
	err := s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	p := provisioner.NewProvisioner(s.State)
	defer s.stopProvisioner(c, p)

	// try to create a machine
	_, err = s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should not create it
	s.checkNotStartInstance(c)
}

func (s *ProvisionerSuite) TestProvisioningOccursWithFixedEnvironment(c *C) {
	err := s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	p := provisioner.NewProvisioner(s.State)
	defer s.stopProvisioner(c, p)

	// try to create a machine
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should not create it
	s.checkNotStartInstance(c)

	err = s.fixEnvironment()
	c.Assert(err, IsNil)

	s.checkStartInstance(c, m, "pork")
}

func (s *ProvisionerSuite) TestProvisioningDoesOccurAfterInvalidEnvironmentPublished(c *C) {
	p := provisioner.NewProvisioner(s.State)
	defer s.stopProvisioner(c, p)

	// place a new machine into the state
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	err = s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	// create a second machine
	m, err = s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should create it using the old environment
	s.checkStartInstance(c, m, "pork")
}

func (s *ProvisionerSuite) TestProvisioningDoesNotProvisionTheSameMachineAfterRestart(c *C) {
	p := provisioner.NewProvisioner(s.State)
	// we are not using defer s.stopProvisioner(c, p) because we need to control when
	// the PA is restarted in this test. tf. Methods like Fatalf and Assert should not be used.

	// create a machine
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Check(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	// restart the PA
	c.Check(p.Stop(), IsNil)

	p = provisioner.NewProvisioner(s.State)

	// check that there is only one machine known
	machines, err := p.AllMachines()
	c.Check(err, IsNil)
	c.Check(len(machines), Equals, 1)
	c.Check(machines[0].Id(), Equals, "0")

	// the PA should not create it a second time
	s.checkNotStartInstance(c)

	c.Assert(p.Stop(), IsNil)
}

func (s *ProvisionerSuite) TestProvisioningStopsUnknownInstances(c *C) {
	p := provisioner.NewProvisioner(s.State)
	// we are not using defer s.stopProvisioner(c, p) because we need to control when
	// the PA is restarted in this test. Methods like Fatalf and Assert should not be used.

	// create a machine
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Check(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	// create a second machine
	m, err = s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Check(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	// stop the PA
	c.Check(p.Stop(), IsNil)

	// mark the machine as dead
	c.Assert(m.EnsureDead(), IsNil)

	// start a new provisioner
	p = provisioner.NewProvisioner(s.State)

	s.checkStopInstance(c)

	c.Assert(p.Stop(), IsNil)
}

// This check is different from the one above as it catches the edge case
// where the final machine has been removed from the state while the PA was
// not running.
func (s *ProvisionerSuite) TestProvisioningStopsOnlyUnknownInstances(c *C) {
	p := provisioner.NewProvisioner(s.State)
	// we are not using defer s.stopProvisioner(c, p) because we need to control when
	// the PA is restarted in this test. Methods like Fatalf and Assert should not be used.

	// create a machine
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Check(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	// stop the PA
	c.Check(p.Stop(), IsNil)

	// mark the machine as dead
	c.Assert(m.EnsureDead(), IsNil)

	// start a new provisioner
	p = provisioner.NewProvisioner(s.State)

	s.checkStopInstance(c)

	c.Assert(p.Stop(), IsNil)
}

func (s *ProvisionerSuite) TestProvisioningRecoversAfterInvalidEnvironmentPublished(c *C) {
	p := provisioner.NewProvisioner(s.State)
	defer s.stopProvisioner(c, p)

	// place a new machine into the state
	m, err := s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	s.checkStartInstance(c, m, "pork")

	err = s.invalidateEnvironment(c)
	c.Assert(err, IsNil)

	s.State.StartSync()

	// create a second machine
	m, err = s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should create it using the old environment
	s.checkStartInstance(c, m, "pork")

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
	m, err = s.State.AddMachine(version.Current.Series, state.JobHostUnits)
	c.Assert(err, IsNil)

	// the PA should create it using the new environment
	s.checkStartInstance(c, m, "beef")
}
