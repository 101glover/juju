package state_test

import (
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/version"
	"sort"
	"time"
)

type MachineSuite struct {
	ConnSuite
	machine *state.Machine
}

var _ = Suite(&MachineSuite{})

func (s *MachineSuite) SetUpTest(c *C) {
	s.ConnSuite.SetUpTest(c)
	var err error
	s.machine, err = s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
}

func (s *MachineSuite) TestLifeJobManageEnviron(c *C) {
	// A JobManageEnviron machine must never advance lifecycle.
	m, err := s.State.AddMachine(state.JobManageEnviron)
	c.Assert(err, IsNil)
	err = m.EnsureDying()
	c.Assert(err, ErrorMatches, "machine 1 cannot become dying: required by environment")
	err = m.EnsureDead()
	c.Assert(err, ErrorMatches, "machine 1 cannot become dead: required by environment")
}

func (s *MachineSuite) TestLifeJobHostUnits(c *C) {
	// A machine with an assigned unit must not advance lifecycle.
	svc, err := s.State.AddService("wordpress", s.AddTestingCharm(c, "wordpress"))
	c.Assert(err, IsNil)
	unit, err := svc.AddUnit()
	c.Assert(err, IsNil)
	err = unit.AssignToMachine(s.machine)
	c.Assert(err, IsNil)
	err = s.machine.EnsureDying()
	c.Assert(err, ErrorMatches, `machine 0 cannot become dying: unit "wordpress/0" is assigned to it`)
	err = s.machine.EnsureDead()
	c.Assert(err, ErrorMatches, `machine 0 cannot become dead: unit "wordpress/0" is assigned to it`)

	// Once no unit is assigned, lifecycle can advance.
	err = unit.UnassignFromMachine()
	c.Assert(err, IsNil)
	err = s.machine.EnsureDying()
	c.Assert(err, IsNil)
	err = s.machine.EnsureDead()
	c.Assert(err, IsNil)

	// A machine that has never had units assigned can advance lifecycle.
	m, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = m.EnsureDying()
	c.Assert(err, IsNil)
	err = m.EnsureDead()
	c.Assert(err, IsNil)
}

func (s *MachineSuite) TestMachineSetAgentAlive(c *C) {
	alive, err := s.machine.AgentAlive()
	c.Assert(err, IsNil)
	c.Assert(alive, Equals, false)

	pinger, err := s.machine.SetAgentAlive()
	c.Assert(err, IsNil)
	c.Assert(pinger, Not(IsNil))
	defer pinger.Stop()

	s.State.Sync()
	alive, err = s.machine.AgentAlive()
	c.Assert(err, IsNil)
	c.Assert(alive, Equals, true)
}

func (s *MachineSuite) TestEntityName(c *C) {
	c.Assert(s.machine.EntityName(), Equals, "machine-0")
}

func (s *MachineSuite) TestMachineEntityName(c *C) {
	c.Assert(state.MachineEntityName("10"), Equals, "machine-10")
}

func (s *MachineSuite) TestSetMongoPassword(c *C) {
	testSetMongoPassword(c, func(st *state.State) (entity, error) {
		return st.Machine(s.machine.Id())
	})
}

func (s *MachineSuite) TestMachineWaitAgentAlive(c *C) {
	timeout := 200 * time.Millisecond
	alive, err := s.machine.AgentAlive()
	c.Assert(err, IsNil)
	c.Assert(alive, Equals, false)

	s.State.StartSync()
	err = s.machine.WaitAgentAlive(timeout)
	c.Assert(err, ErrorMatches, `waiting for agent of machine 0: still not alive after timeout`)

	pinger, err := s.machine.SetAgentAlive()
	c.Assert(err, IsNil)

	s.State.StartSync()
	err = s.machine.WaitAgentAlive(timeout)
	c.Assert(err, IsNil)

	alive, err = s.machine.AgentAlive()
	c.Assert(err, IsNil)
	c.Assert(alive, Equals, true)

	err = pinger.Kill()
	c.Assert(err, IsNil)

	s.State.Sync()
	alive, err = s.machine.AgentAlive()
	c.Assert(err, IsNil)
	c.Assert(alive, Equals, false)
}

