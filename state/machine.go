package state

import (
	"fmt"
	"labix.org/v2/mgo"
	"labix.org/v2/mgo/txn"
	"launchpad.net/juju-core/state/presence"
	"launchpad.net/juju-core/trivial"
	"time"
)

// An InstanceId is a provider-specific identifier associated with an
// instance (physical or virtual machine allocated in the provider).
type InstanceId string

// Machine represents the state of a machine.
type Machine struct {
	st  *State
	doc machineDoc
}

// MachineAgentJob values define responsibilities that machines may be
// expected to fulfil.
type MachineAgentJob int

const (
	_ MachineAgentJob = iota
	HostPrincipalUnits
	HostEnvironController
)

// machineDoc represents the internal state of a machine in MongoDB.
type machineDoc struct {
	Id         string `bson:"_id"`
	InstanceId InstanceId
	Principals []string
	Life       Life
	Tools      *Tools `bson:",omitempty"`
	TxnRevno   int64  `bson:"txn-revno"`
	Jobs       []MachineAgentJob
}

func newMachine(st *State, doc *machineDoc) *Machine {
	return &Machine{st: st, doc: *doc}
}

// Id returns the machine id.
func (m *Machine) Id() string {
	return m.doc.Id
}

// globalKey returns the global database key for the machine.
func (m *Machine) globalKey() string {
	return "m#" + m.String()
}

// MachineEntityName returns the entity name for the
// machine with the given id.
func MachineEntityName(id string) string {
	return fmt.Sprintf("machine-%s", id)
}

// EntityName returns a name identifying the machine that is safe to use
// as a file name.  The returned name will be different from other
// EntityName values returned by any other entities from the same state.
func (m *Machine) EntityName() string {
	return MachineEntityName(m.Id())
}

// Life returns whether the machine is Alive, Dying or Dead.
func (m *Machine) Life() Life {
	return m.doc.Life
}

// AgentJobs returns the responsibilities that must be fulfilled by m's agent.
func (m *Machine) AgentJobs() []MachineAgentJob {
	return m.doc.Jobs
}

// AgentTools returns the tools that the agent is currently running.
// It returns a *NotFoundError if the tools have not yet been set.
func (m *Machine) AgentTools() (*Tools, error) {
	if m.doc.Tools == nil {
		return nil, notFound("agent tools for machine %v", m)
	}
	tools := *m.doc.Tools
	return &tools, nil
}

// SetAgentTools sets the tools that the agent is currently running.
func (m *Machine) SetAgentTools(t *Tools) (err error) {
	defer trivial.ErrorContextf(&err, "cannot set agent tools for machine %v", m)
	if t.Series == "" || t.Arch == "" {
		return fmt.Errorf("empty series or arch")
	}
	ops := []txn.Op{{
		C:      m.st.machines.Name,
		Id:     m.doc.Id,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"tools", t}}}},
	}}
	if err := m.st.runner.Run(ops, "", nil); err != nil {
		return onAbort(err, errNotAlive)
	}
	tools := *t
	m.doc.Tools = &tools
	return nil
}

// SetPassword sets the password the agent responsible for the machine
// should use to communicate with the state servers.  Previous passwords
// are invalidated.
func (m *Machine) SetPassword(password string) error {
	return m.st.setPassword(m.EntityName(), password)
}

// EnsureDying sets the machine lifecycle to Dying if it is Alive.
// It does nothing otherwise.
func (m *Machine) EnsureDying() error {
	err := ensureDying(m.st, m.st.machines, m.doc.Id, "machine")
	if err != nil {
		return err
	}
	m.doc.Life = Dying
	return nil
}

// EnsureDead sets the machine lifecycle to Dead if it is Alive or Dying.
// It does nothing otherwise.
func (m *Machine) EnsureDead() error {
	err := ensureDead(m.st, m.st.machines, m.doc.Id, "machine", nil, "")
	if err != nil {
		return err
	}
	m.doc.Life = Dead
	return nil
}

// Refresh refreshes the contents of the machine from the underlying
// state. It returns a NotFoundError if the machine has been removed.
func (m *Machine) Refresh() error {
	doc := machineDoc{}
	err := m.st.machines.FindId(m.doc.Id).One(&doc)
	if err == mgo.ErrNotFound {
		return notFound("machine %v", m)
	}
	if err != nil {
		return fmt.Errorf("cannot refresh machine %v: %v", m, err)
	}
	m.doc = doc
	return nil
}

// AgentAlive returns whether the respective remote agent is alive.
func (m *Machine) AgentAlive() (bool, error) {
	return m.st.pwatcher.Alive(m.globalKey())
}

// WaitAgentAlive blocks until the respective agent is alive.
func (m *Machine) WaitAgentAlive(timeout time.Duration) (err error) {
	defer trivial.ErrorContextf(&err, "waiting for agent of machine %v", m)
	ch := make(chan presence.Change)
	m.st.pwatcher.Watch(m.globalKey(), ch)
	defer m.st.pwatcher.Unwatch(m.globalKey(), ch)
	for i := 0; i < 2; i++ {
		select {
		case change := <-ch:
			if change.Alive {
				return nil
			}
		case <-time.After(timeout):
			return fmt.Errorf("still not alive after timeout")
		case <-m.st.pwatcher.Dead():
			return m.st.pwatcher.Err()
		}
	}
	panic(fmt.Sprintf("presence reported dead status twice in a row for machine %v", m))
}

// SetAgentAlive signals that the agent for machine m is alive.
// It returns the started pinger.
func (m *Machine) SetAgentAlive() (*presence.Pinger, error) {
	p := presence.NewPinger(m.st.presence, m.globalKey())
	err := p.Start()
	if err != nil {
		return nil, err
	}
	return p, nil
}

// InstanceId returns the provider specific instance id for this machine.
func (m *Machine) InstanceId() (InstanceId, error) {
	if m.doc.InstanceId == "" {
		return "", notFound("instance id for machine %v", m)
	}
	return m.doc.InstanceId, nil
}

// Units returns all the units that have been assigned to the machine.
func (m *Machine) Units() (units []*Unit, err error) {
	defer trivial.ErrorContextf(&err, "cannot get units assigned to machine %v", m)
	pudocs := []unitDoc{}
	err = m.st.units.Find(D{{"machineid", m.doc.Id}}).All(&pudocs)
	if err != nil {
		return nil, err
	}
	for _, pudoc := range pudocs {
		units = append(units, newUnit(m.st, &pudoc))
		docs := []unitDoc{}
		err = m.st.units.Find(D{{"principal", pudoc.Name}}).All(&docs)
		if err != nil {
			return nil, err
		}
		for _, doc := range docs {
			units = append(units, newUnit(m.st, &doc))
		}
	}
	return units, nil
}

// SetInstanceId sets the provider specific machine id for this machine.
func (m *Machine) SetInstanceId(id InstanceId) (err error) {
	ops := []txn.Op{{
		C:      m.st.machines.Name,
		Id:     m.doc.Id,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"instanceid", id}}}},
	}}
	if err := m.st.runner.Run(ops, "", nil); err != nil {
		return fmt.Errorf("cannot set instance id of machine %v: %v", m, onAbort(err, errNotAlive))
	}
	m.doc.InstanceId = id
	return nil
}

// String returns a unique description of this machine.
func (m *Machine) String() string {
	return m.doc.Id
}
