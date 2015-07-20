// Copyright 2015 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package server

import (
	jc "github.com/juju/testing/checkers"
	gc "gopkg.in/check.v1"
	"gopkg.in/juju/charm.v5"

	"github.com/juju/juju/process"
	"github.com/juju/juju/process/api"
)

type suite struct{}

var _ = gc.Suite(&suite{})

func (suite) TestRegisterProcess(c *gc.C) {
	st := &FakeState{}
	a := HookContextAPI{st}

	args := api.RegisterProcessesArgs{
		Processes: []api.Process{{
			Definition: api.ProcessDefinition{
				Name:        "foobar",
				Description: "desc",
				Type:        "type",
				TypeOptions: map[string]string{"foo": "bar"},
				Command:     "cmd",
				Image:       "img",
				Ports: []api.ProcessPort{{
					External: 8080,
					Internal: 80,
					Endpoint: "endpoint",
				}},
				Volumes: []api.ProcessVolume{{
					ExternalMount: "/foo/bar",
					InternalMount: "/baz/bat",
					Mode:          "ro",
					Name:          "volname",
				}},
				EnvVars: map[string]string{"envfoo": "bar"},
			},
			Details: api.ProcessDetails{
				ID: "idfoo",
				Status: api.ProcessStatus{
					Label: "running",
				},
			},
		}},
	}

	res, err := a.RegisterProcesses(args)
	c.Assert(err, jc.ErrorIsNil)

	expectedResults := api.ProcessResults{
		Results: []api.ProcessResult{{
			ID:    "foobar/idfoo",
			Error: nil,
		}},
	}

	c.Assert(res, gc.DeepEquals, expectedResults)

	expected := process.Info{
		Process: charm.Process{
			Name:        "foobar",
			Description: "desc",
			Type:        "type",
			TypeOptions: map[string]string{"foo": "bar"},
			Command:     "cmd",
			Image:       "img",
			Ports: []charm.ProcessPort{
				{
					External: 8080,
					Internal: 80,
					Endpoint: "endpoint",
				},
			},
			Volumes: []charm.ProcessVolume{
				{
					ExternalMount: "/foo/bar",
					InternalMount: "/baz/bat",
					Mode:          "ro",
					Name:          "volname",
				},
			},
			EnvVars: map[string]string{"envfoo": "bar"},
		},
		Details: process.Details{
			ID: "idfoo",
			Status: process.PluginStatus{
				Label: "running",
			},
		},
	}

	c.Assert(st.info, gc.DeepEquals, expected)
}

func (suite) TestListProcessesOne(c *gc.C) {
	proc := process.Info{
		Process: charm.Process{
			Name:        "foobar",
			Description: "desc",
			Type:        "type",
			TypeOptions: map[string]string{"foo": "bar"},
			Command:     "cmd",
			Image:       "img",
			Ports: []charm.ProcessPort{
				{
					External: 8080,
					Internal: 80,
					Endpoint: "endpoint",
				},
			},
			Volumes: []charm.ProcessVolume{
				{
					ExternalMount: "/foo/bar",
					InternalMount: "/baz/bat",
					Mode:          "ro",
					Name:          "volname",
				},
			},
			EnvVars: map[string]string{"envfoo": "bar"},
		},
		Details: process.Details{
			ID: "idfoo",
			Status: process.PluginStatus{
				Label: "running",
			},
		},
	}
	st := &FakeState{procs: []process.Info{proc}}
	a := HookContextAPI{st}
	args := api.ListProcessesArgs{
		IDs: []string{"foobar/idfoo"},
	}
	results, err := a.ListProcesses(args)
	c.Assert(err, jc.ErrorIsNil)

	expected := api.Process{
		Definition: api.ProcessDefinition{
			Name:        "foobar",
			Description: "desc",
			Type:        "type",
			TypeOptions: map[string]string{"foo": "bar"},
			Command:     "cmd",
			Image:       "img",
			Ports: []api.ProcessPort{
				{
					External: 8080,
					Internal: 80,
					Endpoint: "endpoint",
				},
			},
			Volumes: []api.ProcessVolume{
				{
					ExternalMount: "/foo/bar",
					InternalMount: "/baz/bat",
					Mode:          "ro",
					Name:          "volname",
				},
			},
			EnvVars: map[string]string{"envfoo": "bar"},
		},
		Details: api.ProcessDetails{
			ID: "idfoo",
			Status: api.ProcessStatus{
				Label: "running",
			},
		},
	}

	expectedResults := api.ListProcessesResults{
		Results: []api.ListProcessResult{{
			ID:    "foobar/idfoo",
			Info:  expected,
			Error: nil,
		}},
	}

	c.Assert(results, gc.DeepEquals, expectedResults)
}