func (s *MachineSuite) TestMachineInstanceId(c *C) {
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = s.machines.Update(
		D{{"_id", machine.Id()}},
		D{{"$set", D{{"instanceid", "spaceship/0"}}}},
	)
	c.Assert(err, IsNil)

	err = machine.Refresh()
	c.Assert(err, IsNil)
	iid, _ := machine.InstanceId()
	c.Assert(iid, Equals, state.InstanceId("spaceship/0"))
}

func (s *MachineSuite) TestMachineInstanceIdCorrupt(c *C) {
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = s.machines.Update(
		D{{"_id", machine.Id()}},
		D{{"$set", D{{"instanceid", D{{"foo", "bar"}}}}}},
	)
	c.Assert(err, IsNil)

	err = machine.Refresh()
	c.Assert(err, IsNil)
	iid, err := machine.InstanceId()
	c.Assert(state.IsNotFound(err), Equals, true)
	c.Assert(iid, Equals, state.InstanceId(""))
}

func (s *MachineSuite) TestMachineInstanceIdMissing(c *C) {
	iid, err := s.machine.InstanceId()
	c.Assert(state.IsNotFound(err), Equals, true)
	c.Assert(err, ErrorMatches, "instance id for machine 0 not found")
	c.Assert(string(iid), Equals, "")
}

func (s *MachineSuite) TestMachineInstanceIdBlank(c *C) {
	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	err = s.machines.Update(
		D{{"_id", machine.Id()}},
		D{{"$set", D{{"instanceid", ""}}}},
	)
	c.Assert(err, IsNil)

	err = machine.Refresh()
	c.Assert(err, IsNil)
	iid, err := machine.InstanceId()
	c.Assert(state.IsNotFound(err), Equals, true)
	c.Assert(string(iid), Equals, "")
}

func (s *MachineSuite) TestMachineSetInstanceId(c *C) {
	err := s.machine.SetInstanceId("umbrella/0")
	c.Assert(err, IsNil)

	m, err := s.State.Machine(s.machine.Id())
	c.Assert(err, IsNil)
	id, err := m.InstanceId()
	c.Assert(err, IsNil)
	c.Assert(string(id), Equals, "umbrella/0")
}

func (s *MachineSuite) TestMachineRefresh(c *C) {
	m0, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	oldId, _ := m0.InstanceId()

	m1, err := s.State.Machine(m0.Id())
	c.Assert(err, IsNil)
	err = m0.SetInstanceId("umbrella/0")
	c.Assert(err, IsNil)
	newId, _ := m0.InstanceId()

	m1Id, _ := m1.InstanceId()
	c.Assert(m1Id, Equals, oldId)
	err = m1.Refresh()
	c.Assert(err, IsNil)
	m1Id, _ = m1.InstanceId()
	c.Assert(m1Id, Equals, newId)

	err = m0.EnsureDead()
	c.Assert(err, IsNil)
	err = s.State.RemoveMachine(m0.Id())
	c.Assert(err, IsNil)
	err = m0.Refresh()
	c.Assert(state.IsNotFound(err), Equals, true)
}

func (s *MachineSuite) TestRefreshWhenNotAlive(c *C) {
	// Refresh should work regardless of liveness status.
	m := s.machine
	err := m.SetInstanceId("foo")
	c.Assert(err, IsNil)

	testWhenDying(c, s.machine, noErr, noErr, func() error {
		return m.Refresh()
	})
}

