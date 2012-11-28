package state

import (
	"labix.org/v2/mgo"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/state/watcher"
	"launchpad.net/tomb"
	"strings"
)

// commonWatcher is part of all client watchers.
type commonWatcher struct {
	st   *State
	tomb tomb.Tomb
}

// Stop stops the watcher, and returns any error encountered while running
// or shutting down.
func (w *commonWatcher) Stop() error {
	w.tomb.Kill(nil)
	return w.tomb.Wait()
}

// Err returns any error encountered while running or shutting down, or
// tomb.ErrStillAlive if the watcher is still running.
func (w *commonWatcher) Err() error {
	return w.tomb.Err()
}

// RelationScopeWatcher observes changes to the set of units
// in a particular relation scope.
type RelationScopeWatcher struct {
	commonWatcher
	prefix     string
	ignore     string
	knownUnits map[string]bool
	changeChan chan *RelationScopeChange
}

// RelationScopeChange contains information about units that have
// entered or left a particular scope.
type RelationScopeChange struct {
	Entered []string
	Left    []string
}

// MachinePrincipalUnitsWatcher observes the assignment and removal of units
// to and from a machine.
type MachinePrincipalUnitsWatcher struct {
	commonWatcher
	machine    *Machine
	changeChan chan *MachinePrincipalUnitsChange
	knownUnits map[string]*Unit
}

// MachinePrincipalUnitsChange contains information about units that have been
// assigned to or removed from the machine.
type MachinePrincipalUnitsChange struct {
	Added   []*Unit
	Removed []*Unit
}

func hasString(changes []string, name string) bool {
	for _, v := range changes {
		if v == name {
			return true
		}
	}
	return false
}

func hasInt(changes []int, id int) bool {
	for _, v := range changes {
		if v == id {
			return true
		}
	}
	return false
}

// MachinesWatcher notifies about lifecycle changes for all machines
// in the environment.
//
// The first event emitted will contain the ids of all machines found
// irrespective of their life state. From then on a new event is emitted
// whenever one or more machines are added or change their lifecycle.
//
// After a machine is found to be Dead, no further event will include it.
type MachinesWatcher struct {
	commonWatcher
	out  chan []string
	life map[string]Life
}

var lifeFields = D{{"_id", 1}, {"life", 1}}

// WatchMachines returns a new MachinesWatcher.
func (s *State) WatchMachines() *MachinesWatcher {
	return newMachinesWatcher(s)
}

// WatchMachines returns a new MachinesWatcher.
func newMachinesWatcher(st *State) *MachinesWatcher {
	w := &MachinesWatcher{
		commonWatcher: commonWatcher{st: st},
		out:           make(chan []string),
		life:          make(map[string]Life),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop())
	}()
	return w
}

// Changes returns the event channel for the MachinesWatcher.
func (w *MachinesWatcher) Changes() <-chan []string {
	return w.out
}

func (w *MachinesWatcher) initial() (ids []string, err error) {
	iter := w.st.machines.Find(nil).Select(lifeFields).Iter()
	var doc machineDoc
	for iter.Next(&doc) {
		ids = append(ids, doc.Id)
		w.life[doc.Id] = doc.Life
	}
	if err := iter.Err(); err != nil {
		return nil, err
	}
	return ids, nil
}

func (w *MachinesWatcher) merge(ids []string, ch watcher.Change) ([]string, error) {
	id := ch.Id.(string)
	for _, pending := range ids {
		if id == pending {
			return ids, nil
		}
	}
	if ch.Revno == -1 {
		if life, ok := w.life[id]; ok && life != Dead {
			ids = append(ids, id)
		}
		delete(w.life, id)
		return ids, nil
	}
	doc := machineDoc{Id: id, Life: Dead}
	err := w.st.machines.FindId(id).Select(lifeFields).One(&doc)
	if err != nil && err != mgo.ErrNotFound {
		return nil, err
	}
	if life, ok := w.life[id]; !ok || doc.Life != life {
		ids = append(ids, id)
		if err != mgo.ErrNotFound {
			w.life[id] = doc.Life
		}
	}
	return ids, nil
}

func (w *MachinesWatcher) loop() (err error) {
	ch := make(chan watcher.Change)
	w.st.watcher.WatchCollection(w.st.machines.Name, ch)
	defer w.st.watcher.UnwatchCollection(w.st.machines.Name, ch)
	ids, err := w.initial()
	if err != nil {
		return err
	}
	out := w.out
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-ch:
			if ids, err = w.merge(ids, c); err != nil {
				return err
			}
			if len(ids) > 0 {
				out = w.out
			}
		case out <- ids:
			ids = nil
			out = nil
		}
	}
	return nil
}

// ServicesWatcher notifies about the lifecycle changes for the services
// in the environment. The first event returned by the watcher is the set
// of names of all services, irrespective of their life state. Subsequent
// events returns batches of newly added services and services which have
// changed their lifecycle. After a service is found dead, no further event
// will include it.
type ServicesWatcher struct {
	commonWatcher
	out   chan []string
	known map[string]Life
}

