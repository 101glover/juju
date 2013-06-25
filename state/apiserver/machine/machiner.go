// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package machine

import (
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/api/params"
	"launchpad.net/juju-core/state/apiserver/common"
)

// Machiner implements the API used by the machiner worker.
type MachinerAPI struct {
	st   *state.State
	auth common.Authorizer
}

// NewMachinerAPI creates a new instance of the Machiner API.
func NewMachinerAPI(st *state.State, authorizer common.Authorizer) (*MachinerAPI, error) {
	if !authorizer.AuthMachineAgent() {
		return nil, common.ErrPerm
	}
	return &MachinerAPI{st, authorizer}, nil
}

// SetStatus sets the status of each given machine.
func (m *MachinerAPI) SetStatus(args params.MachinesSetStatus) (params.ErrorResults, error) {
	result := params.ErrorResults{
		Errors: make([]*params.Error, len(args.Machines)),
	}
	if len(args.Machines) == 0 {
		return result, nil
	}
	for i, arg := range args.Machines {
		machine, err := m.st.Machine(arg.Id)
		if err == nil {
			// Allow only for the owner agent.
			if !m.auth.AuthOwner(machine.Tag()) {
				err = common.ErrPerm
			} else {
				err = machine.SetStatus(arg.Status, arg.Info)
			}
		}
		result.Errors[i] = common.ServerError(err)
	}
	return result, nil
}

// Watch starts an EntityWatcher for each given machine.
//func (m *MachinerAPI) Watch(args params.Machines) (params.MachinerWatchResults, error) {
// TODO (dimitern) implement this once the watchers can handle bulk ops
//}

// Life returns the lifecycle state of each given machine.
func (m *MachinerAPI) Life(args params.Machines) (params.MachinesLifeResults, error) {
	result := params.MachinesLifeResults{
		Machines: make([]params.MachineLifeResult, len(args.Ids)),
	}
	if len(args.Ids) == 0 {
		return result, nil
	}
	for i, id := range args.Ids {
		machine, err := m.st.Machine(id)
		if err == nil {
			// Allow only for the owner agent.
			if !m.auth.AuthOwner(machine.Tag()) {
				err = common.ErrPerm
			} else {
				result.Machines[i].Life = params.Life(machine.Life().String())
			}
		}
		result.Machines[i].Error = common.ServerError(err)
	}
	return result, nil
}

// EnsureDead changes the lifecycle of each given machine to Dead if
// it's Alive or Dying. It does nothing otherwise.
func (m *MachinerAPI) EnsureDead(args params.Machines) (params.ErrorResults, error) {
	result := params.ErrorResults{
		Errors: make([]*params.Error, len(args.Ids)),
	}
	if len(args.Ids) == 0 {
		return result, nil
	}
	for i, id := range args.Ids {
		machine, err := m.st.Machine(id)
		if err == nil {
			// Allow only for the owner agent.
			if !m.auth.AuthOwner(machine.Tag()) {
				err = common.ErrPerm
			} else {
				err = machine.EnsureDead()
			}
		}
		result.Errors[i] = common.ServerError(err)
	}
	return result, nil
}
