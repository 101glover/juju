package state_test

import (
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/state"
	"sort"
	"strconv"
	"time"
)

type AssignSuite struct {
	ConnSuite
	wordpress *state.Service
}

var _ = Suite(&AssignSuite{})

func (s *AssignSuite) SetUpTest(c *C) {
	s.ConnSuite.SetUpTest(c)
	wordpress, err := s.State.AddService("wordpress", s.AddTestingCharm(c, "wordpress"))
	c.Assert(err, IsNil)
	s.wordpress = wordpress
}

func (s *AssignSuite) TestUnassignUnitFromMachineWithoutBeingAssigned(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// When unassigning a machine from a unit, it is possible that
	// the machine has not been previously assigned, or that it
	// was assigned but the state changed beneath us.  In either
	// case, the end state is the intended state, so we simply
	// move forward without any errors here, to avoid having to
	// handle the extra complexity of dealing with the concurrency
	// problems.
	err = unit.UnassignFromMachine()
	c.Assert(err, IsNil)

	// Check that the unit has no machine assigned.
	_, err = unit.AssignedMachineId()
	c.Assert(err, ErrorMatches, `unit "wordpress/0" is not assigned to a machine`)
}

func (s *AssignSuite) TestAssignUnitToMachineAgainFails(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Check that assigning an already assigned unit to
	// a machine fails if it isn't precisely the same
	// machine.
	machineOne, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	machineTwo, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)

	err = unit.AssignToMachine(machineOne)
	c.Assert(err, IsNil)

	// Assigning the unit to the same machine should return no error.
	err = unit.AssignToMachine(machineOne)
	c.Assert(err, IsNil)

	// Assigning the unit to a different machine should fail.
	// BUG(aram): use error strings from state.
	err = unit.AssignToMachine(machineTwo)
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to machine 1: .*`)

	machineId, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	c.Assert(machineId, Equals, "0")
}

func (s *AssignSuite) TestAssignedMachineIdWhenNotAlive(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)

	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)

	testWhenDying(c, unit, noErr, noErr,
		func() error {
			_, err = unit.AssignedMachineId()
			return err
		})
}

func (s *AssignSuite) TestAssignedMachineIdWhenPrincipalNotAlive(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)

	subCharm := s.AddTestingCharm(c, "logging")
	subSvc, err := s.State.AddService("logging", subCharm)
	c.Assert(err, IsNil)
	subUnit, err := subSvc.AddUnitSubordinateTo(unit)
	c.Assert(err, IsNil)

	err = unit.Destroy()
	c.Assert(err, IsNil)
	mid, err := subUnit.AssignedMachineId()
	c.Assert(err, IsNil)
	c.Assert(mid, Equals, machine.Id())
}

func (s *AssignSuite) TestUnassignUnitFromMachineWithChangingState(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Check that unassigning while the state changes fails nicely.
	// Remove the unit for the tests.
	err = unit.EnsureDead()
	c.Assert(err, IsNil)
	err = unit.Remove()
	c.Assert(err, IsNil)

	err = unit.UnassignFromMachine()
	c.Assert(err, ErrorMatches, `cannot unassign unit "wordpress/0" from machine: .*`)
	_, err = unit.AssignedMachineId()
	c.Assert(err, ErrorMatches, `unit "wordpress/0" is not assigned to a machine`)

	err = s.wordpress.Destroy()
	c.Assert(err, IsNil)
	err = unit.UnassignFromMachine()
	c.Assert(err, ErrorMatches, `cannot unassign unit "wordpress/0" from machine: .*`)
	_, err = unit.AssignedMachineId()
	c.Assert(err, ErrorMatches, `unit "wordpress/0" is not assigned to a machine`)
}

func (s *AssignSuite) TestAssignSubordinatesToMachine(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Check that assigning a principal unit assigns its subordinates too.
	subCharm := s.AddTestingCharm(c, "logging")
	logService1, err := s.State.AddService("logging1", subCharm)
	c.Assert(err, IsNil)
	logService2, err := s.State.AddService("logging2", subCharm)
	c.Assert(err, IsNil)
	log1Unit, err := logService1.AddUnitSubordinateTo(unit)
	c.Assert(err, IsNil)
	log2Unit, err := logService2.AddUnitSubordinateTo(unit)
	c.Assert(err, IsNil)

	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = log1Unit.AssignToMachine(machine)
	c.Assert(err, ErrorMatches, ".*: unit is a subordinate")
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)

	id, err := log1Unit.AssignedMachineId()
	c.Assert(err, IsNil)
	c.Check(id, Equals, machine.Id())
	id, err = log2Unit.AssignedMachineId()
	c.Check(id, Equals, machine.Id())

	// Check that unassigning the principal unassigns the
	// subordinates too.
	err = unit.UnassignFromMachine()
	c.Assert(err, IsNil)
	_, err = log1Unit.AssignedMachineId()
	c.Assert(err, ErrorMatches, `unit "logging1/0" is not assigned to a machine`)
	_, err = log2Unit.AssignedMachineId()
	c.Assert(err, ErrorMatches, `unit "logging2/0" is not assigned to a machine`)
}

func (s *AssignSuite) TestDeployerName(c *C) {
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	principal, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	logging, err := s.State.AddService("logging", s.AddTestingCharm(c, "logging"))
	c.Assert(err, IsNil)
	subordinate, err := logging.AddUnitSubordinateTo(principal)
	c.Assert(err, IsNil)

	assertDeployer := func(u *state.Unit, d entityNamer) {
		err := u.Refresh()
		c.Assert(err, IsNil)
		name, ok := u.DeployerName()
		if d == nil {
			c.Assert(ok, Equals, false)
		} else {
			c.Assert(ok, Equals, true)
			c.Assert(name, Equals, d.EntityName())
		}
	}
	assertDeployer(subordinate, principal)
	assertDeployer(principal, nil)

	err = principal.AssignToMachine(machine)
	c.Assert(err, IsNil)
	assertDeployer(subordinate, principal)
	assertDeployer(principal, machine)

	err = principal.UnassignFromMachine()
	c.Assert(err, IsNil)
	assertDeployer(subordinate, principal)
	assertDeployer(principal, nil)
}

type entityNamer interface {
	EntityName() string
}

func (s *AssignSuite) TestAssignMachineWhenDying(c *C) {
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)

	const unitDeadErr = ".*: unit is dead"
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	assignTest := func() error {
		err := unit.AssignToMachine(machine)
		err1 := unit.UnassignFromMachine()
		c.Assert(err1, IsNil)
		return err
	}
	testWhenDying(c, unit, unitDeadErr, unitDeadErr, assignTest)

	const machineDeadErr = ".*: machine is dead"
	unit, err = s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	testWhenDying(c, machine, machineDeadErr, machineDeadErr, assignTest)
}

func (s *AssignSuite) TestAssignMachinePrincipalsChange(c *C) {
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)
	unit, err = s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)
	subCharm := s.AddTestingCharm(c, "logging")
	logService, err := s.State.AddService("logging", subCharm)
	c.Assert(err, IsNil)
	logUnit, err := logService.AddUnitSubordinateTo(unit)
	c.Assert(err, IsNil)

	doc := make(map[string][]string)
	s.ConnSuite.machines.FindId(machine.Id()).One(&doc)
	principals, ok := doc["principals"]
	if !ok {
		c.Errorf(`machine document does not have a "principals" field`)
	}
	c.Assert(principals, DeepEquals, []string{"wordpress/0", "wordpress/1"})

	err = logUnit.EnsureDead()
	c.Assert(err, IsNil)
	err = logUnit.Remove()
	c.Assert(err, IsNil)
	err = unit.EnsureDead()
	c.Assert(err, IsNil)
	err = unit.Remove()
	c.Assert(err, IsNil)
	doc = make(map[string][]string)
	s.ConnSuite.machines.FindId(machine.Id()).One(&doc)
	principals, ok = doc["principals"]
	if !ok {
		c.Errorf(`machine document does not have a "principals" field`)
	}
	c.Assert(principals, DeepEquals, []string{"wordpress/0"})
}

func (s *AssignSuite) TestAssignUnitToUnusedMachine(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Add some units to another service and allocate them to machines
	service1, err := s.State.AddService("mysql", s.AddTestingCharm(c, "mysql"))
	c.Assert(err, IsNil)
	units := make([]*state.Unit, 3)
	for i := range units {
		u, err := service1.AddUnit()
		c.Assert(err, IsNil)
		m, err := s.State.AddMachine(state.JobHostUnits)
		c.Assert(err, IsNil)
		err = u.AssignToMachine(m)
		c.Assert(err, IsNil)
		units[i] = u
	}

	// Assign the suite's unit to a machine, then remove the unit
	// so the machine becomes available again.
	origMachine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(origMachine)
	c.Assert(err, IsNil)
	err = unit.EnsureDead()
	c.Assert(err, IsNil)
	err = unit.Remove()
	c.Assert(err, IsNil)
	err = s.wordpress.Destroy()
	c.Assert(err, IsNil)

	// Check that AssignToUnusedMachine finds the old (now unused) machine.
	newService, err := s.State.AddService("riak", s.AddTestingCharm(c, "riak"))
	c.Assert(err, IsNil)
	newUnit, err := newService.AddUnit()
	c.Assert(err, IsNil)
	reusedMachine, err := newUnit.AssignToUnusedMachine()
	c.Assert(err, IsNil)
	c.Assert(reusedMachine.Id(), Equals, origMachine.Id())

	// Check that it fails when called again, even when there's an available machine
	m, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	_, err = newUnit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "riak/0" to unused machine: unit is already assigned to a machine`)
	err = m.EnsureDead()
	c.Assert(err, IsNil)
	err = m.Remove()
	c.Assert(err, IsNil)

	// Try to assign another unit to an unused machine
	// and check that we can't
	newUnit, err = newService.AddUnit()
	c.Assert(err, IsNil)
	m, err = newUnit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all machines in use`)

	// Add a dying machine and check that it is not chosen.
	m, err = s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = m.Destroy()
	c.Assert(err, IsNil)
	m, err = newUnit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all machines in use`)

	// Add another non-unit-hosting machine and check it is not chosen.
	m, err = s.State.AddMachine(state.JobManageEnviron)
	c.Assert(err, IsNil)
	m, err = newUnit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all machines in use`)
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineWithRemovedService(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Fail if service is removed.
	removeAllUnits(c, s.wordpress)
	err = s.wordpress.Destroy()
	c.Assert(err, IsNil)
	_, err = s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	_, err = unit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to unused machine.*: unit "wordpress/0" not found`)
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineWithRemovedUnit(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Fail if unit is removed.
	err = unit.EnsureDead()
	c.Assert(err, IsNil)
	err = unit.Remove()
	c.Assert(err, IsNil)
	_, err = s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)

	_, err = unit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to unused machine.*: unit "wordpress/0" not found`)
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineWorksWithMachine0(c *C) {
	m, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	c.Assert(m.Id(), Equals, "0")
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	assignedTo, err := unit.AssignToUnusedMachine()
	c.Assert(err, IsNil)
	c.Assert(assignedTo.Id(), Equals, "0")
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineNoneAvailable(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Check that assigning without unused machine fails.
	m1, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(m1)
	c.Assert(err, IsNil)

	newUnit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	_, err = newUnit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `all machines in use`)
}

func (s *AssignSuite) TestAssignUnitBadPolicy(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Check nonsensical policy
	fail := func() { s.State.AssignUnit(unit, state.AssignmentPolicy("random")) }
	c.Assert(fail, PanicMatches, `unknown unit assignment policy: "random"`)
	_, err = unit.AssignedMachineId()
	c.Assert(err, NotNil)
	assertMachineCount(c, s.State, 0)
}

func (s *AssignSuite) TestAssignUnitLocalPolicy(c *C) {
	m, err := s.State.AddMachine(state.JobManageEnviron, state.JobHostUnits) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	for i := 0; i < 2; i++ {
		err = s.State.AssignUnit(unit, state.AssignLocal)
		c.Assert(err, IsNil)
		mid, err := unit.AssignedMachineId()
		c.Assert(err, IsNil)
		c.Assert(mid, Equals, m.Id())
		assertMachineCount(c, s.State, 1)
	}
}

func (s *AssignSuite) TestAssignUnitUnusedPolicy(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)

	// Check unassigned placements with no unused machines.
	for i := 0; i < 10; i++ {
		unit, err := s.wordpress.AddUnit()
		c.Assert(err, IsNil)
		err = s.State.AssignUnit(unit, state.AssignUnused)
		c.Assert(err, IsNil)
		mid, err := unit.AssignedMachineId()
		c.Assert(err, IsNil)
		c.Assert(mid, Equals, strconv.Itoa(1+i))
		assertMachineCount(c, s.State, i+2)

		// Sanity check that the machine knows about its assigned unit.
		m, err := s.State.Machine(mid)
		c.Assert(err, IsNil)
		units, err := m.Units()
		c.Assert(err, IsNil)
		c.Assert(units, HasLen, 1)
		c.Assert(units[0].Name(), Equals, unit.Name())
	}

	// Remove units from alternate machines.
	var unused []string
	for i := 1; i < 11; i += 2 {
		mid := strconv.Itoa(i)
		m, err := s.State.Machine(mid)
		c.Assert(err, IsNil)
		units, err := m.Units()
		c.Assert(err, IsNil)
		c.Assert(units, HasLen, 1)
		unit := units[0]
		err = unit.UnassignFromMachine()
		c.Assert(err, IsNil)
		err = unit.Destroy()
		c.Assert(err, IsNil)
		unused = append(unused, mid)
	}
	// Add some more unused machines
	for i := 0; i < 4; i++ {
		m, err := s.State.AddMachine(state.JobHostUnits)
		c.Assert(err, IsNil)
		unused = append(unused, m.Id())
	}

	// Assign units to all the unused machines.
	var got []string
	for _ = range unused {
		unit, err := s.wordpress.AddUnit()
		c.Assert(err, IsNil)
		err = s.State.AssignUnit(unit, state.AssignUnused)
		c.Assert(err, IsNil)
		mid, err := unit.AssignedMachineId()
		c.Assert(err, IsNil)
		got = append(got, mid)
	}
	sort.Strings(unused)
	sort.Strings(got)
	c.Assert(got, DeepEquals, unused)
}

func (s *AssignSuite) TestAssignUnitUnusedPolicyConcurrently(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	us := make([]*state.Unit, 50)
	for i := range us {
		us[i], err = s.wordpress.AddUnit()
		c.Assert(err, IsNil)
	}
	type result struct {
		u   *state.Unit
		err error
	}
	done := make(chan result)
	for i, u := range us {
		i, u := i, u
		go func() {
			// Start the AssignUnit at different times
			// to increase the likeliness of a race.
			time.Sleep(time.Duration(i) * time.Millisecond / 2)
			err := s.State.AssignUnit(u, state.AssignUnused)
			done <- result{u, err}
		}()
	}
	assignments := make(map[string][]*state.Unit)
	for _ = range us {
		r := <-done
		if !c.Check(r.err, IsNil) {
			continue
		}
		id, err := r.u.AssignedMachineId()
		c.Assert(err, IsNil)
		assignments[id] = append(assignments[id], r.u)
	}
	for id, us := range assignments {
		if len(us) != 1 {
			c.Errorf("machine %s expected one unit, got %q", id, us)
		}
	}
	c.Assert(assignments, HasLen, len(us))
}

func (s *AssignSuite) TestAssignSubordinate(c *C) {
	_, err := s.State.AddMachine(state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Check cannot assign subordinates to machines
	subCharm := s.AddTestingCharm(c, "logging")
	logging, err := s.State.AddService("logging", subCharm)
	c.Assert(err, IsNil)
	unit2, err := logging.AddUnitSubordinateTo(unit)
	c.Assert(err, IsNil)
	err = s.State.AssignUnit(unit2, state.AssignUnused)
	c.Assert(err, ErrorMatches, `subordinate unit "logging/0" cannot be assigned directly to a machine`)
}

func assertMachineCount(c *C, st *state.State, expect int) {
	ms, err := st.AllMachines()
	c.Assert(err, IsNil)
	c.Assert(ms, HasLen, expect, Commentf("%v", ms))
}