// Changes returns the event channel for w.
func (w *ServicesWatcher) Changes() <-chan []string {
	return w.out
}

// WatchServices returns a new ServicesWatcher.
func (s *State) WatchServices() *ServicesWatcher {
	return newServicesWatcher(s)
}

func newServicesWatcher(s *State) *ServicesWatcher {
	w := &ServicesWatcher{
		commonWatcher: commonWatcher{st: s},
		out:           make(chan []string),
		known:         make(map[string]Life),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop())
	}()
	return w
}

func (w *ServicesWatcher) initial() (change []string, err error) {
	doc := &serviceDoc{}
	iter := w.st.services.Find(nil).Select(lifeFields).Iter()
	for iter.Next(doc) {
		w.known[doc.Name] = doc.Life
		change = append(change, doc.Name)
	}
	if iter.Err() != nil {
		return nil, err
	}
	return change, nil
}

func (w *ServicesWatcher) merge(pending []string, name string) (new []string, err error) {
	doc := serviceDoc{}
	err = w.st.services.FindId(name).One(&doc)
	if err != nil && err != mgo.ErrNotFound {
		return nil, err
	}
	life, known := w.known[name]
	if err == mgo.ErrNotFound {
		delete(w.known, name)
		if known && life != Dead && !hasString(pending, name) {
			return append(pending, name), nil
		}
		return pending, nil
	}
	w.known[name] = doc.Life
	if !known {
		return append(pending, name), nil
	}
	if life == doc.Life || hasString(pending, name) {
		return pending, nil
	}
	return append(pending, name), nil
}

func (w *ServicesWatcher) loop() (err error) {
	ch := make(chan watcher.Change)
	w.st.watcher.WatchCollection(w.st.services.Name, ch)
	defer w.st.watcher.UnwatchCollection(w.st.services.Name, ch)
	changes, err := w.initial()
	if err != nil {
		return err
	}
	out := w.out
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-ch:
			name := c.Id.(string)
			changes, err = w.merge(changes, name)
			if err != nil {
				return err
			}
			if len(changes) > 0 {
				out = w.out
			}
		case out <- changes:
			out = nil
			changes = nil
		}
	}
	return nil
}

// ServiceUnitsWatcher notifies about the lifecycle changes of the units
// belonging to the service. The first event returned by the watcher is the
// set of names of all units that are part of the service, irrespective of
// their life state. Subsequent events return batches of newly added units
// and units which have changed their lifecycle.  After a unit is reported
// to be Dead, no further event will include it.
type ServiceUnitsWatcher struct {
	commonWatcher
	service *Service
	out     chan []string
	known   map[string]Life
}

// Changes returns the event channel for w.
func (w *ServiceUnitsWatcher) Changes() <-chan []string {
	return w.out
}

// WatchUnits returns a new ServiceUnitsWatcher for s.
func (s *Service) WatchUnits() *ServiceUnitsWatcher {
	return newServiceUnitsWatcher(s)
}

func newServiceUnitsWatcher(svc *Service) *ServiceUnitsWatcher {
	w := &ServiceUnitsWatcher{
		commonWatcher: commonWatcher{st: svc.st},
		known:         make(map[string]Life),
		out:           make(chan []string),
		service:       &Service{svc.st, svc.doc}, // Copy so it may be freely refreshed.
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop())
	}()
	return w
}

func (w *ServiceUnitsWatcher) merge(pending []string, name string) (changes []string, err error) {
	doc := unitDoc{}
	err = w.st.units.FindId(name).One(&doc)
	if err != nil && err != mgo.ErrNotFound {
		return nil, err
	}
	life, known := w.known[name]
	if err == mgo.ErrNotFound {
		delete(w.known, name)
		if known && life != Dead && !hasString(pending, name) {
			return append(pending, name), nil
		}
		return pending, nil
	}
	w.known[name] = doc.Life
	if !known {
		return append(pending, name), nil
	}
	if life == doc.Life || hasString(pending, name) {
		return pending, nil
	}
	return append(pending, name), nil
}

func (w *ServiceUnitsWatcher) initial() (changes []string, err error) {
	doc := &unitDoc{}
	iter := w.st.units.Find(D{{"service", w.service.doc.Name}}).Select(lifeFields).Iter()
	for iter.Next(doc) {
		w.known[doc.Name] = doc.Life
		changes = append(changes, doc.Name)
	}
	if iter.Err() != nil {
		return nil, err
	}
	return changes, nil
}

func (w *ServiceUnitsWatcher) loop() (err error) {
	ch := make(chan watcher.Change)
	w.st.watcher.WatchCollection(w.st.units.Name, ch)
	defer w.st.watcher.UnwatchCollection(w.st.units.Name, ch)
	changes, err := w.initial()
	if err != nil {
		return err
	}
	prefix := w.service.doc.Name + "/"
	out := w.out
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-ch:
			name := c.Id.(string)
			if !strings.HasPrefix(name, prefix) {
				continue
			}
			changes, err = w.merge(changes, name)
			if err != nil {
				return err
			}
			if len(changes) > 0 {
				out = w.out
			}
		case out <- changes:
			out = nil
			changes = nil
		}
	}
	return nil
}

