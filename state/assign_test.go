// Copyright 2012, 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package state_test

import (
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/constraints"
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

func (s *AssignSuite) addSubordinate(c *C, principal *state.Unit) *state.Unit {
	_, err := s.State.AddService("logging", s.AddTestingCharm(c, "logging"))
	c.Assert(err, IsNil)
	eps, err := s.State.InferEndpoints([]string{"logging", "wordpress"})
	c.Assert(err, IsNil)
	rel, err := s.State.AddRelation(eps...)
	c.Assert(err, IsNil)
	ru, err := rel.Unit(principal)
	c.Assert(err, IsNil)
	err = ru.EnterScope(nil)
	c.Assert(err, IsNil)
	subUnit, err := s.State.Unit("logging/0")
	c.Assert(err, IsNil)
	return subUnit
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
	machineOne, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	machineTwo, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)

	err = unit.AssignToMachine(machineOne)
	c.Assert(err, IsNil)

	// Assigning the unit to the same machine should return no error.
	err = unit.AssignToMachine(machineOne)
	c.Assert(err, IsNil)

	// Assigning the unit to a different machine should fail.
	err = unit.AssignToMachine(machineTwo)
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to machine 1: unit is already assigned to a machine`)

	machineId, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	c.Assert(machineId, Equals, "0")
}

func (s *AssignSuite) TestAssignedMachineIdWhenNotAlive(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
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
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)

	subUnit := s.addSubordinate(c, unit)
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
	// Check that assigning a principal unit assigns its subordinates too.
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	subUnit := s.addSubordinate(c, unit)

	// None of the direct unit assign methods work on subordinates.
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	err = subUnit.AssignToMachine(machine)
	c.Assert(err, ErrorMatches, `cannot assign unit "logging/0" to machine 0: unit is a subordinate`)
	_, err = subUnit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "logging/0" to unused machine: unit is a subordinate`)
	err = subUnit.AssignToNewMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "logging/0" to new machine: unit is a subordinate`)

	// Subordinates know the machine they're indirectly assigned to.
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)
	id, err := subUnit.AssignedMachineId()
	c.Assert(err, IsNil)
	c.Check(id, Equals, machine.Id())

	// Unassigning the principal unassigns the subordinates too.
	err = unit.UnassignFromMachine()
	c.Assert(err, IsNil)
	_, err = subUnit.AssignedMachineId()
	c.Assert(err, ErrorMatches, `unit "logging/0" is not assigned to a machine`)
}

func (s *AssignSuite) TestDeployerTag(c *C) {
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	principal, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	subordinate := s.addSubordinate(c, principal)

	assertDeployer := func(u *state.Unit, d state.Tagger) {
		err := u.Refresh()
		c.Assert(err, IsNil)
		name, ok := u.DeployerTag()
		if d == nil {
			c.Assert(ok, Equals, false)
		} else {
			c.Assert(ok, Equals, true)
			c.Assert(name, Equals, d.Tag())
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

func (s *AssignSuite) TestDirectAssignIgnoresConstraints(c *C) {
	// Set up constraints.
	scons := constraints.MustParse("mem=2G cpu-power=400")
	err := s.wordpress.SetConstraints(scons)
	c.Assert(err, IsNil)
	econs := constraints.MustParse("mem=4G cpu-cores=2")
	err = s.State.SetEnvironConstraints(econs)
	c.Assert(err, IsNil)

	// Machine will take environment constraints on creation.
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)

	// Unit will take combined service/environ constraints on creation.
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Machine keeps its original constraints on direct assignment.
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)
	mcons, err := machine.Constraints()
	c.Assert(err, IsNil)
	c.Assert(mcons, DeepEquals, econs)
}

func (s *AssignSuite) TestAssignBadSeries(c *C) {
	machine, err := s.State.AddMachine("burble", state.JobHostUnits)
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to machine 0: series does not match`)
}

func (s *AssignSuite) TestAssignMachineWhenDying(c *C) {
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)

	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	subUnit := s.addSubordinate(c, unit)
	assignTest := func() error {
		err := unit.AssignToMachine(machine)
		c.Assert(unit.UnassignFromMachine(), IsNil)
		if subUnit != nil {
			err := subUnit.EnsureDead()
			c.Assert(err, IsNil)
			err = subUnit.Remove()
			c.Assert(err, IsNil)
			subUnit = nil
		}
		return err
	}
	expect := ".*: unit is not alive"
	testWhenDying(c, unit, expect, expect, assignTest)

	expect = ".*: machine is not alive"
	unit, err = s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	testWhenDying(c, machine, expect, expect, assignTest)
}