func (s *MachineSuite) TestMachinePrincipalUnits(c *C) {
	// Check that Machine.Units works correctly.

	// Make three machines, three services and three units for each service;
	// variously assign units to machines and check that Machine.Units
	// tells us the right thing.

	m1 := s.machine
	m2, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)
	m3, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)

	dummy := s.AddTestingCharm(c, "dummy")
	logging := s.AddTestingCharm(c, "logging")
	s0, err := s.State.AddService("s0", dummy)
	c.Assert(err, IsNil)
	s1, err := s.State.AddService("s1", dummy)
	c.Assert(err, IsNil)
	s2, err := s.State.AddService("s2", dummy)
	c.Assert(err, IsNil)
	s3, err := s.State.AddService("s3", logging)
	c.Assert(err, IsNil)

	units := make([][]*state.Unit, 4)
	for i, svc := range []*state.Service{s0, s1, s2} {
		units[i] = make([]*state.Unit, 3)
		for j := range units[i] {
			units[i][j], err = svc.AddUnit()
			c.Assert(err, IsNil)
		}
	}
	// Add the logging units subordinate to the s2 units.
	units[3] = make([]*state.Unit, 3)
	for i := range units[3] {
		units[3][i], err = s3.AddUnitSubordinateTo(units[2][i])
	}

	assignments := []struct {
		machine      *state.Machine
		units        []*state.Unit
		subordinates []*state.Unit
	}{
		{m1, []*state.Unit{units[0][0]}, nil},
		{m2, []*state.Unit{units[0][1], units[1][0], units[1][1], units[2][0]}, []*state.Unit{units[3][0]}},
		{m3, []*state.Unit{units[2][2]}, []*state.Unit{units[3][2]}},
	}

	for _, a := range assignments {
		for _, u := range a.units {
			err := u.AssignToMachine(a.machine)
			c.Assert(err, IsNil)
		}
	}

	for i, a := range assignments {
		c.Logf("test %d", i)
		got, err := a.machine.Units()
		c.Assert(err, IsNil)
		expect := sortedUnitNames(append(a.units, a.subordinates...))
		c.Assert(sortedUnitNames(got), DeepEquals, expect)
	}
}

func sortedUnitNames(units []*state.Unit) []string {
	names := make([]string, len(units))
	for i, u := range units {
		names[i] = u.Name()
	}
	sort.Strings(names)
	return names
}

type machineInfo struct {
	tools      *state.Tools
	instanceId string
}

func tools(tools int, url string) *state.Tools {
	return &state.Tools{
		URL: url,
		Binary: version.Binary{
			Number: version.Number{
				Major: 0, Minor: 0, Patch: tools,
			},
			Series: "series",
			Arch:   "arch",
		},
	}
}

var watchMachineTests = []func(m *state.Machine) error{
	func(m *state.Machine) error {
		return nil
	},
	func(m *state.Machine) error {
		return m.SetInstanceId("m-foo")
	},
	func(m *state.Machine) error {
		return m.SetInstanceId("")
	},
	func(m *state.Machine) error {
		return m.SetAgentTools(tools(3, "baz"))
	},
}

func (s *MachineSuite) TestWatchMachine(c *C) {
	w := s.machine.Watch()
	defer func() {
		c.Assert(w.Stop(), IsNil)
	}()
	for i, test := range watchMachineTests {
		c.Logf("test %d", i)
		err := test(s.machine)
		c.Assert(err, IsNil)
		s.State.StartSync()
		select {
		case _, ok := <-w.Changes():
			c.Assert(ok, Equals, true)
		case <-time.After(5 * time.Second):
			c.Fatalf("did not get change")
		}
	}
	select {
	case got := <-w.Changes():
		c.Fatalf("got unexpected change: %#v", got)
	case <-time.After(50 * time.Millisecond):
	}
}