// ServiceRelationsWatcher notifies about the lifecycle changes of the
// relations the service is in. The first event returned by the watcher is
// the set of ids of all relations that the service is part of, irrespective
// of their life state. Subsequent events return batches of newly added
// relations and relations which have changed their lifecycle. After a
// relation is reported to be Dead, no further event will include it.
type ServiceRelationsWatcher struct {
	commonWatcher
	service *Service
	out     chan []int
	known   map[string]relationDoc
}

// WatchRelations returns a new ServiceRelationsWatcher for s.
func (s *Service) WatchRelations() *ServiceRelationsWatcher {
	return newServiceRelationsWatcher(s)
}

func newServiceRelationsWatcher(s *Service) *ServiceRelationsWatcher {
	w := &ServiceRelationsWatcher{
		commonWatcher: commonWatcher{st: s.st},
		out:           make(chan []int),
		known:         make(map[string]relationDoc),
		service:       &Service{s.st, s.doc}, // Copy so it may be freely refreshed
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop())
	}()
	return w
}

func (w *ServiceRelationsWatcher) Stop() error {
	w.tomb.Kill(nil)
	return w.tomb.Wait()
}

// Changes returns the event channel for w.
func (w *ServiceRelationsWatcher) Changes() <-chan []int {
	return w.out
}

func (w *ServiceRelationsWatcher) initial() (new []int, err error) {
	doc := relationDoc{}
	iter := w.st.relations.Find(D{{"endpoints.servicename", w.service.doc.Name}}).Select(append(D{{"id", 1}}, lifeFields...)).Iter()
	for iter.Next(&doc) {
		w.known[doc.Key] = doc
		new = append(new, doc.Id)
	}
	if iter.Err() != nil {
		return nil, err
	}
	return new, nil
}

func (w *ServiceRelationsWatcher) merge(pending []int, key string) (new []int, err error) {
	doc := relationDoc{}
	err = w.st.relations.FindId(key).One(&doc)
	if err != nil && err != mgo.ErrNotFound {
		return nil, err
	}
	old, known := w.known[key]
	if err == mgo.ErrNotFound {
		if known && old.Life != Dead && !hasInt(pending, old.Id) {
			delete(w.known, key)
			return append(pending, old.Id), nil
		}
		return pending, nil
	}
	w.known[key] = doc
	if !known {
		return append(pending, doc.Id), nil
	}
	if old.Life == doc.Life || hasInt(pending, old.Id) {
		return pending, nil
	}
	return append(pending, doc.Id), nil
}

func (w *ServiceRelationsWatcher) loop() (err error) {
	ch := make(chan watcher.Change)
	w.st.watcher.WatchCollection(w.st.relations.Name, ch)
	defer w.st.watcher.UnwatchCollection(w.st.relations.Name, ch)
	changes, err := w.initial()
	if err != nil {
		return err
	}
	prefix1 := w.service.doc.Name + ":"
	prefix2 := " " + w.service.doc.Name + ":"
	out := w.out
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-ch:
			key := c.Id.(string)
			if !strings.HasPrefix(key, prefix1) && !strings.Contains(key, prefix2) {
				continue
			}
			changes, err = w.merge(changes, key)
			if err != nil {
				return err
			}
			if len(changes) > 0 {
				out = w.out
			}
		case out <- changes:
			out = nil
			changes = nil
		}
	}
	return nil
}

// WatchPrincipalUnits returns a watcher for observing units being
// added to or removed from the machine.
func (m *Machine) WatchPrincipalUnits() *MachinePrincipalUnitsWatcher {
	return newMachinePrincipalUnitsWatcher(m)
}

// newMachinePrincipalUnitsWatcher creates and starts a watcher to watch information
// about units being added to or deleted from the machine.
func newMachinePrincipalUnitsWatcher(m *Machine) *MachinePrincipalUnitsWatcher {
	w := &MachinePrincipalUnitsWatcher{
		changeChan:    make(chan *MachinePrincipalUnitsChange),
		machine:       m,
		knownUnits:    make(map[string]*Unit),
		commonWatcher: commonWatcher{st: m.st},
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.changeChan)
		w.tomb.Kill(w.loop())
	}()
	return w
}

// Changes returns a channel that will receive changes when units are
// added or deleted. The Added field in the first event on the channel
// holds the initial state as returned by Machine.Units.
func (w *MachinePrincipalUnitsWatcher) Changes() <-chan *MachinePrincipalUnitsChange {
	return w.changeChan
}