func (s *AssignSuite) TestAssignMachinePrincipalsChange(c *C) {
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)
	unit, err = s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(machine)
	c.Assert(err, IsNil)
	subUnit := s.addSubordinate(c, unit)

	doc := make(map[string][]string)
	s.ConnSuite.machines.FindId(machine.Id()).One(&doc)
	principals, ok := doc["principals"]
	if !ok {
		c.Errorf(`machine document does not have a "principals" field`)
	}
	c.Assert(principals, DeepEquals, []string{"wordpress/0", "wordpress/1"})

	err = subUnit.EnsureDead()
	c.Assert(err, IsNil)
	err = subUnit.Remove()
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
	_, err := s.State.AddMachine("series", state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)

	// Add some units to another service and allocate them to machines
	service1, err := s.State.AddService("mysql", s.AddTestingCharm(c, "mysql"))
	c.Assert(err, IsNil)
	units := make([]*state.Unit, 3)
	for i := range units {
		u, err := service1.AddUnit()
		c.Assert(err, IsNil)
		m, err := s.State.AddMachine("series", state.JobHostUnits)
		c.Assert(err, IsNil)
		err = u.AssignToMachine(m)
		c.Assert(err, IsNil)
		units[i] = u
	}

	// Create a new, unused machine.
	machine, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	c.Assert(machine.Clean(), Equals, true)

	// Check that AssignToUnusedMachine finds the newly created, unused machine.
	newService, err := s.State.AddService("riak", s.AddTestingCharm(c, "riak"))
	c.Assert(err, IsNil)
	newUnit, err := newService.AddUnit()
	c.Assert(err, IsNil)
	reusedMachine, err := newUnit.AssignToUnusedMachine()
	c.Assert(err, IsNil)
	c.Assert(reusedMachine.Id(), Equals, machine.Id())
	c.Assert(reusedMachine.Clean(), Equals, false)

	// Check that it fails when called again, even when there's an available machine
	m, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	_, err = newUnit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "riak/0" to unused machine: unit is already assigned to a machine`)
	err = m.EnsureDead()
	c.Assert(err, IsNil)
	err = m.Remove()
	c.Assert(err, IsNil)
}

func (s *AssignSuite) TestAssignToUnusedMachineNoneAvailable(c *C) {
	// Try to assign a unit to an unused machine and check that we can't.
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	m, err := unit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all eligible machines in use`)

	// Add a dying machine and check that it is not chosen.
	m, err = s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	err = m.Destroy()
	c.Assert(err, IsNil)
	m, err = unit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all eligible machines in use`)

	// Add a non-unit-hosting machine and check it is not chosen.
	m, err = s.State.AddMachine("series", state.JobManageEnviron)
	c.Assert(err, IsNil)
	m, err = unit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all eligible machines in use`)

	// Add a machine with the wrong series and check it is not chosen.
	m, err = s.State.AddMachine("anotherseries", state.JobHostUnits)
	c.Assert(err, IsNil)
	m, err = unit.AssignToUnusedMachine()
	c.Assert(m, IsNil)
	c.Assert(err, ErrorMatches, `all eligible machines in use`)
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineWithRemovedService(c *C) {
	_, err := s.State.AddMachine("series", state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Fail if service is removed.
	removeAllUnits(c, s.wordpress)
	err = s.wordpress.Destroy()
	c.Assert(err, IsNil)
	_, err = s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	_, err = unit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to unused machine.*: unit "wordpress/0" not found`)
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineWithRemovedUnit(c *C) {
	_, err := s.State.AddMachine("series", state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Fail if unit is removed.
	err = unit.EnsureDead()
	c.Assert(err, IsNil)
	err = unit.Remove()
	c.Assert(err, IsNil)
	_, err = s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)

	_, err = unit.AssignToUnusedMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to unused machine.*: unit "wordpress/0" not found`)
}

func (s *AssignSuite) TestAssignUnitToUnusedMachineWorksWithMachine0(c *C) {
	m, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)
	c.Assert(m.Id(), Equals, "0")
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	assignedTo, err := unit.AssignToUnusedMachine()
	c.Assert(err, IsNil)
	c.Assert(assignedTo.Id(), Equals, "0")
}

func (s *AssignSuite) TestAssignUnitToNewMachine(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	err = unit.AssignToNewMachine()
	c.Assert(err, IsNil)
	// Check the machine on the unit is set.
	machineId, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	// Check that the principal is set on the machine.
	machine, err := s.State.Machine(machineId)
	c.Assert(err, IsNil)
	machineUnits, err := machine.Units()
	c.Assert(err, IsNil)
	c.Assert(machineUnits, HasLen, 1)
	// Make sure it is the right unit.
	c.Assert(machineUnits[0].Name(), Equals, unit.Name())
}

func (s *AssignSuite) TestAssignToNewMachineMakesDirty(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	err = unit.AssignToNewMachine()
	c.Assert(err, IsNil)
	mid, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	machine, err := s.State.Machine(mid)
	c.Assert(err, IsNil)
	c.Assert(machine.Clean(), Equals, false)
}

