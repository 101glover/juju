package state

import (
	"errors"
	"fmt"
	"labix.org/v2/mgo"
	"labix.org/v2/mgo/bson"
	"labix.org/v2/mgo/txn"
	"launchpad.net/juju-core/charm"
	"launchpad.net/juju-core/state/presence"
	"launchpad.net/juju-core/trivial"
	"sort"
	"strings"
	"time"
)

// ResolvedMode describes the way state transition errors
// are resolved.
type ResolvedMode string

const (
	ResolvedNone       ResolvedMode = ""
	ResolvedRetryHooks ResolvedMode = "retry-hooks"
	ResolvedNoHooks    ResolvedMode = "no-hooks"
)

// AssignmentPolicy controls what machine a unit will be assigned to.
type AssignmentPolicy string

const (
	// AssignLocal indicates that all service units should be assigned
	// to machine 0.
	AssignLocal AssignmentPolicy = "local"
	// AssignUnused indicates that every service unit should be assigned
	// to a dedicated machine, and that new machines should be launched
	// if required.
	AssignUnused AssignmentPolicy = "unused"
)

// UnitStatus represents the status of the unit agent.
type UnitStatus string

const (
	UnitPending   UnitStatus = "pending"   // Agent hasn't started
	UnitInstalled UnitStatus = "installed" // Agent has run the installed hook
	UnitStarted   UnitStatus = "started"   // Agent is running properly
	UnitStopped   UnitStatus = "stopped"   // Agent has stopped running on request
	UnitError     UnitStatus = "error"     // Agent is waiting in an error state
	UnitDown      UnitStatus = "down"      // Agent is down or not communicating
)

// Port identifies a network port number for a particular protocol.
type Port struct {
	Protocol string
	Number   int
}

func (p Port) String() string {
	return fmt.Sprintf("%s:%d", p.Protocol, p.Number)
}

// UnitSettings holds information about a service unit's settings within a
// relation.
type UnitSettings struct {
	Version  int64
	Settings map[string]interface{}
}

// unitDoc represents the internal state of a unit in MongoDB.
type unitDoc struct {
	Name           string `bson:"_id"`
	Service        string
	CharmURL       *charm.URL
	Principal      string
	Subordinates   []string
	PublicAddress  string
	PrivateAddress string
	MachineId      string
	Resolved       ResolvedMode
	Tools          *Tools `bson:",omitempty"`
	Ports          []Port
	Life           Life
	Status         UnitStatus
	StatusInfo     string
	TxnRevno       int64 `bson:"txn-revno"`
	PasswordHash   string
}

// Unit represents the state of a service unit.
type Unit struct {
	st  *State
	doc unitDoc
	annotator
}

func newUnit(st *State, udoc *unitDoc) *Unit {
	unit := &Unit{
		st:        st,
		doc:       *udoc,
		annotator: annotator{st: st},
	}
	unit.annotator.entityName = unit.EntityName()
	return unit
}

// Service returns the service.
func (u *Unit) Service() (*Service, error) {
	return u.st.Service(u.doc.Service)
}

// ServiceName returns the service name.
func (u *Unit) ServiceName() string {
	return u.doc.Service
}

// String returns the unit as string.
func (u *Unit) String() string {
	return u.doc.Name
}

// Name returns the unit name.
func (u *Unit) Name() string {
	return u.doc.Name
}

// globalKey returns the global database key for the unit.
func (u *Unit) globalKey() string {
	return "u#" + u.doc.Name
}

// Life returns whether the unit is Alive, Dying or Dead.
func (u *Unit) Life() Life {
	return u.doc.Life
}

// AgentTools returns the tools that the agent is currently running.
// It an error that satisfies IsNotFound if the tools have not yet been set.
func (u *Unit) AgentTools() (*Tools, error) {
	if u.doc.Tools == nil {
		return nil, NotFoundf("agent tools for unit %q", u)
	}
	tools := *u.doc.Tools
	return &tools, nil
}