func (w *MachinePrincipalUnitsWatcher) mergeChange(changes *MachinePrincipalUnitsChange, ch watcher.Change) (err error) {
	err = w.machine.Refresh()
	if err != nil {
		return err
	}
	units := make(map[string]*Unit)
	for _, name := range w.machine.doc.Principals {
		var unit *Unit
		doc := &unitDoc{}
		if _, ok := w.knownUnits[name]; !ok {
			err = w.st.units.FindId(name).One(doc)
			if err == mgo.ErrNotFound {
				continue
			}
			if err != nil {
				return err
			}
			unit = newUnit(w.st, doc)
			changes.Added = append(changes.Added, unit)
			w.knownUnits[name] = unit
		}
		units[name] = unit
	}
	for name, unit := range w.knownUnits {
		if _, ok := units[name]; !ok {
			changes.Removed = append(changes.Removed, unit)
			delete(w.knownUnits, name)
		}
	}
	return nil
}

func (changes *MachinePrincipalUnitsChange) isEmpty() bool {
	return len(changes.Added)+len(changes.Removed) == 0
}

func (w *MachinePrincipalUnitsWatcher) getInitialEvent() (initial *MachinePrincipalUnitsChange, err error) {
	changes := &MachinePrincipalUnitsChange{}
	docs := []unitDoc{}
	err = w.st.units.Find(D{{"_id", D{{"$in", w.machine.doc.Principals}}}}).All(&docs)
	if err != nil {
		return nil, err
	}
	for _, doc := range docs {
		unit := newUnit(w.st, &doc)
		w.knownUnits[doc.Name] = unit
		changes.Added = append(changes.Added, unit)
	}
	return changes, nil
}

func (w *MachinePrincipalUnitsWatcher) loop() (err error) {
	ch := make(chan watcher.Change)
	w.st.watcher.Watch(w.st.machines.Name, w.machine.doc.Id, w.machine.doc.TxnRevno, ch)
	defer w.st.watcher.Unwatch(w.st.machines.Name, w.machine.doc.Id, ch)
	changes, err := w.getInitialEvent()
	if err != nil {
		return err
	}
	for {
		for changes != nil {
			select {
			case <-w.st.watcher.Dead():
				return watcher.MustErr(w.st.watcher)
			case <-w.tomb.Dying():
				return tomb.ErrDying
			case c := <-ch:
				err := w.mergeChange(changes, c)
				if err != nil {
					return err
				}
			case w.changeChan <- changes:
				changes = nil
			}
		}
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-ch:
			changes = &MachinePrincipalUnitsChange{}
			err := w.mergeChange(changes, c)
			if err != nil {
				return err
			}
			if changes.isEmpty() {
				changes = nil
			}
		}
	}
	return nil
}

func newRelationScopeWatcher(st *State, scope, ignore string) *RelationScopeWatcher {
	w := &RelationScopeWatcher{
		commonWatcher: commonWatcher{st: st},
		prefix:        scope + "#",
		ignore:        ignore,
		changeChan:    make(chan *RelationScopeChange),
		knownUnits:    make(map[string]bool),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.changeChan)
		w.tomb.Kill(w.loop())
	}()
	return w
}

// Changes returns a channel that will receive changes when units enter and
// leave a relation scope. The Entered field in the first event on the channel
// holds the initial state.
func (w *RelationScopeWatcher) Changes() <-chan *RelationScopeChange {
	return w.changeChan
}

func (changes *RelationScopeChange) isEmpty() bool {
	return len(changes.Entered)+len(changes.Left) == 0
}

func (w *RelationScopeWatcher) mergeChange(changes *RelationScopeChange, ch watcher.Change) (err error) {
	doc := &relationScopeDoc{ch.Id.(string)}
	if !strings.HasPrefix(doc.Key, w.prefix) {
		return nil
	}
	name := doc.unitName()
	if name == w.ignore {
		return nil
	}
	if ch.Revno == -1 {
		if w.knownUnits[name] {
			changes.Left = append(changes.Left, name)
			delete(w.knownUnits, name)
		}
		return nil
	}
	if !w.knownUnits[name] {
		changes.Entered = append(changes.Entered, name)
		w.knownUnits[name] = true
	}
	return nil
}

func (w *RelationScopeWatcher) getInitialEvent() (initial *RelationScopeChange, err error) {
	changes := &RelationScopeChange{}
	docs := []relationScopeDoc{}
	sel := D{{"_id", D{{"$regex", "^" + w.prefix}}}}
	err = w.st.relationScopes.Find(sel).All(&docs)
	if err != nil {
		return nil, err
	}
	for _, doc := range docs {
		if name := doc.unitName(); name != w.ignore {
			changes.Entered = append(changes.Entered, name)
			w.knownUnits[name] = true
		}
	}
	return changes, nil
}

func (w *RelationScopeWatcher) loop() error {
	ch := make(chan watcher.Change)
	w.st.watcher.WatchCollection(w.st.relationScopes.Name, ch)
	defer w.st.watcher.UnwatchCollection(w.st.relationScopes.Name, ch)
	changes, err := w.getInitialEvent()
	if err != nil {
		return err
	}
	for {
		for changes != nil {
			select {
			case <-w.st.watcher.Dead():
				return watcher.MustErr(w.st.watcher)
			case <-w.tomb.Dying():
				return tomb.ErrDying
			case c := <-ch:
				if err := w.mergeChange(changes, c); err != nil {
					return err
				}
			case w.changeChan <- changes:
				changes = nil
			}
		}
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-ch:
			changes = &RelationScopeChange{}
			if err := w.mergeChange(changes, c); err != nil {
				return err
			}
			if changes.isEmpty() {
				changes = nil
			}
		}
	}
	return nil
}