var machinePrincipalsWatchTests = []struct {
	test    func(*C, *MachineSuite, *state.Service)
	changed []string
}{
	{
		test:    func(_ *C, _ *MachineSuite, _ *state.Service) {},
		changed: []string{},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/0"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/1"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit2, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit2.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			unit3, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit3.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/2", "mysql/3"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit3, err := service.Unit("mysql/3")
			c.Assert(err, IsNil)
			err = unit3.EnsureDead()
			c.Assert(err, IsNil)
			err = service.RemoveUnit(unit3)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/3"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit0, err := service.Unit("mysql/0")
			c.Assert(err, IsNil)
			err = unit0.EnsureDead()
			c.Assert(err, IsNil)
			err = service.RemoveUnit(unit0)
			c.Assert(err, IsNil)
			unit2, err := service.Unit("mysql/2")
			c.Assert(err, IsNil)
			err = unit2.EnsureDead()
			c.Assert(err, IsNil)
			err = service.RemoveUnit(unit2)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/0", "mysql/2"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit4, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit4.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			unit1, err := service.Unit("mysql/1")
			c.Assert(err, IsNil)
			err = unit1.EnsureDead()
			c.Assert(err, IsNil)
			err = service.RemoveUnit(unit1)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/1", "mysql/4"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			units := [20]*state.Unit{}
			var err error
			for i := 0; i < len(units); i++ {
				units[i], err = service.AddUnit()
				c.Assert(err, IsNil)
				err = units[i].AssignToMachine(s.machine)
				c.Assert(err, IsNil)
			}
			for i := 10; i < len(units); i++ {
				err = units[i].EnsureDead()
				c.Assert(err, IsNil)
				err = service.RemoveUnit(units[i])
				c.Assert(err, IsNil)
			}
		},
		changed: []string{"mysql/10", "mysql/11", "mysql/12", "mysql/13", "mysql/14", "mysql/5", "mysql/6", "mysql/7", "mysql/8", "mysql/9"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit25, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit25.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			unit9, err := service.Unit("mysql/9")
			c.Assert(err, IsNil)
			err = unit9.EnsureDead()
			c.Assert(err, IsNil)
			err = service.RemoveUnit(unit9)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/9", "mysql/25"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit26, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit26.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			unit27, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit27.AssignToMachine(s.machine)
			c.Assert(err, IsNil)

			ch, _, err := service.Charm()
			c.Assert(err, IsNil)
			svc, err := s.State.AddService("bacon", ch)
			c.Assert(err, IsNil)
			bacon0, err := svc.AddUnit()
			c.Assert(err, IsNil)
			err = bacon0.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			bacon1, err := svc.AddUnit()
			c.Assert(err, IsNil)
			err = bacon1.AssignToMachine(s.machine)
			c.Assert(err, IsNil)

			spammachine, err := s.State.AddMachine(state.JobHostUnits)
			c.Assert(err, IsNil)
			svc, err = s.State.AddService("spam", ch)
			c.Assert(err, IsNil)
			spam0, err := svc.AddUnit()
			c.Assert(err, IsNil)
			err = spam0.AssignToMachine(spammachine)
			c.Assert(err, IsNil)
			spam1, err := svc.AddUnit()
			c.Assert(err, IsNil)
			err = spam1.AssignToMachine(spammachine)
			c.Assert(err, IsNil)

			unit14, err := service.Unit("mysql/14")
			c.Assert(err, IsNil)
			err = unit14.EnsureDead()
			c.Assert(err, IsNil)
			err = service.RemoveUnit(unit14)
			c.Assert(err, IsNil)
		},
		changed: []string{"bacon/0", "bacon/1", "mysql/14", "mysql/26", "mysql/27"},
	},
	{
		test: func(c *C, s *MachineSuite, service *state.Service) {
			unit28, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit28.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			unit29, err := service.AddUnit()
			c.Assert(err, IsNil)
			err = unit29.AssignToMachine(s.machine)
			c.Assert(err, IsNil)
			subCharm := s.AddTestingCharm(c, "logging")
			logService, err := s.State.AddService("logging", subCharm)
			c.Assert(err, IsNil)
			_, err = logService.AddUnitSubordinateTo(unit28)
			c.Assert(err, IsNil)
			_, err = logService.AddUnitSubordinateTo(unit28)
			c.Assert(err, IsNil)
			_, err = logService.AddUnitSubordinateTo(unit29)
			c.Assert(err, IsNil)
		},
		changed: []string{"mysql/28", "mysql/29"},
	},
}

func (s *MachineSuite) TestWatchPrincipalUnits(c *C) {
	charm := s.AddTestingCharm(c, "dummy")
	service, err := s.State.AddService("mysql", charm)
	c.Assert(err, IsNil)
	unitWatcher := s.machine.WatchPrincipalUnits()
	defer func() {
		c.Assert(unitWatcher.Stop(), IsNil)
	}()
	for i, test := range machinePrincipalsWatchTests {
		c.Logf("test %d", i)
		test.test(c, s, service)
		s.State.StartSync()
		got := []string{}
		for {
			select {
			case new, ok := <-unitWatcher.Changes():
				c.Assert(ok, Equals, true)
			checkDupes:
				for _, name := range new {
					for _, known := range got {
						if name == known {
							continue checkDupes
						}
					}
					got = append(got, name)
				}
				if len(got) < len(test.changed) {
					continue
				}
				sort.Strings(got)
				sort.Strings(test.changed)
				c.Assert(got, DeepEquals, test.changed)
			case <-time.After(500 * time.Millisecond):
				c.Fatalf("wanted %#v, got %#v", test.changed, got)
			}
			break
		}
	}
	select {
	case got := <-unitWatcher.Changes():
		c.Fatalf("got unexpected change: %#v", got)
	case <-time.After(100 * time.Millisecond):
	}
}

