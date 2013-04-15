package state

import (
	"errors"
	"fmt"
	"labix.org/v2/mgo"
	"labix.org/v2/mgo/txn"
	"launchpad.net/juju-core/charm"
	"launchpad.net/juju-core/state/api/params"
	"launchpad.net/juju-core/state/presence"
	"launchpad.net/juju-core/utils"
	"sort"
	"strings"
	"time"
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

	// AssignNew indicates that every service unit should be assigned to a new
	// dedicated machine.  A new machine will be launched for each new unit.
	AssignNew AssignmentPolicy = "new"
)

// ResolvedMode describes the way state transition errors
// are resolved.
type ResolvedMode string

const (
	ResolvedNone       ResolvedMode = ""
	ResolvedRetryHooks ResolvedMode = "retry-hooks"
	ResolvedNoHooks    ResolvedMode = "no-hooks"
)

// UnitSettings holds information about a service unit's settings within a
// relation.
type UnitSettings struct {
	Version  int64
	Settings map[string]interface{}
}

// unitDoc represents the internal state of a unit in MongoDB.
// Note the correspondence with UnitInfo in state/api/params.
type unitDoc struct {
	Name           string `bson:"_id"`
	Service        string
	Series         string
	CharmURL       *charm.URL
	Principal      string
	Subordinates   []string
	PublicAddress  string
	PrivateAddress string
	MachineId      string
	Resolved       ResolvedMode
	Tools          *Tools `bson:",omitempty"`
	Ports          []params.Port
	Life           Life
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
		st:  st,
		doc: *udoc,
	}
	unit.annotator = annotator{
		globalKey: unit.globalKey(),
		tag:       unit.Tag(),
		st:        st,
	}
	return unit
}

// Service returns the service.
func (u *Unit) Service() (*Service, error) {
	return u.st.Service(u.doc.Service)
}

// ServiceConfig returns the contents of this unit's service configuration.
func (u *Unit) ServiceConfig() (map[string]interface{}, error) {
	if u.doc.CharmURL == nil {
		return nil, fmt.Errorf("unit charm not set")
	}
	settings, err := readSettings(u.st, serviceSettingsKey(u.doc.Service, u.doc.CharmURL))
	if err != nil {
		return nil, err
	}
	charm, err := u.st.Charm(u.doc.CharmURL)
	if err != nil {
		return nil, err
	}
	// Build a dictionary containing charm defaults, and overwrite any
	// values that have actually been set.
	cfg, err := charm.Config().Validate(nil)
	if err != nil {
		return nil, err
	}
	for k, v := range settings.Map() {
		cfg[k] = v
	}
	return cfg, nil
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

// unitGlobalKey returns the global database key for the named unit.
func unitGlobalKey(name string) string {
	return "u#" + name
}

// globalKey returns the global database key for the unit.
func (u *Unit) globalKey() string {
	return unitGlobalKey(u.doc.Name)
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
	defer utils.ErrorContextf(&err, "cannot set agent tools for unit %q", u)
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
		return onAbort(err, errDead)
	}
	tools := *t
	u.doc.Tools = &tools
	return nil
}

// SetMongoPassword sets the password the agent responsible for the unit
// should use to communicate with the state servers.  Previous passwords
// are invalidated.
func (u *Unit) SetMongoPassword(password string) error {
	return u.st.setMongoPassword(u.Tag(), password)
}

// SetPassword sets the password for the machine's agent.
func (u *Unit) SetPassword(password string) error {
	hp := utils.PasswordHash(password)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$set", D{{"passwordhash", hp}}}},
	}}
	err := u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot set password of unit %q: %v", u, onAbort(err, errDead))
	}
	u.doc.PasswordHash = hp
	return nil
}

// PasswordValid returns whether the given password is valid
// for the given unit.
func (u *Unit) PasswordValid(password string) bool {
	return utils.PasswordHash(password) == u.doc.PasswordHash
}