// RelationUnitsWatcher sends notifications of units entering and leaving the
// scope of a RelationUnit, and changes to the settings of those units known
// to have entered.
type RelationUnitsWatcher struct {
	commonWatcher
	sw       *RelationScopeWatcher
	watching map[string]bool
	updates  chan watcher.Change
	out      chan RelationUnitsChange
}

// RelationUnitsChange holds notifications of units entering and leaving the
// scope of a RelationUnit, and changes to the settings of those units known
// to have entered.
//
// When a counterpart first enters scope, it is/ noted in the Joined field,
// and its settings are noted in the Changed field. Subsequently, settings
// changes will be noted in the Changed field alone, until the couterpart
// leaves the scope; at that point, it will be noted in the Departed field,
// and no further events will be sent for that counterpart unit.
type RelationUnitsChange struct {
	Joined   []string
	Changed  map[string]UnitSettings
	Departed []string
}

// Watch returns a watcher that notifies of changes to conterpart units in
// the relation.
func (ru *RelationUnit) Watch() *RelationUnitsWatcher {
	return newRelationUnitsWatcher(ru)
}

func newRelationUnitsWatcher(ru *RelationUnit) *RelationUnitsWatcher {
	w := &RelationUnitsWatcher{
		commonWatcher: commonWatcher{st: ru.st},
		sw:            ru.WatchScope(),
		watching:      map[string]bool{},
		updates:       make(chan watcher.Change),
		out:           make(chan RelationUnitsChange),
	}
	go func() {
		defer w.finish()
		w.tomb.Kill(w.loop())
	}()
	return w
}

// Changes returns a channel that will receive the changes to
// counterpart units in a relation. The first event on the
// channel holds the initial state of the relation in its
// Joined and Changed fields.
func (w *RelationUnitsWatcher) Changes() <-chan RelationUnitsChange {
	return w.out
}

func (changes *RelationUnitsChange) empty() bool {
	return len(changes.Joined)+len(changes.Changed)+len(changes.Departed) == 0
}

// mergeSettings reads the relation settings node for the unit with the
// supplied id, and sets a value in the Changed field keyed on the unit's
// name. It returns the mgo/txn revision number of the settings node.
func (w *RelationUnitsWatcher) mergeSettings(changes *RelationUnitsChange, key string) (int64, error) {
	node, err := readSettings(w.st, key)
	if err != nil {
		return -1, err
	}
	name := (&relationScopeDoc{key}).unitName()
	settings := UnitSettings{node.txnRevno, node.Map()}
	if changes.Changed == nil {
		changes.Changed = map[string]UnitSettings{name: settings}
	} else {
		changes.Changed[name] = settings
	}
	return node.txnRevno, nil
}

// mergeScope starts and stops settings watches on the units entering and
// leaving the scope in the supplied RelationScopeChange event, and applies
// the expressed changes to the supplied RelationUnitsChange event.
func (w *RelationUnitsWatcher) mergeScope(changes *RelationUnitsChange, c *RelationScopeChange) error {
	for _, name := range c.Entered {
		key := w.sw.prefix + name
		revno, err := w.mergeSettings(changes, key)
		if err != nil {
			return err
		}
		changes.Joined = append(changes.Joined, name)
		changes.Departed = remove(changes.Departed, name)
		w.st.watcher.Watch(w.st.settings.Name, key, revno, w.updates)
		w.watching[key] = true
	}
	for _, name := range c.Left {
		key := w.sw.prefix + name
		changes.Departed = append(changes.Departed, name)
		if changes.Changed != nil {
			delete(changes.Changed, name)
		}
		changes.Joined = remove(changes.Joined, name)
		w.st.watcher.Unwatch(w.st.settings.Name, key, w.updates)
		delete(w.watching, key)
	}
	return nil
}

// remove removes s from strs and returns the modified slice.
func remove(strs []string, s string) []string {
	for i, v := range strs {
		if s == v {
			strs[i] = strs[len(strs)-1]
			return strs[:len(strs)-1]
		}
	}
	return strs
}

func (w *RelationUnitsWatcher) finish() {
	watcher.Stop(w.sw, &w.tomb)
	for key := range w.watching {
		w.st.watcher.Unwatch(w.st.settings.Name, key, w.updates)
	}
	close(w.updates)
	close(w.out)
	w.tomb.Done()
}