func (suite) TestListProcessesAll(c *gc.C) {
	proc := process.Info{
		Process: charm.Process{
			Name:        "foobar",
			Description: "desc",
			Type:        "type",
			TypeOptions: map[string]string{"foo": "bar"},
			Command:     "cmd",
			Image:       "img",
			Ports: []charm.ProcessPort{
				{
					External: 8080,
					Internal: 80,
					Endpoint: "endpoint",
				},
			},
			Volumes: []charm.ProcessVolume{
				{
					ExternalMount: "/foo/bar",
					InternalMount: "/baz/bat",
					Mode:          "ro",
					Name:          "volname",
				},
			},
			EnvVars: map[string]string{"envfoo": "bar"},
		},
		Details: process.Details{
			ID: "idfoo",
			Status: process.PluginStatus{
				Label: "running",
			},
		},
	}
	st := &FakeState{procs: []process.Info{proc}}
	a := HookContextAPI{st}
	args := api.ListProcessesArgs{}
	results, err := a.ListProcesses(args)
	c.Assert(err, jc.ErrorIsNil)

	expected := api.Process{
		Definition: api.ProcessDefinition{
			Name:        "foobar",
			Description: "desc",
			Type:        "type",
			TypeOptions: map[string]string{"foo": "bar"},
			Command:     "cmd",
			Image:       "img",
			Ports: []api.ProcessPort{
				{
					External: 8080,
					Internal: 80,
					Endpoint: "endpoint",
				},
			},
			Volumes: []api.ProcessVolume{
				{
					ExternalMount: "/foo/bar",
					InternalMount: "/baz/bat",
					Mode:          "ro",
					Name:          "volname",
				},
			},
			EnvVars: map[string]string{"envfoo": "bar"},
		},
		Details: api.ProcessDetails{
			ID: "idfoo",
			Status: api.ProcessStatus{
				Label: "running",
			},
		},
	}

	expectedResults := api.ListProcessesResults{
		Results: []api.ListProcessResult{{
			ID:    "foobar/idfoo",
			Info:  expected,
			Error: nil,
		}},
	}

	c.Assert(results, gc.DeepEquals, expectedResults)
}

func (suite) TestSetProcessStatus(c *gc.C) {
	st := &FakeState{}
	a := HookContextAPI{st}
	args := api.SetProcessesStatusArgs{
		Args: []api.SetProcessStatusArg{{
			ID: "fooID",
			Status: api.ProcessStatus{
				Label: "statusfoo",
			},
		}},
	}
	res, err := a.SetProcessesStatus(args)
	c.Assert(err, jc.ErrorIsNil)

	c.Assert(st.id, gc.Equals, "fooID")
	c.Assert(st.status, jc.DeepEquals, &process.PluginStatus{
		Label: "statusfoo",
	})

	expected := api.ProcessResults{
		Results: []api.ProcessResult{{
			ID:    "fooID",
			Error: nil,
		}},
	}
	c.Assert(res, gc.DeepEquals, expected)
}

func (suite) TestUnregisterProcesses(c *gc.C) {
	st := &FakeState{}
	a := HookContextAPI{st}
	args := api.UnregisterProcessesArgs{
		IDs: []string{"fooID"},
	}
	res, err := a.UnregisterProcesses(args)
	c.Assert(err, jc.ErrorIsNil)

	c.Assert(st.id, gc.Equals, "fooID")

	expected := api.ProcessResults{
		Results: []api.ProcessResult{{
			ID:    "fooID",
			Error: nil,
		}},
	}
	c.Assert(res, gc.DeepEquals, expected)
}

type FakeState struct {
	// inputs
	id     string
	ids    []string
	status *process.PluginStatus

	// info is used as input and output
	info process.Info

	//outputs
	procs []process.Info
	err   error
}

func (f *FakeState) Add(info process.Info) error {
	f.info = info
	return f.err
}
func (f *FakeState) List(ids ...string) ([]process.Info, error) {
	f.ids = ids
	return f.procs, f.err
}

func (f *FakeState) SetStatus(id string, status process.PluginStatus) error {
	f.id = id
	f.status = &status
	return f.err
}

func (f *FakeState) Remove(id string) error {
	f.id = id
	return f.err
}