// Destroy, when called on a Alive unit, advances its lifecycle as far as
// possible; it otherwise has no effect. In most situations, the unit's
// life is just set to Dying; but if a principal unit that is not assigned
// to a provisioned machine is Destroyed, it will be removed from state
// directly.
func (u *Unit) Destroy() (err error) {
	defer func() {
		if err == nil {
			// This is a white lie; the document might actually be removed.
			u.doc.Life = Dying
		}
	}()
	unit := &Unit{st: u.st, doc: u.doc}
	for i := 0; i < 5; i++ {
		ops, err := unit.destroyOps()
		switch {
		case err == errRefresh:
		case err == errAlreadyDying:
			return nil
		case err != nil:
			return err
		default:
			if err := unit.st.runner.Run(ops, "", nil); err != txn.ErrAborted {
				return err
			}
		}
		if err := unit.Refresh(); IsNotFound(err) {
			return nil
		} else if err != nil {
			return err
		}
	}
	return ErrExcessiveContention
}

// destroyOps returns the operations required to destroy the unit. If it
// returns errRefresh, the unit should be refreshed and the destruction
// operations recalculated.
func (u *Unit) destroyOps() ([]txn.Op, error) {
	if u.doc.Life != Alive {
		return nil, errAlreadyDying
	}
	// In many cases, we just want to set Dying and let the agents deal with it.
	defaultOps := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: isAliveDoc,
		Update: D{{"$set", D{{"life", Dying}}}},
	}}

	// Subordinates, and principals with subordinates, are left for the agents.
	if u.doc.Principal != "" {
		return defaultOps, nil
	} else if len(u.doc.Subordinates) != 0 {
		return defaultOps, nil
	}

	// If the (known principal) unit has no assigned machine id, the unit can
	// be removed directly.
	asserts := D{{"machineid", u.doc.MachineId}}
	asserts = append(asserts, unitHasNoSubordinates...)
	asserts = append(asserts, isAliveDoc...)
	if u.doc.MachineId == "" {
		return u.removeOps(asserts)
	}

	// If the unit's machine has an instance id, leave it for the agents.
	m, err := u.st.Machine(u.doc.MachineId)
	if IsNotFound(err) {
		return nil, errRefresh
	} else if err != nil {
		return nil, err
	}
	if _, found := m.InstanceId(); found {
		return defaultOps, nil
	}

	// Units assigned to unprovisioned machines can be removed directly.
	ops := []txn.Op{{
		C:      u.st.machines.Name,
		Id:     u.doc.MachineId,
		Assert: D{{"instanceid", ""}},
	}}
	removeOps, err := u.removeOps(asserts)
	if err != nil {
		return nil, err
	}
	return append(ops, removeOps...), nil
}

// removeOps returns the operations necessary to remove the unit, assuming
// the supplied asserts apply to the unit document.
func (u *Unit) removeOps(asserts D) ([]txn.Op, error) {
	svc, err := u.st.Service(u.doc.Service)
	if err != nil {
		return nil, err
	}
	return svc.removeUnitOps(u, asserts)
}

var ErrUnitHasSubordinates = errors.New("unit has subordinates")