func (w *RelationUnitsWatcher) loop() (err error) {
	sentInitial := false
	changes := RelationUnitsChange{}
	out := w.out
	out = nil
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c, ok := <-w.sw.Changes():
			if !ok {
				return watcher.MustErr(w.sw)
			}
			if err = w.mergeScope(&changes, c); err != nil {
				return err
			}
			if !sentInitial || !changes.empty() {
				out = w.out
			} else {
				out = nil
			}
		case c := <-w.updates:
			if _, err = w.mergeSettings(&changes, c.Id.(string)); err != nil {
				return err
			}
			out = w.out
		case out <- changes:
			sentInitial = true
			changes = RelationUnitsChange{}
			out = nil
		}
	}
	panic("unreachable")
}

// UnitsWatcher notifies of changes to a set of units. Notifications will be
// sent when units enter or leave the set, and when units in the set change
// their lifecycle status. The initial event contains all non-Dead units in
// the set. Once a unit's Dead status has been notified, it will not be
// reported again.
type UnitsWatcher struct {
	commonWatcher
	init     D
	getUnits func() ([]string, error)
	life     map[string]Life
	in       chan watcher.Change
	out      chan []string
}

// WatchSubordinateUnits returns a UnitsWatcher tracking the unit's subordinate units.
func (u *Unit) WatchSubordinateUnits() *UnitsWatcher {
	u = &Unit{u.st, u.doc}
	coll := u.st.units.Name
	init := D{{"_id", D{{"$in", u.doc.Subordinates}}}}
	getUnits := func() ([]string, error) {
		if err := u.Refresh(); err != nil {
			return nil, err
		}
		return u.doc.Subordinates, nil
	}
	return newUnitsWatcher(u.st, init, getUnits, coll, u.doc.Name, u.doc.TxnRevno)
}

func newUnitsWatcher(st *State, init D, getUnits func() ([]string, error), coll, id string, revno int64) *UnitsWatcher {
	w := &UnitsWatcher{
		commonWatcher: commonWatcher{st: st},
		init:          init,
		getUnits:      getUnits,
		life:          map[string]Life{},
		in:            make(chan watcher.Change),
		out:           make(chan []string),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop(coll, id, revno))
	}()
	return w
}

// Changes returns the UnitsWatcher's output channel.
func (w *UnitsWatcher) Changes() <-chan []string {
	return w.out
}

// lifeWatchDoc holds the fields used in starting and maintaining a watch
// on a entity's lifecycle.
type lifeWatchDoc struct {
	Id       string `bson:"_id"`
	Life     Life
	TxnRevno int64 `bson:"txn-revno"`
}

// lifeWatchFields specifies the fields of a lifeWatchDoc.
var lifeWatchFields = D{{"_id", 1}, {"life", 1}, {"txn-revno", 1}}

// initial returns every non-Dead member of the tracked set.
func (w *UnitsWatcher) initial() ([]string, error) {
	docs := []lifeWatchDoc{}
	if err := w.st.units.Find(w.init).Select(lifeWatchFields).All(&docs); err != nil {
		return nil, err
	}
	changes := []string{}
	for _, doc := range docs {
		if doc.Life != Dead {
			w.life[doc.Id] = doc.Life
			w.st.watcher.Watch(w.st.units.Name, doc.Id, doc.TxnRevno, w.in)
			changes = append(changes, doc.Id)
		}
	}
	return changes, nil
}

// update adds to and returns changes, such that it contains the names of any
// non-Dead units to have entered or left the tracked set.
func (w *UnitsWatcher) update(changes []string) ([]string, error) {
	latest, err := w.getUnits()
	if err != nil {
		return nil, err
	}
	for _, name := range latest {
		if _, known := w.life[name]; !known {
			changes, err = w.merge(changes, name)
			if err != nil {
				return nil, err
			}
		}
	}
	for name := range w.life {
		if hasString(latest, name) {
			continue
		}
		if !hasString(changes, name) {
			changes = append(changes, name)
		}
		delete(w.life, name)
		w.st.watcher.Unwatch(w.st.units.Name, name, w.in)
	}
	return changes, nil
}

// merge adds to and returns changes, such that it contains the supplied unit
// name if that unit is unknown and non-Dead, or has changed lifecycle status.
func (w *UnitsWatcher) merge(changes []string, name string) ([]string, error) {
	doc := lifeWatchDoc{}
	err := w.st.units.FindId(name).Select(lifeWatchFields).One(&doc)
	gone := false
	if err == mgo.ErrNotFound {
		gone = true
	} else if err != nil {
		return nil, err
	} else if doc.Life == Dead {
		gone = true
	}
	life, known := w.life[name]
	switch {
	case known && gone:
		delete(w.life, name)
		w.st.watcher.Unwatch(w.st.units.Name, name, w.in)
	case !known && !gone:
		w.st.watcher.Watch(w.st.units.Name, name, doc.TxnRevno, w.in)
		w.life[name] = doc.Life
	case known && life != doc.Life:
		w.life[name] = doc.Life
	default:
		return changes, nil
	}
	if !hasString(changes, name) {
		changes = append(changes, name)
	}
	return changes, nil
}