// SetAgentTools sets the tools that the agent is currently running.
func (u *Unit) SetAgentTools(t *Tools) (err error) {
	defer trivial.ErrorContextf(&err, "cannot set agent tools for unit %q", u)
	if t.Series == "" || t.Arch == "" {
		return fmt.Errorf("empty series or arch")
	}
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"tools", t}}}},
	}}
	if err := u.st.runner.Run(ops, "", nil); err != nil {
		return onAbort(err, errNotAlive)
	}
	tools := *t
	u.doc.Tools = &tools
	return nil
}

// SetMongoPassword sets the password the agent responsible for the unit
// should use to communicate with the state servers.  Previous passwords
// are invalidated.
func (u *Unit) SetMongoPassword(password string) error {
	return u.st.setMongoPassword(u.EntityName(), password)
}

// SetPassword sets the password for the machine's agent.
func (u *Unit) SetPassword(password string) error {
	hp := trivial.PasswordHash(password)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"passwordhash", hp}}}},
	}}
	err := u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot set password of unit %q: %v", u, onAbort(err, errNotAlive))
	}
	u.doc.PasswordHash = hp
	return nil
}

// PasswordValid returns whether the given password is valid
// for the given unit.
func (u *Unit) PasswordValid(password string) bool {
	return trivial.PasswordHash(password) == u.doc.PasswordHash
}

// Destroy sets the unit lifecycle to Dying if it is Alive.
// It does nothing otherwise.
func (u *Unit) Destroy() error {
	err := ensureDying(u.st, u.st.units, u.doc.Name, "unit")
	if err != nil {
		return err
	}
	u.doc.Life = Dying
	return nil
}

var ErrUnitHasSubordinates = errors.New("unit has subordinates")

// EnsureDead sets the unit lifecycle to Dead if it is Alive or Dying.
// It does nothing otherwise. If the unit has subordinates, it will
// return ErrUnitHasSubordinates.
func (u *Unit) EnsureDead() (err error) {
	if u.doc.Life == Dead {
		return nil
	}
	defer func() {
		if err == nil {
			u.doc.Life = Dead
		}
	}()
	ops := []txn.Op{{
		C:  u.st.units.Name,
		Id: u.doc.Name,
		Assert: append(notDeadDoc, bson.DocElem{
			"$or", []D{
				{{"subordinates", D{{"$size", 0}}}},
				{{"subordinates", D{{"$exists", false}}}},
			},
		}),
		Update: D{{"$set", D{{"life", Dead}}}},
	}}
	if err := u.st.runner.Run(ops, "", nil); err != txn.ErrAborted {
		return err
	}
	if notDead, err := isNotDead(u.st.units, u.doc.Name); err != nil {
		return err
	} else if !notDead {
		return nil
	}
	return ErrUnitHasSubordinates
}

// Remove removes the unit from state, and may remove its service as well, if
// the service is Dying and no other references to it exist. It will fail if
// the unit is not Dead.
func (u *Unit) Remove() (err error) {
	defer trivial.ErrorContextf(&err, "cannot remove unit %q", u)
	if u.doc.Life != Dead {
		return errors.New("unit is not dead")
	}
	svc, err := u.st.Service(u.doc.Service)
	if err != nil {
		return err
	}
	unit := &Unit{st: u.st, doc: u.doc}
	for i := 0; i < 5; i++ {
		ops := svc.removeUnitOps(unit)
		if err := svc.st.runner.Run(ops, "", nil); err != txn.ErrAborted {
			return err
		}
		if err := svc.Refresh(); IsNotFound(err) {
			return nil
		} else if err != nil {
			return err
		}
		if err := unit.Refresh(); IsNotFound(err) {
			return nil
		} else if err != nil {
			return err
		}
	}
	return ErrExcessiveContention
}

// Resolved returns the resolved mode for the unit.
func (u *Unit) Resolved() ResolvedMode {
	return u.doc.Resolved
}

// IsPrincipal returns whether the unit is deployed in its own container,
// and can therefore have subordinate services deployed alongside it.
func (u *Unit) IsPrincipal() bool {
	return u.doc.Principal == ""
}