var unitHasNoSubordinates = D{{
	"$or", []D{
		{{"subordinates", D{{"$size", 0}}}},
		{{"subordinates", D{{"$exists", false}}}},
	},
}}

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
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: append(notDeadDoc, unitHasNoSubordinates...),
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
	defer utils.ErrorContextf(&err, "cannot remove unit %q", u)
	if u.doc.Life != Dead {
		return errors.New("unit is not dead")
	}
	svc, err := u.st.Service(u.doc.Service)
	if err != nil {
		return err
	}
	unit := &Unit{st: u.st, doc: u.doc}
	for i := 0; i < 5; i++ {
		ops, err := svc.removeUnitOps(unit, isDeadDoc)
		if err != nil {
			return err
		}
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

// DeployerTag returns the tag of the agent responsible for deploying
// the unit. If no such entity can be determined, false is returned.
func (u *Unit) DeployerTag() (string, bool) {
	if u.doc.Principal != "" {
		return UnitTag(u.doc.Principal), true
	} else if u.doc.MachineId != "" {
		return MachineTag(u.doc.MachineId), true
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
func (u *Unit) Status() (status params.Status, info string, err error) {
	doc, err := getStatus(u.st, u.globalKey())
	if err != nil {
		return "", "", err
	}
	status = doc.Status
	info = doc.StatusInfo
	return
}

// SetStatus sets the status of the unit.
func (u *Unit) SetStatus(status params.Status, info string) error {
	if status == params.StatusError && info == "" {
		panic("unit error status with no info")
	}
	doc := statusDoc{
		Status:     status,
		StatusInfo: info,
	}
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
	},
		updateStatusOp(u.st, u.globalKey(), doc),
	}
	err := u.st.runner.Run(ops, "", nil)
	if err != nil {
		return fmt.Errorf("cannot set status of unit %q: %v", u, onAbort(err, errDead))
	}
	return nil
}

// OpenPort sets the policy of the port with protocol and number to be opened.
func (u *Unit) OpenPort(protocol string, number int) (err error) {
	port := params.Port{Protocol: protocol, Number: number}
	defer utils.ErrorContextf(&err, "cannot open port %v for unit %q", port, u)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$addToSet", D{{"ports", port}}}},
	}}
	err = u.st.runner.Run(ops, "", nil)
	if err != nil {
		return onAbort(err, errDead)
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
	port := params.Port{Protocol: protocol, Number: number}
	defer utils.ErrorContextf(&err, "cannot close port %v for unit %q", port, u)
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: notDeadDoc,
		Update: D{{"$pull", D{{"ports", port}}}},
	}}
	err = u.st.runner.Run(ops, "", nil)
	if err != nil {
		return onAbort(err, errDead)
	}
	newPorts := make([]params.Port, 0, len(u.doc.Ports))
	for _, p := range u.doc.Ports {
		if p != port {
			newPorts = append(newPorts, p)
		}
	}
	u.doc.Ports = newPorts
	return nil
}

// OpenedPorts returns a slice containing the open ports of the unit.
func (u *Unit) OpenedPorts() []params.Port {
	ports := append([]params.Port{}, u.doc.Ports...)
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
func (u *Unit) SetCharmURL(curl *charm.URL) (err error) {
	defer func() {
		if err == nil {
			u.doc.CharmURL = curl
		}
	}()
	if curl == nil {
		return fmt.Errorf("cannot set nil charm url")
	}
	for i := 0; i < 5; i++ {
		if notDead, err := isNotDead(u.st.units, u.doc.Name); err != nil {
			return err
		} else if !notDead {
			return fmt.Errorf("unit %q is dead", u)
		}
		sel := D{{"_id", u.doc.Name}, {"charmurl", curl}}
		if count, err := u.st.units.Find(sel).Count(); err != nil {
			return err
		} else if count == 1 {
			// Already set
			return nil
		}
		if count, err := u.st.charms.FindId(curl).Count(); err != nil {
			return err
		} else if count < 1 {
			return fmt.Errorf("unknown charm url %q", curl)
		}

		// Add a reference to the service settings for the new charm.
		incOp, err := settingsIncRefOp(u.st, u.doc.Service, curl, false)
		if err != nil {
			return err
		}

		// Set the new charm URL.
		differentCharm := D{{"charmurl", D{{"$ne", curl}}}}
		ops := []txn.Op{
			incOp,
			{
				C:      u.st.units.Name,
				Id:     u.doc.Name,
				Assert: append(notDeadDoc, differentCharm...),
				Update: D{{"$set", D{{"charmurl", curl}}}},
			}}
		if u.doc.CharmURL != nil {
			// Drop the reference to the old charm.
			decOps, err := settingsDecRefOps(u.st, u.doc.Service, u.doc.CharmURL)
			if err != nil {
				return err
			}
			ops = append(ops, decOps...)
		}
		if err := u.st.runner.Run(ops, "", nil); err != txn.ErrAborted {
			return err
		}
	}
	return ErrExcessiveContention
}

// AgentAlive returns whether the respective remote agent is alive.
func (u *Unit) AgentAlive() (bool, error) {
	return u.st.pwatcher.Alive(u.globalKey())
}

// UnitTag returns the tag for the
// unit with the given name.
func UnitTag(unitName string) string {
	return "unit-" + strings.Replace(unitName, "/", "-", -1)
}

// Tag returns a name identifying the unit that is safe to use
// as a file name.  The returned name will be different from other
// Tag values returned by any other entities from the same state.
func (u *Unit) Tag() string {
	return UnitTag(u.Name())
}

// WaitAgentAlive blocks until the respective agent is alive.
func (u *Unit) WaitAgentAlive(timeout time.Duration) (err error) {
	defer utils.ErrorContextf(&err, "waiting for agent of unit %q", u)
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
		return "", NotFoundf("principal unit %q", u, u.doc.Principal)
	} else if err != nil {
		return "", err
	}
	if pudoc.MachineId == "" {
		return "", &NotAssignedError{u}
	}
	return pudoc.MachineId, nil
}