var machineUnitsWatchTests = []struct {
	summary string
	test    func(*C, *MachineSuite, *state.Unit, *state.Charm)
	changes []string
}{
	{
		summary: "Add a subordinate unit",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			log, err := s.State.AddService("log0", subCh)
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log0/0"},
	},
	{
		summary: "Ignore unrelated change",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log0")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log0/0")
			c.Assert(err, IsNil)
			err = unit.SetPublicAddress("what.ever")
			c.Assert(err, IsNil)
			log, err := s.State.AddService("log1", subCh)
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log1/0"},
	},
	{
		summary: "Add two units at once",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			log2, err := s.State.AddService("log2", subCh)
			c.Assert(err, IsNil)
			_, err = log2.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
			log3, err := s.State.AddService("log3", subCh)
			c.Assert(err, IsNil)
			_, err = log3.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log2/0", "log3/0"},
	},
	{
		summary: "Add three units at once",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			log4, err := s.State.AddService("log4", subCh)
			c.Assert(err, IsNil)
			_, err = log4.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
			_, err = log4.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
			_, err = log4.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log4/0", "log4/1", "log4/2"},
	},
	{
		summary: "Report dying unit",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log0")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log0/0")
			c.Assert(err, IsNil)
			err = unit.EnsureDying()
			c.Assert(err, IsNil)
		},
		changes: []string{"log0/0"},
	},
	{
		summary: "Report dead unit",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log1")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log1/0")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
		},
		changes: []string{"log1/0"},
	},
	{
		summary: "Report multiple dying or dead units",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc2, err := s.State.Service("log2")
			c.Assert(err, IsNil)
			unit2, err := svc2.Unit("log2/0")
			c.Assert(err, IsNil)
			err = unit2.EnsureDying()
			c.Assert(err, IsNil)
			svc3, err := s.State.Service("log3")
			c.Assert(err, IsNil)
			unit3, err := svc3.Unit("log3/0")
			c.Assert(err, IsNil)
			err = unit3.EnsureDead()
			c.Assert(err, IsNil)
		},
		changes: []string{"log2/0", "log3/0"},
	},
	{
		summary: "Report dead unit",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log4")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log4/0")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
		},
		changes: []string{"log4/0"},
	},
	{
		summary: "Report dead unit and a new, alive, unit",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log4")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log4/1")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
			log, err := s.State.AddService("log5", subCh)
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log5/0", "log4/1"},
	},
	{
		summary: "Report multiple dead units and multiple new, alive, units",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log4")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log4/2")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
			log, err := s.State.Service("log5")
			c.Assert(err, IsNil)
			unit, err = log.Unit("log5/0")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log5/1", "log5/2", "log4/2", "log5/0"},
	},
	{
		summary: "Add many, change many, and remove many at once",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			log10, err := s.State.AddService("log10", subCh)
			c.Assert(err, IsNil)
			log20, err := s.State.AddService("log20", subCh)
			c.Assert(err, IsNil)
			units10 := [10]*state.Unit{}
			units20 := [20]*state.Unit{}
			for i := 0; i < len(units20); i++ {
				units20[i], err = log20.AddUnitSubordinateTo(principal)
				c.Assert(err, IsNil)
			}
			for i := 0; i < len(units10); i++ {
				units10[i], err = log10.AddUnitSubordinateTo(principal)
				c.Assert(err, IsNil)
			}
			for i := 10; i < len(units20); i++ {
				err = units20[i].EnsureDead()
				c.Assert(err, IsNil)
				err = log20.RemoveUnit(units20[i])
				c.Assert(err, IsNil)
			}
			for i := 5; i < len(units10); i++ {
				err = units10[i].EnsureDead()
				c.Assert(err, IsNil)
				err = log10.RemoveUnit(units10[i])
				c.Assert(err, IsNil)
			}
		},
		changes: []string{"log10/0", "log10/1", "log10/2", "log10/3", "log10/4", "log20/0", "log20/1", "log20/2", "log20/3", "log20/4", "log20/5", "log20/6", "log20/7", "log20/8", "log20/9"},
	},
	{
		summary: "report dead when first seen and also add a new unit",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log20")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log20/9")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
			log, err := s.State.AddService("log30", subCh)
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
		},
		changes: []string{"log30/0", "log20/9"},
	},
	{
		summary: "report only units assigned to this machine",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc20, err := s.State.Service("log20")
			c.Assert(err, IsNil)
			unit208, err := svc20.Unit("log20/8")
			c.Assert(err, IsNil)
			err = unit208.EnsureDead()
			c.Assert(err, IsNil)
			log35, err := s.State.AddService("log35", subCh)
			c.Assert(err, IsNil)
			_, err = log35.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)

			err = principal.Refresh()
			c.Assert(err, IsNil)
			svc, err := s.State.Service(principal.ServiceName())
			c.Assert(err, IsNil)
			ch, _, err := svc.Charm()
			c.Assert(err, IsNil)
			log98, err := s.State.AddService("log98", ch)
			c.Assert(err, IsNil)
			log99, err := s.State.AddService("log99", subCh)
			c.Assert(err, IsNil)
			unit980, err := log98.AddUnit()
			c.Assert(err, IsNil)
			_, err = log99.AddUnitSubordinateTo(unit980)
			c.Assert(err, IsNil)
			_, err = log99.AddUnitSubordinateTo(unit980)
			c.Assert(err, IsNil)
			m, err := s.State.AddMachine(state.JobHostUnits)
			c.Assert(err, IsNil)
			err = unit980.AssignToMachine(m)
		},
		changes: []string{"log35/0", "log20/8"},
	},
	{
		summary: "Report previously known machines that are removed",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			svc, err := s.State.Service("log35")
			c.Assert(err, IsNil)
			unit, err := svc.Unit("log35/0")
			c.Assert(err, IsNil)
			err = unit.EnsureDead()
			c.Assert(err, IsNil)
			err = svc.RemoveUnit(unit)
			c.Assert(err, IsNil)
		},
		changes: []string{"log35/0"},
	},
	{
		summary: "Add a subordinate and change life at the same time",
		test: func(c *C, s *MachineSuite, principal *state.Unit, subCh *state.Charm) {
			log, err := s.State.AddService("log40", subCh)
			c.Assert(err, IsNil)
			_, err = log.AddUnitSubordinateTo(principal)
			c.Assert(err, IsNil)
			err = principal.EnsureDying()
			c.Assert(err, IsNil)
		},
		changes: []string{"log40/0", "mysql/0"},
	},
	// TODO(niemeyer): This is totally unmaintainable. Do not add more
	// tests here. Create independent tests that explore specific
	// functionality instead (see TestWatchUnitsUnassignFromMachine).
}