// SubordinateNames returns the names of any subordinate units.
func (u *Unit) SubordinateNames() []string {
	names := make([]string, len(u.doc.Subordinates))
	copy(names, u.doc.Subordinates)
	return names
}

// DeployerName returns the entity name of the agent responsible for deploying
// the unit. If no such entity can be determined, false is returned.
func (u *Unit) DeployerName() (string, bool) {
	if u.doc.Principal != "" {
		return UnitEntityName(u.doc.Principal), true
	} else if u.doc.MachineId != "" {
		return MachineEntityName(u.doc.MachineId), true
	}
	return "", false
}

// PublicAddress returns the public address of the unit and whether it is valid.
func (u *Unit) PublicAddress() (string, bool) {
	return u.doc.PublicAddress, u.doc.PublicAddress != ""
}

// PrivateAddress returns the private address of the unit and whether it is valid.
func (u *Unit) PrivateAddress() (string, bool) {
	return u.doc.PrivateAddress, u.doc.PrivateAddress != ""
}

// Refresh refreshes the contents of the Unit from the underlying
// state. It an error that satisfies IsNotFound if the unit has been removed.
func (u *Unit) Refresh() error {
	err := u.st.units.FindId(u.doc.Name).One(&u.doc)
	if err == mgo.ErrNotFound {
		return NotFoundf("unit %q", u)
	}
	if err != nil {
		return fmt.Errorf("cannot refresh unit %q: %v", u, err)
	}
	return nil
}

// Status returns the status of the unit's agent.
func (u *Unit) Status() (status UnitStatus, info string, err error) {
	status, info = u.doc.Status, u.doc.StatusInfo
	if status == UnitPending {
		return
	}
	switch status {
	case UnitError:
		// We always expect an info if status is 'error'.
		if info == "" {
			panic("no status-info found for unit error")
		}
		return
	case UnitStopped:
		return
	}

	// TODO(fwereade,niemeyer): Take this out of Status and drop error result.
	alive, err := u.AgentAlive()
	if err != nil {
		return "", "", err
	}
	if !alive {
		return UnitDown, "", nil
	}
	return
}

// SetStatus sets the status of the unit.
func (u *Unit) SetStatus(status UnitStatus, info string) error {
	if status == UnitPending {
		panic("unit status must not be set to pending")
	}
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"status", status}, {"statusinfo", info}}}},
	}}
	err := u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot set status of unit %q: %v", u, onAbort(err, errNotAlive))
	}
	u.doc.Status = status
	u.doc.StatusInfo = info
	return nil
}

// OpenPort sets the policy of the port with protocol and number to be opened.
func (u *Unit) OpenPort(protocol string, number int) (err error) {
	port := Port{Protocol: protocol, Number: number}
	defer trivial.ErrorContextf(&err, "cannot open port %v for unit %q", port, u)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$addToSet", D{{"ports", port}}}},
	}}
	err = u.st.runner.Run(ops, "", nil)
	if err != nil {
		return onAbort(err, errNotAlive)
	}
	found := false
	for _, p := range u.doc.Ports {
		if p == port {
			break
		}
	}
	if !found {
		u.doc.Ports = append(u.doc.Ports, port)
	}
	return nil
}

// ClosePort sets the policy of the port with protocol and number to be closed.
func (u *Unit) ClosePort(protocol string, number int) (err error) {
	port := Port{Protocol: protocol, Number: number}
	defer trivial.ErrorContextf(&err, "cannot close port %v for unit %q", port, u)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$pull", D{{"ports", port}}}},
	}}
	err = u.st.runner.Run(ops, "", nil)
	if err != nil {
		return onAbort(err, errNotAlive)
	}
	newPorts := make([]Port, 0, len(u.doc.Ports))
	for _, p := range u.doc.Ports {
		if p != port {
			newPorts = append(newPorts, p)
		}
	}
	u.doc.Ports = newPorts
	return nil
}