func (w *UnitsWatcher) loop(coll, id string, revno int64) error {
	w.st.watcher.Watch(coll, id, revno, w.in)
	defer func() {
		w.st.watcher.Unwatch(coll, id, w.in)
		for name := range w.life {
			w.st.watcher.Unwatch(w.st.units.Name, name, w.in)
		}
	}()
	changes, err := w.initial()
	if err != nil {
		return err
	}
	out := w.out
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case c := <-w.in:
			name := c.Id.(string)
			if name == id {
				changes, err = w.update(changes)
			} else {
				changes, err = w.merge(changes, name)
			}
			if err != nil {
				return err
			}
			if len(changes) > 0 {
				out = w.out
			}
		case out <- changes:
			out = nil
			changes = nil
		}
	}
	return nil
}

// EnvironConfigWatcher observes changes to the
// environment configuration.
type EnvironConfigWatcher struct {
	commonWatcher
	out chan *config.Config
}

// WatchEnvironConfig returns a watcher for observing changes
// to the environment configuration.
func (s *State) WatchEnvironConfig() *EnvironConfigWatcher {
	return newEnvironConfigWatcher(s)
}

func newEnvironConfigWatcher(s *State) *EnvironConfigWatcher {
	w := &EnvironConfigWatcher{
		commonWatcher: commonWatcher{st: s},
		out:           make(chan *config.Config),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop())
	}()
	return w
}

// Changes returns a channel that will receive the new environment
// configuration when a change is detected. Note that multiple changes may
// be observed as a single event in the channel.
func (w *EnvironConfigWatcher) Changes() <-chan *config.Config {
	return w.out
}

func (w *EnvironConfigWatcher) loop() (err error) {
	sw := w.st.watchSettings("e")
	defer sw.Stop()
	out := w.out
	out = nil
	cfg := &config.Config{}
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case settings, ok := <-sw.Changes():
			if !ok {
				return watcher.MustErr(sw)
			}
			cfg, err = config.New(settings.Map())
			if err == nil {
				out = w.out
			} else {
				out = nil
			}
		case out <- cfg:
			out = nil
		}
	}
	return nil
}

type settingsWatcher struct {
	commonWatcher
	out chan *Settings
}

// watchSettings creates a watcher for observing changes to settings.
func (s *State) watchSettings(key string) *settingsWatcher {
	return newSettingsWatcher(s, key)
}

func newSettingsWatcher(s *State, key string) *settingsWatcher {
	w := &settingsWatcher{
		commonWatcher: commonWatcher{st: s},
		out:           make(chan *Settings),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop(key))
	}()
	return w
}

// Changes returns a channel that will receive the new settings.
// Multiple changes may be observed as a single event in the channel.
func (w *settingsWatcher) Changes() <-chan *Settings {
	return w.out
}

func (w *settingsWatcher) loop(key string) (err error) {
	ch := make(chan watcher.Change)
	revno := int64(-1)
	settings, err := readSettings(w.st, key)
	if err == nil {
		revno = settings.txnRevno
	} else if !IsNotFound(err) {
		return err
	}
	w.st.watcher.Watch(w.st.settings.Name, key, revno, ch)
	defer w.st.watcher.Unwatch(w.st.settings.Name, key, ch)
	out := w.out
	if revno == -1 {
		out = nil
	}
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case <-ch:
			settings, err = readSettings(w.st, key)
			if err != nil {
				return err
			}
			out = w.out
		case out <- settings:
			out = nil
		}
	}
	return nil
}

type ConfigWatcher struct {
	*settingsWatcher
}

func (s *Service) WatchConfig() *ConfigWatcher {
	return &ConfigWatcher{newSettingsWatcher(s.st, "s#"+s.Name())}
}

// EntityWatcher observes changes to a state entity.
type EntityWatcher struct {
	commonWatcher
	changeChan chan struct{}
}

// Watch return a watcher for observing changes to a service.
func (s *Service) Watch() *EntityWatcher {
	return newEntityWatcher(s.st, s.st.services.Name, s.doc.Name, s.doc.TxnRevno)
}

// Watch return a watcher for observing changes to a unit.
func (u *Unit) Watch() *EntityWatcher {
	return newEntityWatcher(u.st, u.st.units.Name, u.doc.Name, u.doc.TxnRevno)
}

// Watch return a watcher for observing changes to a machine.
func (m *Machine) Watch() *EntityWatcher {
	return newEntityWatcher(m.st, m.st.machines.Name, m.doc.Id, m.doc.TxnRevno)
}

func newEntityWatcher(st *State, coll string, key interface{}, revno int64) *EntityWatcher {
	w := &EntityWatcher{
		commonWatcher: commonWatcher{st: st},
		changeChan:    make(chan struct{}),
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.changeChan)
		ch := make(chan watcher.Change)
		w.st.watcher.Watch(coll, key, revno, ch)
		defer w.st.watcher.Unwatch(coll, key, ch)
		w.tomb.Kill(w.loop(ch))
	}()
	return w
}