var (
	machineNotAliveErr = errors.New("machine is not alive")
	unitNotAliveErr    = errors.New("unit is not alive")
	alreadyAssignedErr = errors.New("unit is already assigned to a machine")
	inUseErr           = errors.New("machine is not unused")
)

// assignToMachine is the internal version of AssignToMachine,
// also used by AssignToUnusedMachine. It returns specific errors
// in some cases:
// - machineNotAliveErr when the machine is not alive.
// - unitNotAliveErr when the unit is not alive.
// - alreadyAssignedErr when the unit has already been assigned
// - inUseErr when the machine already has a unit assigned (if unused is true)
func (u *Unit) assignToMachine(m *Machine, unused bool) (err error) {
	if u.doc.Series != m.doc.Series {
		return fmt.Errorf("series does not match")
	}
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
		return unitNotAliveErr
	case m0.Life() != Alive:
		return machineNotAliveErr
	case u0.doc.MachineId != "" || !unused:
		return alreadyAssignedErr
	}
	return inUseErr
}

func assignContextf(err *error, unit *Unit, target string) {
	if *err != nil {
		*err = fmt.Errorf("cannot assign unit %q to %s: %v", unit, target, *err)
	}
}

// AssignToMachine assigns this unit to a given machine.
func (u *Unit) AssignToMachine(m *Machine) (err error) {
	defer assignContextf(&err, u, fmt.Sprintf("machine %s", m))
	return u.assignToMachine(m, false)
}

// AssignToNewMachine assigns the unit to a new machine, with constraints
// determined according to the service and environment constraints at the
// time of unit creation.
func (u *Unit) AssignToNewMachine() (err error) {
	defer assignContextf(&err, u, "new machine")
	if u.doc.Principal != "" {
		return fmt.Errorf("unit is a subordinate")
	}
	// Get the ops necessary to create a new machine, and the machine doc that
	// will be added with those operations (which includes the machine id).
	cons, err := readConstraints(u.st, u.globalKey())
	if IsNotFound(err) {
		// Lack of constraints indicates lack of unit.
		return NotFoundf("unit")
	} else if err != nil {
		return err
	}
	mdoc := &machineDoc{
		Series:     u.doc.Series,
		Jobs:       []MachineJob{JobHostUnits},
		Principals: []string{u.doc.Name},
	}
	mdoc, ops, err := u.st.addMachineOps(mdoc, cons)
	if err != nil {
		return err
	}
	isUnassigned := D{{"machineid", ""}}
	ops = append(ops, txn.Op{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: append(isAliveDoc, isUnassigned...),
		Update: D{{"$set", D{{"machineid", mdoc.Id}}}},
	})
	err = u.st.runner.Run(ops, "", nil)
	if err == nil {
		u.doc.MachineId = mdoc.Id
		return nil
	} else if err != txn.ErrAborted {
		return err
	}
	// If we assume that the machine ops will never give us an operation that
	// would fail (because the machine id that it has is unique), then the only
	// reasons that the transaction could have been aborted are:
	//  * the unit is no longer alive
	//  * the unit has been assigned to a different machine
	unit, err := u.st.Unit(u.Name())
	if err != nil {
		return err
	}
	switch {
	case unit.Life() != Alive:
		return unitNotAliveErr
	case unit.doc.MachineId != "":
		return alreadyAssignedErr
	}
	// Other error condition not considered.
	return fmt.Errorf("unknown error")
}