// OpenedPorts returns a slice containing the open ports of the unit.
func (u *Unit) OpenedPorts() []Port {
	ports := append([]Port{}, u.doc.Ports...)
	SortPorts(ports)
	return ports
}

// CharmURL returns the charm URL this unit is currently using.
func (u *Unit) CharmURL() (*charm.URL, bool) {
	if u.doc.CharmURL == nil {
		return nil, false
	}
	return u.doc.CharmURL, true
}

// SetCharmURL marks the unit as currently using the supplied charm URL.
// An error will be returned if the unit is dead, or the charm URL not known.
func (u *Unit) SetCharmURL(curl *charm.URL) error {
	if curl == nil {
		return fmt.Errorf("cannot set nil charm url")
	}
	ops := []txn.Op{{
		// This will be replaced by a more stringent check in a follow-up.
		C:      u.st.charms.Name,
		Id:     curl,
		Assert: txn.DocExists,
	}, {
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"charmurl", curl}}}},
	}}
	if err := u.st.runner.Run(ops, "", nil); err == nil {
		u.doc.CharmURL = curl
		return nil
	} else if err != txn.ErrAborted {
		return err
	}
	if notDead, err := isNotDead(u.st.units, u.doc.Name); err != nil {
		return err
	} else if notDead {
		return fmt.Errorf("unknown charm url %q", curl)
	}
	return fmt.Errorf("unit %q is dead", u)
}

// AgentAlive returns whether the respective remote agent is alive.
func (u *Unit) AgentAlive() (bool, error) {
	return u.st.pwatcher.Alive(u.globalKey())
}

// UnitEntityName returns the entity name for the
// unit with the given name.
func UnitEntityName(unitName string) string {
	return "unit-" + strings.Replace(unitName, "/", "-", -1)
}

// EntityName returns a name identifying the unit that is safe to use
// as a file name.  The returned name will be different from other
// EntityName values returned by any other entities from the same state.
func (u *Unit) EntityName() string {
	return UnitEntityName(u.Name())
}

// WaitAgentAlive blocks until the respective agent is alive.
func (u *Unit) WaitAgentAlive(timeout time.Duration) (err error) {
	defer trivial.ErrorContextf(&err, "waiting for agent of unit %q", u)
	ch := make(chan presence.Change)
	u.st.pwatcher.Watch(u.globalKey(), ch)
	defer u.st.pwatcher.Unwatch(u.globalKey(), ch)
	for i := 0; i < 2; i++ {
		select {
		case change := <-ch:
			if change.Alive {
				return nil
			}
		case <-time.After(timeout):
			return fmt.Errorf("still not alive after timeout")
		case <-u.st.pwatcher.Dead():
			return u.st.pwatcher.Err()
		}
	}
	panic(fmt.Sprintf("presence reported dead status twice in a row for unit %q", u))
}

// SetAgentAlive signals that the agent for unit u is alive.
// It returns the started pinger.
func (u *Unit) SetAgentAlive() (*presence.Pinger, error) {
	p := presence.NewPinger(u.st.presence, u.globalKey())
	err := p.Start()
	if err != nil {
		return nil, err
	}
	return p, nil
}

// NotAssignedError indicates that a unit is not assigned to a machine (and, in
// the case of subordinate units, that the unit's principal is not assigned).
type NotAssignedError struct{ Unit *Unit }

func (e *NotAssignedError) Error() string {
	return fmt.Sprintf("unit %q is not assigned to a machine", e.Unit)
}

func IsNotAssigned(err error) bool {
	_, ok := err.(*NotAssignedError)
	return ok
}

// AssignedMachineId returns the id of the assigned machine.
func (u *Unit) AssignedMachineId() (id string, err error) {
	if u.IsPrincipal() {
		if u.doc.MachineId == "" {
			return "", &NotAssignedError{u}
		}
		return u.doc.MachineId, nil
	}
	pudoc := unitDoc{}
	err = u.st.units.Find(D{{"_id", u.doc.Principal}}).One(&pudoc)
	if err == mgo.ErrNotFound {
		return "", NotFoundf("cannot get machine id of unit %q: principal %q", u, u.doc.Principal)
	} else if err != nil {
		return "", err
	}
	if pudoc.MachineId == "" {
		return "", &NotAssignedError{u}
	}
	return pudoc.MachineId, nil
}