// Changes returns the event channel for the EntityWatcher.
func (w *EntityWatcher) Changes() <-chan struct{} {
	return w.changeChan
}

func (w *EntityWatcher) loop(ch <-chan watcher.Change) (err error) {
	out := w.changeChan
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case <-ch:
			out = w.changeChan
		case out <- struct{}{}:
			out = nil
		}
	}
	return nil
}

// MachineUnitsWatcher notifies about assignments and lifecycle changes
// for all units of a machine.
//
// The first event emitted contains the unit names of all units currently
// assigned to the machine, irrespective of their life state. From then on,
// a new event is emitted whenever a unit is assigned to or unassigned from
// the machine, or the lifecycle of a unit that is currently assigned to
// the machine changes.
//
// After a unit is found to be Dead, no further event will include it.
type MachineUnitsWatcher struct {
	commonWatcher
	machine *Machine
	out     chan []string
	in      chan watcher.Change
	known   map[string]Life
}

// WatchUnits returns a new MachineUnitsWatcher for m.
func (m *Machine) WatchUnits() *MachineUnitsWatcher {
	return newMachineUnitsWatcher(m)
}

func newMachineUnitsWatcher(m *Machine) *MachineUnitsWatcher {
	w := &MachineUnitsWatcher{
		commonWatcher: commonWatcher{st: m.st},
		out:           make(chan []string),
		in:            make(chan watcher.Change),
		known:         make(map[string]Life),
		machine:       &Machine{m.st, m.doc}, // Copy so it may be freely refreshed
	}
	go func() {
		defer w.tomb.Done()
		defer close(w.out)
		w.tomb.Kill(w.loop())
	}()
	return w
}

// Changes returns the event channel for w.
func (w *MachineUnitsWatcher) Changes() <-chan []string {
	return w.out
}

func (w *MachineUnitsWatcher) updateMachine(pending []string) (new []string, err error) {
	err = w.machine.Refresh()
	if err != nil {
		return nil, err
	}
	for _, unit := range w.machine.doc.Principals {
		if _, ok := w.known[unit]; !ok {
			pending, err = w.merge(pending, unit)
			if err != nil {
				return nil, err
			}
		}
	}
	return pending, nil
}

func (w *MachineUnitsWatcher) merge(pending []string, unit string) (new []string, err error) {
	doc := unitDoc{}
	err = w.st.units.FindId(unit).One(&doc)
	if err != nil && err != mgo.ErrNotFound {
		return nil, err
	}
	life, known := w.known[unit]
	if err == mgo.ErrNotFound || doc.Principal == "" && (doc.MachineId == "" || doc.MachineId != w.machine.doc.Id) {
		// Unit was removed or unassigned from w.machine.
		if known {
			delete(w.known, unit)
			w.st.watcher.Unwatch(w.st.units.Name, unit, w.in)
			if life != Dead && !hasString(pending, unit) {
				pending = append(pending, unit)
			}
			for _, subunit := range doc.Subordinates {
				if sublife, subknown := w.known[subunit]; subknown {
					delete(w.known, subunit)
					w.st.watcher.Unwatch(w.st.units.Name, subunit, w.in)
					if sublife != Dead && !hasString(pending, subunit) {
						pending = append(pending, subunit)
					}
				}
			}
		}
		return pending, nil
	}
	if !known {
		w.st.watcher.Watch(w.st.units.Name, unit, doc.TxnRevno, w.in)
		pending = append(pending, unit)
	} else if life != doc.Life && !hasString(pending, unit) {
		pending = append(pending, unit)
	}
	w.known[unit] = doc.Life
	for _, subunit := range doc.Subordinates {
		if _, ok := w.known[subunit]; !ok {
			pending, err = w.merge(pending, subunit)
			if err != nil {
				return nil, err
			}
		}
	}
	return pending, nil
}

func (w *MachineUnitsWatcher) loop() (err error) {
	defer func() {
		for _, unit := range w.known {
			w.st.watcher.Unwatch(w.st.units.Name, unit, w.in)
		}
	}()
	machineCh := make(chan watcher.Change)
	w.st.watcher.Watch(w.st.machines.Name, w.machine.doc.Id, w.machine.doc.TxnRevno, machineCh)
	defer w.st.watcher.Unwatch(w.st.machines.Name, w.machine.doc.Id, machineCh)
	changes, err := w.updateMachine([]string(nil))
	if err != nil {
		return err
	}
	out := w.out
	for {
		select {
		case <-w.st.watcher.Dead():
			return watcher.MustErr(w.st.watcher)
		case <-w.tomb.Dying():
			return tomb.ErrDying
		case <-machineCh:
			changes, err = w.updateMachine(changes)
			if err != nil {
				return err
			}
			if len(changes) > 0 {
				out = w.out
			}
		case c := <-w.in:
			changes, err = w.merge(changes, c.Id.(string))
			if err != nil {
				return err
			}
			if len(changes) > 0 {
				out = w.out
			}
		case out <- changes:
			out = nil
			changes = nil
		}
	}
	panic("unreachable")
}