var noUnusedMachines = errors.New("all eligible machines in use")

// AssignToUnusedMachine assigns u to a machine without other units.
// If there are no unused machines besides machine 0, an error is returned.
// This method does not take constraints into consideration when choosing a
// machine (lp:1161919).
func (u *Unit) AssignToUnusedMachine() (m *Machine, err error) {
	// Select all machines that can accept principal units but have none assigned.
	query := u.st.machines.Find(D{
		{"life", Alive},
		{"series", u.doc.Series},
		{"jobs", JobHostUnits},
		{"principals", D{{"$size", 0}}},
	})

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
		if err != inUseErr && err != machineNotAliveErr {
			assignContextf(&err, u, "unused machine")
			return nil, err
		}
	}
	if err := iter.Err(); err != nil {
		assignContextf(&err, u, "unused machine")
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

// Resolve marks the unit as having had any previous state transition
// problems resolved, and informs the unit that it may attempt to
// reestablish normal workflow. The retryHooks parameter informs
// whether to attempt to reexecute previous failed hooks or to continue
// as if they had succeeded before.
func (u *Unit) Resolve(retryHooks bool) error {
	status, _, err := u.Status()
	if err != nil {
		return err
	}
	if status != params.StatusError {
		return fmt.Errorf("unit %q is not in an error state", u)
	}
	mode := ResolvedNoHooks
	if retryHooks {
		mode = ResolvedRetryHooks
	}
	return u.SetResolved(mode)
}

// SetResolved marks the unit as having had any previous state transition
// problems resolved, and informs the unit that it may attempt to
// reestablish normal workflow. The resolved mode parameter informs
// whether to attempt to reexecute previous failed hooks or to continue
// as if they had succeeded before.
func (u *Unit) SetResolved(mode ResolvedMode) (err error) {
	defer utils.ErrorContextf(&err, "cannot set resolved mode for unit %q", u)
	switch mode {
	case ResolvedRetryHooks, ResolvedNoHooks:
	default:
		return fmt.Errorf("invalid error resolution mode: %q", mode)
	}
	// TODO(fwereade): assert unit has error status.
	resolvedNotSet := D{{"resolved", ResolvedNone}}
	ops := []txn.Op{{
		C:      u.st.units.Name,
		Id:     u.doc.Name,
		Assert: append(notDeadDoc, resolvedNotSet...),
		Update: D{{"$set", D{{"resolved", mode}}}},
	}}
	if err := u.st.runner.Run(ops, "", nil); err == nil {
		u.doc.Resolved = mode
		return nil
	} else if err != txn.ErrAborted {
		return err
	}
	if ok, err := isNotDead(u.st.units, u.doc.Name); err != nil {
		return err
	} else if !ok {
		return errDead
	}
	// For now, the only remaining assert is that resolved was unset.
	return fmt.Errorf("already resolved")
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

type portSlice []params.Port

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
func SortPorts(ports []params.Port) {
	sort.Sort(portSlice(ports))
}