var (
	machineDeadErr     = errors.New("machine is dead")
	unitDeadErr        = errors.New("unit is dead")
	alreadyAssignedErr = errors.New("unit is already assigned to a machine")
	inUseErr           = errors.New("machine is not unused")
)

// assignToMachine is the internal version of AssignToMachine,
// also used by AssignToUnusedMachine. It returns specific errors
// in some cases:
// - machineDeadErr when the machine is not alive.
// - unitDeadErr when the unit is not alive.
// - alreadyAssignedErr when the unit has already been assigned
// - inUseErr when the machine already has a unit assigned (if unused is true)
func (u *Unit) assignToMachine(m *Machine, unused bool) (err error) {
	if u.doc.MachineId != "" {
		if u.doc.MachineId != m.Id() {
			return alreadyAssignedErr
		}
		return nil
	}
	if u.doc.Principal != "" {
		return fmt.Errorf("unit is a subordinate")
	}
	canHost := false
	for _, j := range m.doc.Jobs {
		if j == JobHostUnits {
			canHost = true
			break
		}
	}
	if !canHost {
		return fmt.Errorf("machine %q cannot host units", m)
	}
	assert := append(isAliveDoc, D{
		{"$or", []D{
			{{"machineid", ""}},
			{{"machineid", m.Id()}},
		}},
	}...)
	massert := isAliveDoc
	if unused {
		massert = append(massert, D{{"principals", D{{"$size", 0}}}}...)
	}
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: assert,
		Update: D{{"$set", D{{"machineid", m.doc.Id}}}},
	}, {
		C:      u.st.machines.Name,
		Id:     m.doc.Id,
		Assert: massert,
		Update: D{{"$addToSet", D{{"principals", u.doc.Name}}}},
	}}
	err = u.st.runner.Run(ops, "", nil)
	if err == nil {
		u.doc.MachineId = m.doc.Id
		return nil
	}
	if err != txn.ErrAborted {
		return err
	}
	u0, err := u.st.Unit(u.Name())
	if err != nil {
		return err
	}
	m0, err := u.st.Machine(m.Id())
	if err != nil {
		return err
	}
	switch {
	case u0.Life() != Alive:
		return unitDeadErr
	case m0.Life() != Alive:
		return machineDeadErr
	case u0.doc.MachineId != "" || !unused:
		return alreadyAssignedErr
	}
	return inUseErr
}

// AssignToMachine assigns this unit to a given machine.
func (u *Unit) AssignToMachine(m *Machine) error {
	err := u.assignToMachine(m, false)
	if err != nil {
		return fmt.Errorf("cannot assign unit %q to machine %v: %v", u, m, err)
	}
	return nil
}

var noUnusedMachines = errors.New("all machines in use")

// AssignToUnusedMachine assigns u to a machine without other units.
// If there are no unused machines besides machine 0, an error is returned.
func (u *Unit) AssignToUnusedMachine() (m *Machine, err error) {
	// Select all machines that can accept principal units but have none assigned.
	sel := D{{"principals", D{{"$size", 0}}}, {"jobs", JobHostUnits}}
	query := u.st.machines.Find(sel)

	// TODO use Batch(1). See https://bugs.launchpad.net/mgo/+bug/1053509
	// TODO(rog) Fix so this is more efficient when there are concurrent uses.
	// Possible solution: pick the highest and the smallest id of all
	// unused machines, and try to assign to the first one >= a random id in the
	// middle.
	iter := query.Batch(2).Prefetch(0).Iter()
	var mdoc machineDoc
	for iter.Next(&mdoc) {
		m := newMachine(u.st, &mdoc)
		err := u.assignToMachine(m, true)
		if err == nil {
			return m, nil
		}
		if err != inUseErr && err != machineDeadErr {
			return nil, fmt.Errorf("cannot assign unit %q to unused machine: %v", u, err)
		}
	}
	if err := iter.Err(); err != nil {
		return nil, err
	}
	return nil, noUnusedMachines
}