func (s *MachineSuite) TestWatchUnits(c *C) {
	unitWatcher := s.machine.WatchUnits()
	defer func() {
		c.Assert(unitWatcher.Stop(), IsNil)
	}()
	// Check initial event.
	s.State.StartSync()
	select {
	case got := <-unitWatcher.Changes():
		c.Assert(got, DeepEquals, []string(nil))
		// TODO(niemeyer): Where's the test checking that this
		// returns anything other than nil?
	case <-time.After(500 * time.Millisecond):
		c.Fatalf("did not get change: %#v", []string(nil))
	}

	// Check principal unit assignment.
	charm := s.AddTestingCharm(c, "dummy")
	service, err := s.State.AddService("mysql", charm)
	c.Assert(err, IsNil)
	principal, err := service.AddUnit()
	c.Assert(err, IsNil)
	err = principal.AssignToMachine(s.machine)
	c.Assert(err, IsNil)
	s.State.StartSync()
	select {
	case got := <-unitWatcher.Changes():
		c.Assert(got, DeepEquals, []string{"mysql/0"})
	case <-time.After(500 * time.Millisecond):
		c.Fatalf("did not get change: %#v", []string{"mysql/0"})
	}

	all := []string{}
	subCh := s.AddTestingCharm(c, "logging")
	for i, test := range machineUnitsWatchTests {
		c.Logf("test %d: %s", i, test.summary)
		test.test(c, s, principal, subCh)
		s.State.StartSync()
		all = append(all, test.changes...)
		got := []string{}
		want := append([]string(nil), test.changes...)
		sort.Strings(want)
		for {
			select {
			case new, ok := <-unitWatcher.Changes():
				c.Assert(ok, Equals, true)
				got = append(got, new...)
				if len(got) < len(want) {
					continue
				}
				sort.Strings(got)
				c.Assert(got, DeepEquals, want)
			case <-time.After(500 * time.Millisecond):
				c.Fatalf("did not get change, want: %#v", want)
			}
			break
		}
	}

	// Check that removing units for which we already got a Dead event
	// does not yield any more events.
	for _, uname := range all {
		unit, err := s.State.Unit(uname)
		if state.IsNotFound(err) || unit.Life() != state.Dead {
			continue
		}
		c.Assert(err, IsNil)
		svc, err := s.State.Service(unit.ServiceName())
		c.Assert(err, IsNil)
		err = svc.RemoveUnit(unit)
		c.Assert(err, IsNil)
	}
	s.State.StartSync()
	select {
	case got := <-unitWatcher.Changes():
		c.Fatalf("got unexpected change: %#v", got)
	case <-time.After(100 * time.Millisecond):
	}

	// Check assignment of additional principal units.
	service, err = s.State.AddService("wordpress", charm)
	c.Assert(err, IsNil)
	unit0, err := service.AddUnit()
	c.Assert(err, IsNil)
	err = unit0.AssignToMachine(s.machine)
	c.Assert(err, IsNil)
	unit1, err := service.AddUnit()
	c.Assert(err, IsNil)
	err = unit1.AssignToMachine(s.machine)
	c.Assert(err, IsNil)
	s.State.StartSync()
	select {
	case got := <-unitWatcher.Changes():
		c.Assert(got, DeepEquals, []string{"wordpress/0", "wordpress/1"})
	case <-time.After(500 * time.Millisecond):
		c.Fatalf("did not get change: %#v", []string{"wordpress/0", "wordpress/1"})
	}

	// Check the lack of spurious events.
	select {
	case got := <-unitWatcher.Changes():
		c.Fatalf("got unexpected change: %#v", got)
	case <-time.After(100 * time.Millisecond):
	}
}