func (s *AssignSuite) TestAssignUnitToNewMachineSetsConstraints(c *C) {
	// Set up constraints.
	scons := constraints.MustParse("mem=2G cpu-power=400")
	err := s.wordpress.SetConstraints(scons)
	c.Assert(err, IsNil)
	econs := constraints.MustParse("mem=4G cpu-cores=2")
	err = s.State.SetEnvironConstraints(econs)
	c.Assert(err, IsNil)

	// Unit will take combined service/environ constraints on creation.
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Change service/env constraints before assigning, to verify this.
	scons = constraints.MustParse("mem=6G cpu-power=800")
	err = s.wordpress.SetConstraints(scons)
	c.Assert(err, IsNil)
	econs = constraints.MustParse("cpu-cores=4")
	err = s.State.SetEnvironConstraints(econs)
	c.Assert(err, IsNil)

	// The new machine takes the original combined unit constraints.
	err = unit.AssignToNewMachine()
	c.Assert(err, IsNil)
	err = unit.Refresh()
	c.Assert(err, IsNil)
	mid, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	machine, err := s.State.Machine(mid)
	c.Assert(err, IsNil)
	mcons, err := machine.Constraints()
	c.Assert(err, IsNil)
	expect := constraints.MustParse("mem=2G cpu-cores=2 cpu-power=400")
	c.Assert(mcons, DeepEquals, expect)
}

func (s *AssignSuite) TestAssignUnitToNewMachineUnusedAvailable(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Add an unused machine.
	unused, err := s.State.AddMachine("series", state.JobHostUnits)
	c.Assert(err, IsNil)

	err = unit.AssignToNewMachine()
	c.Assert(err, IsNil)
	// Check the machine on the unit is set.
	machineId, err := unit.AssignedMachineId()
	c.Assert(err, IsNil)
	// Check that the machine isn't our unused one.
	machine, err := s.State.Machine(machineId)
	c.Assert(err, IsNil)
	c.Assert(machine.Id(), Not(Equals), unused.Id())
}

func (s *AssignSuite) TestAssignUnitToNewMachineAlreadyAssigned(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	// Make the unit assigned
	err = unit.AssignToNewMachine()
	c.Assert(err, IsNil)
	// Try to assign it again
	err = unit.AssignToNewMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to new machine: unit is already assigned to a machine`)
}

func (s *AssignSuite) TestAssignUnitToNewMachineUnitNotAlive(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	subUnit := s.addSubordinate(c, unit)

	// Try to assign a dying unit...
	err = unit.Destroy()
	c.Assert(err, IsNil)
	err = unit.AssignToNewMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to new machine: unit is not alive`)

	// ...and a dead one.
	err = subUnit.EnsureDead()
	c.Assert(err, IsNil)
	err = subUnit.Remove()
	c.Assert(err, IsNil)
	err = unit.EnsureDead()
	c.Assert(err, IsNil)
	err = unit.AssignToNewMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to new machine: unit is not alive`)
}

func (s *AssignSuite) TestAssignUnitToNewMachineUnitRemoved(c *C) {
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)
	err = unit.Destroy()
	c.Assert(err, IsNil)
	err = unit.AssignToNewMachine()
	c.Assert(err, ErrorMatches, `cannot assign unit "wordpress/0" to new machine: unit not found`)
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
	m, err := s.State.AddMachine("series", state.JobManageEnviron, state.JobHostUnits) // bootstrap machine
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

func (s *AssignSuite) TestAssignUnitNewPolicy(c *C) {
	_, err := s.State.AddMachine("series", state.JobHostUnits) // available machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	err = s.State.AssignUnit(unit, state.AssignNew)
	c.Assert(err, IsNil)
	assertMachineCount(c, s.State, 2)
}

func (s *AssignSuite) TestAssignUnitUnusedPolicy(c *C) {
	_, err := s.State.AddMachine("series", state.JobManageEnviron) // bootstrap machine
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

		// Sanity check that the machine knows about its assigned unit and was
		// created with the appropriate series.
		m, err := s.State.Machine(mid)
		c.Assert(err, IsNil)
		units, err := m.Units()
		c.Assert(err, IsNil)
		c.Assert(units, HasLen, 1)
		c.Assert(units[0].Name(), Equals, unit.Name())
		c.Assert(m.Series(), Equals, "series")
	}

	// Remove units from alternate machines. These machines will still be
	// considered as dirty so will continue to be ignored by the policy.
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
	}
	// Add some more unused machines
	for i := 0; i < 4; i++ {
		m, err := s.State.AddMachine("series", state.JobHostUnits)
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
	_, err := s.State.AddMachine("series", state.JobManageEnviron) // bootstrap machine
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

func (s *AssignSuite) TestAssignUnitWithSubordinate(c *C) {
	_, err := s.State.AddMachine("series", state.JobManageEnviron) // bootstrap machine
	c.Assert(err, IsNil)
	unit, err := s.wordpress.AddUnit()
	c.Assert(err, IsNil)

	// Check cannot assign subordinates to machines
	subUnit := s.addSubordinate(c, unit)
	for _, policy := range []state.AssignmentPolicy{
		state.AssignLocal, state.AssignNew, state.AssignUnused,
	} {
		err = s.State.AssignUnit(subUnit, policy)
		c.Assert(err, ErrorMatches, `subordinate unit "logging/0" cannot be assigned directly to a machine`)
	}
}

func assertMachineCount(c *C, st *state.State, expect int) {
	ms, err := st.AllMachines()
	c.Assert(err, IsNil)
	c.Assert(ms, HasLen, expect, Commentf("%v", ms))
}