// UnassignFromMachine removes the assignment between this unit and the
// machine it's assigned to.
func (u *Unit) UnassignFromMachine() (err error) {
	// TODO check local machine id and add an assert that the
	// machine id is as expected.
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: txn.DocExists,
		Update: D{{"$set", D{{"machineid", ""}}}},
	}}
	if u.doc.MachineId != "" {
		ops = append(ops, txn.Op{
			C:      u.st.machines.Name,
			Id:     u.doc.MachineId,
			Assert: txn.DocExists,
			Update: D{{"$pull", D{{"principals", u.doc.Name}}}},
		})
	}
	err = u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot unassign unit %q from machine: %v", u, onAbort(err, NotFoundf("machine")))
	}
	u.doc.MachineId = ""
	return nil
}

// SetPublicAddress sets the public address of the unit.
func (u *Unit) SetPublicAddress(address string) (err error) {
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: txn.DocExists,
		Update: D{{"$set", D{{"publicaddress", address}}}},
	}}
	if err := u.st.runner.Run(ops, "", nil); err != nil {
		return fmt.Errorf("cannot set public address of unit %q: %v", u, onAbort(err, NotFoundf("machine")))
	}
	u.doc.PublicAddress = address
	return nil
}

// SetPrivateAddress sets the private address of the unit.
func (u *Unit) SetPrivateAddress(address string) error {
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: txn.DocExists,
		Update: D{{"$set", D{{"privateaddress", address}}}},
	}}
	err := u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot set private address of unit %q: %v", u, NotFoundf("unit"))
	}
	u.doc.PrivateAddress = address
	return nil
}

// SetResolved marks the unit as having had any previous state transition
// problems resolved, and informs the unit that it may attempt to
// reestablish normal workflow. The resolved mode parameter informs
// whether to attempt to reexecute previous failed hooks or to continue
// as if they had succeeded before.
func (u *Unit) SetResolved(mode ResolvedMode) (err error) {
	defer trivial.ErrorContextf(&err, "cannot set resolved mode for unit %q", u)
	switch mode {
	case ResolvedNone, ResolvedRetryHooks, ResolvedNoHooks:
	default:
		return fmt.Errorf("invalid error resolution mode: %q", mode)
	}
	assert := append(notDeadDoc, D{{"resolved", ResolvedNone}}...)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: assert,
		Update: D{{"$set", D{{"resolved", mode}}}},
	}}
	if err := u.st.runner.Run(ops, "", nil); err != nil {
		if err == txn.ErrAborted {
			// Find which assertion failed so we can give a
			// more specific error.
			u1, err := u.st.Unit(u.Name())
			if err != nil {
				return err
			}
			if u1.Life() != Alive {
				return errNotAlive
			}
			return fmt.Errorf("already resolved")
		}
		return err
	}
	u.doc.Resolved = mode
	return nil
}

// ClearResolved removes any resolved setting on the unit.
func (u *Unit) ClearResolved() error {
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: txn.DocExists,
		Update: D{{"$set", D{{"resolved", ResolvedNone}}}},
	}}
	err := u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot clear resolved mode for unit %q: %v", u, NotFoundf("unit"))
	}
	u.doc.Resolved = ResolvedNone
	return nil
}

type portSlice []Port

func (p portSlice) Len() int      { return len(p) }
func (p portSlice) Swap(i, j int) { p[i], p[j] = p[j], p[i] }
func (p portSlice) Less(i, j int) bool {
	p1 := p[i]
	p2 := p[j]
	if p1.Protocol != p2.Protocol {
		return p1.Protocol < p2.Protocol
	}
	return p1.Number < p2.Number
}

// SortPorts sorts the given ports, first by protocol,
// then by number.
func SortPorts(ports []Port) {
	sort.Sort(portSlice(ports))
}