func (s *MachineSuite) TestWatchUnitsUnassignFromMachine(c *C) {
	s.testWatchUnitsUnassign(c, false)
}

func (s *MachineSuite) TestWatchUnitsReassignToMachine(c *C) {
	s.testWatchUnitsUnassign(c, true)
}

func (s *MachineSuite) testWatchUnitsUnassign(c *C, reassign bool) {
	unitWatcher := s.machine.WatchUnits()
	defer unitWatcher.Stop()

	s.State.Sync()

	select {
	case <-unitWatcher.Changes():
	case <-time.After(5 * time.Second):
		c.Fatalf("wacther didn't send initial event")
	}

	charm := s.AddTestingCharm(c, "wordpress")
	subcharm := s.AddTestingCharm(c, "logging")
	service, err := s.State.AddService("wordpress", charm)
	c.Assert(err, IsNil)
	subservice, err := s.State.AddService("logging", subcharm)
	c.Assert(err, IsNil)
	principal, err := service.AddUnit()
	c.Assert(err, IsNil)
	_, err = subservice.AddUnitSubordinateTo(principal)
	c.Assert(err, IsNil)
	subordinate2, err := subservice.AddUnitSubordinateTo(principal)
	c.Assert(err, IsNil)
	err = principal.AssignToMachine(s.machine)
	c.Assert(err, IsNil)

	s.State.Sync()

	select {
	case change := <-unitWatcher.Changes():
		sort.Strings(change)
		c.Assert(change, DeepEquals, []string{"logging/0", "logging/1", "wordpress/0"})
	case <-time.After(5 * time.Second):
		c.Fatalf("watcher didn't send expected event")
	}

	// Remove one of the subordinate to make matters more interesting.
	subordinate2.EnsureDead()
	err = subservice.RemoveUnit(subordinate2)
	c.Assert(err, IsNil)

	machine, err := s.State.AddMachine(state.JobHostUnits)
	c.Assert(err, IsNil)

	err = principal.UnassignFromMachine()
	c.Assert(err, IsNil)

	if reassign {
		err = principal.AssignToMachine(machine)
		c.Assert(err, IsNil)
	}

	s.State.Sync()

	select {
	case change := <-unitWatcher.Changes():
		sort.Strings(change)
		c.Assert(change, DeepEquals, []string{"logging/0", "logging/1", "wordpress/0"})
	case <-time.After(5 * time.Second):
		c.Fatalf("watcher didn't send expected event")
	}
}
