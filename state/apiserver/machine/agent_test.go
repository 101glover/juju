package machine_test

import (
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/state/api"
	"launchpad.net/juju-core/state/api/params"
	"launchpad.net/juju-core/state/apiserver/machine"
)

type agentSuite struct {
	commonSuite
	agent *machine.AgentAPI
}

var _ = Suite(&agentSuite{})

func (s *agentSuite) SetUpTest(c *C) {
	s.commonSuite.SetUpTest(c)

	// Create a machiner API for machine 1.
	api, err := machine.NewAgentAPI(
		s.State,
		s.authorizer,
	)
	c.Assert(err, IsNil)
	s.agent = api
}

func (s *agentSuite) TestAgentFailsWithNonMachineAgentUser(c *C) {
	auth := s.authorizer
	auth.machineAgent = false
	api, err := machine.NewAgentAPI(s.State, auth)
	c.Assert(err, NotNil)
	c.Assert(api, IsNil)
	c.Assert(err, ErrorMatches, "permission denied")
}

func (s *agentSuite) TestAgentFailsWhenNotLoggedIn(c *C) {
	auth := s.authorizer
	auth.loggedIn = false
	api, err := machine.NewAgentAPI(s.State, auth)
	c.Assert(err, NotNil)
	c.Assert(api, IsNil)
	c.Assert(err, ErrorMatches, "not logged in")
}

func (s *agentSuite) TestGetMachines(c *C) {
	err := s.machine1.Destroy()
	c.Assert(err, IsNil)
	results := s.agent.GetMachines(params.Machines{
		Ids: []string{"1", "0", "42"},
	})
	c.Assert(results, DeepEquals, params.MachineAgentGetMachinesResults{
		Machines: []params.MachineAgentGetMachinesResult{{
			Life: "dying",
			Jobs: []params.MachineJob{params.JobHostUnits},
		}, {
			Error: &params.Error{
				Code:    api.CodeUnauthorized,
				Message: "permission denied",
			},
		}, {
			Error: &params.Error{
				Code:    api.CodeUnauthorized,
				Message: "permission denied",
			},
		}},
	})
}

func (s *agentSuite) TestGetNotFoundMachine(c *C) {
	err := s.machine1.Destroy()
	c.Assert(err, IsNil)
	err = s.machine1.EnsureDead()
	c.Assert(err, IsNil)
	err = s.machine1.Remove()
	c.Assert(err, IsNil)
	results, err := s.agent.GetMachines(params.Machines{
		Ids: []string{"1"},
	})
	c.Assert(err, IsNil)
	c.Assert(results, DeepEquals, params.MachineAgentGetMachinesResults{
		Machines: []params.MachineAgentGetMachinesResult{{
			Error: &params.Error{
				Code:    api.CodeNotFound,
				Message: "machine 1 not found",
			},
		}},
	})
}
