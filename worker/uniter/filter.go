package uniter

import (
	"fmt"
	"launchpad.net/juju-core/charm"
	"launchpad.net/juju-core/log"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/watcher"
	"launchpad.net/juju-core/worker"
	"launchpad.net/tomb"
	"sort"
)

// filter collects unit, service, and service config information from separate
// state watchers, and presents it as events on channels designed specifically
// for the convenience of the uniter.
type filter struct {
	st   *state.State
	tomb tomb.Tomb

	// outUnitDying is closed when the unit's life becomes Dying.
	outUnitDying chan struct{}

	// The out*On chans are used to deliver events to clients.
	// The out* chans, when set to the corresponding out*On chan (rather than
	// nil) indicate that an event of the appropriate type is ready to send
	// to the client.
	outConfig      chan struct{}
	outConfigOn    chan struct{}
	outUpgrade     chan *charm.URL
	outUpgradeOn   chan *charm.URL
	outResolved    chan state.ResolvedMode
	outResolvedOn  chan state.ResolvedMode
	outRelations   chan []int
	outRelationsOn chan []int

	// The want* chans are used to indicate that the filter should send
	// events if it has them available.
	wantUpgrade      chan serviceCharm
	wantResolved     chan struct{}
	wantAllRelations chan struct{}

	// discardConfig is used to indicate that any pending config event
	// should be discarded.
	discardConfig chan struct{}

	// The following fields hold state that is collected while running,
	// and used to detect interesting changes to express as events.
	unit             *state.Unit
	life             state.Life
	resolved         state.ResolvedMode
	service          *state.Service
	upgradeRequested serviceCharm
	upgradeAvailable serviceCharm
	upgrade          *charm.URL
	relations        []int
}

// newFilter returns a filter that handles state changes pertaining to the
// supplied unit.
func newFilter(st *state.State, unitName string) (*filter, error) {
	f := &filter{
		st:               st,
		outUnitDying:     make(chan struct{}),
		outConfig:        make(chan struct{}),
		outConfigOn:      make(chan struct{}),
		outUpgrade:       make(chan *charm.URL),
		outUpgradeOn:     make(chan *charm.URL),
		outResolved:      make(chan state.ResolvedMode),
		outResolvedOn:    make(chan state.ResolvedMode),
		outRelations:     make(chan []int),
		outRelationsOn:   make(chan []int),
		wantResolved:     make(chan struct{}),
		wantAllRelations: make(chan struct{}),
		wantUpgrade:      make(chan serviceCharm),
		discardConfig:    make(chan struct{}),
	}
	go func() {
		defer f.tomb.Done()
		err := f.loop(unitName)
		log.Errorf("worker/uniter/filter: %v", err)
		f.tomb.Kill(err)
	}()
	return f, nil
}

func (f *filter) Stop() error {
	f.tomb.Kill(nil)
	return f.tomb.Wait()
}

func (f *filter) Dead() <-chan struct{} {
	return f.tomb.Dead()
}

func (f *filter) Wait() error {
	return f.tomb.Wait()
}

// UnitDying returns a channel which is closed when the Unit enters a Dying state.
func (f *filter) UnitDying() <-chan struct{} {
	return f.outUnitDying
}

// UpgradeEvents returns a channel that will receive a new charm URL whenever an
// upgrade is indicated. Events should not be read until the baseline state
// has been specified by calling WantUpgradeEvent.
func (f *filter) UpgradeEvents() <-chan *charm.URL {
	return f.outUpgradeOn
}

// ResolvedEvents returns a channel that may receive a ResolvedMode when the
// unit's Resolved value changes, or when an event is explicitly requested.
// A ResolvedNone state will never generate events, but ResolvedRetryHooks and
// ResolvedNoHooks will always be delivered as described.
func (f *filter) ResolvedEvents() <-chan state.ResolvedMode {
	return f.outResolvedOn
}

// ConfigEvents returns a channel that will receive a signal whenever the service's
// configuration changes, or when an event is explicitly requested.
func (f *filter) ConfigEvents() <-chan struct{} {
	return f.outConfigOn
}

// RelationsEvents returns a channel that will receive the ids of all the service's
// relations whose Life status has changed.
func (f *filter) RelationsEvents() <-chan []int {
	return f.outRelationsOn
}

// WantUpgradeEvent sets the baseline from which service charm changes will
// be considered for upgrade. Any service charm with a URL different from
// that supplied will be considered; if mustForce is true, unforced service
// charms will be ignored.
func (f *filter) WantUpgradeEvent(url *charm.URL, mustForce bool) {
	select {
	case <-f.tomb.Dying():
	case f.wantUpgrade <- serviceCharm{url, mustForce}:
	}
}

// WantAllRelationsEvents indicates that the filter should send an
// event for every known relation.
func (f *filter) WantAllRelationsEvents() {
	select {
	case <-f.tomb.Dying():
	case f.wantAllRelations <- nothing:
	}
}

// WantResolvedEvent indicates that the filter should send a resolved event
// if one is available.
func (f *filter) WantResolvedEvent() {
	select {
	case <-f.tomb.Dying():
	case f.wantResolved <- nothing:
	}
}

// DiscardConfigEvent indicates that the filter should discard any pending
// config event.
func (f *filter) DiscardConfigEvent() {
	select {
	case <-f.tomb.Dying():
	case f.discardConfig <- nothing:
	}
}

func (f *filter) loop(unitName string) (err error) {
	f.unit, err = f.st.Unit(unitName)
	if err != nil {
		return err
	}
	if err = f.unitChanged(); err != nil {
		return err
	}
	f.service, err = f.unit.Service()
	if err != nil {
		return err
	}
	f.upgradeRequested.url, _ = f.service.CharmURL()
	if err = f.serviceChanged(); err != nil {
		return err
	}
	unitw := f.unit.Watch()
	defer watcher.Stop(unitw, &f.tomb)
	servicew := f.service.Watch()
	defer watcher.Stop(servicew, &f.tomb)
	configw := f.service.WatchConfig()
	defer watcher.Stop(configw, &f.tomb)
	relationsw := f.service.WatchRelations()
	// relationsw can get restarted, so we need to bind its current value here
	defer func() { watcher.Stop(relationsw, &f.tomb) }()

	// Config events cannot be meaningfully discarded until one is available;
	// once we receive the initial change, we unblock discard requests by
	// setting this channel to its namesake on f.
	var discardConfig chan struct{}
	for {
		var ok bool
		select {
		case <-f.tomb.Dying():
			return tomb.ErrDying

		// Handle watcher changes.
		case _, ok = <-unitw.Changes():
			log.Debugf("worker/uniter/filter: got unit change")
			if !ok {
				return watcher.MustErr(unitw)
			}
			if err = f.unitChanged(); err != nil {
				return err
			}
		case _, ok = <-servicew.Changes():
			log.Debugf("worker/uniter/filter: got service change")
			if !ok {
				return watcher.MustErr(servicew)
			}
			if err = f.serviceChanged(); err != nil {
				return err
			}
		case _, ok := <-configw.Changes():
			log.Debugf("worker/uniter/filter: got config change")
			if !ok {
				return watcher.MustErr(configw)
			}
			log.Debugf("worker/uniter/filter: preparing new config event")
			f.outConfig = f.outConfigOn
			discardConfig = f.discardConfig
		case ids, ok := <-relationsw.Changes():
			log.Debugf("worker/uniter/filter: got relations change")
			if !ok {
				return watcher.MustErr(relationsw)
			}
			f.relationsChanged(ids)

		// Send events on active out chans.
		case f.outUpgrade <- f.upgrade:
			log.Debugf("worker/uniter/filter: sent upgrade event")
			f.upgradeRequested.url = f.upgrade
			f.outUpgrade = nil
		case f.outResolved <- f.resolved:
			log.Debugf("worker/uniter/filter: sent resolved event")
			f.outResolved = nil
		case f.outConfig <- nothing:
			log.Debugf("worker/uniter/filter: sent config event")
			f.outConfig = nil
		case f.outRelations <- f.relations:
			log.Debugf("worker/uniter/filter: sent relations event")
			f.outRelations = nil
			f.relations = nil

		// Handle explicit requests.
		case req := <-f.wantUpgrade:
			log.Debugf("worker/uniter/filter: want upgrade event")
			f.upgradeRequested = req
			if err = f.upgradeChanged(); err != nil {
				return err
			}
		case <-f.wantResolved:
			log.Debugf("worker/uniter/filter: want resolved event")
			if f.resolved != state.ResolvedNone {
				f.outResolved = f.outResolvedOn
			}
		case <-f.wantAllRelations:
			log.Debugf("worker/uniter/filter: want all relations events")
			// Restart the relations watcher.
			watcher.Stop(relationsw, &f.tomb)
			relationsw = f.service.WatchRelations()
		case <-discardConfig:
			log.Debugf("worker/uniter/filter: discarded config event")
			f.outConfig = nil
		}
	}
	panic("unreachable")
}

// unitChanged responds to changes in the unit.
func (f *filter) unitChanged() error {
	if err := f.unit.Refresh(); err != nil {
		if state.IsNotFound(err) {
			return worker.ErrDead
		}
		return err
	}
	if f.life != f.unit.Life() {
		switch f.life = f.unit.Life(); f.life {
		case state.Dying:
			log.Noticef("worker/uniter/filter: unit is dying")
			close(f.outUnitDying)
			f.outUpgrade = nil
		case state.Dead:
			log.Noticef("worker/uniter/filter: unit is dead")
			return worker.ErrDead
		}
	}
	if resolved := f.unit.Resolved(); resolved != f.resolved {
		f.resolved = resolved
		if f.resolved != state.ResolvedNone {
			f.outResolved = f.outResolvedOn
		}
	}
	return nil
}

// serviceChanged responds to changes in the service.
func (f *filter) serviceChanged() error {
	if err := f.service.Refresh(); err != nil {
		if state.IsNotFound(err) {
			return fmt.Errorf("service unexpectedly removed")
		}
		return err
	}
	url, force := f.service.CharmURL()
	f.upgradeAvailable = serviceCharm{url, force}
	switch f.service.Life() {
	case state.Dying:
		if err := f.unit.Destroy(); err != nil {
			return err
		}
	case state.Dead:
		return fmt.Errorf("service unexpectedly dead")
	}
	return f.upgradeChanged()
}

// upgradeChanged responds to changes in the service or in the
// upgrade requests that defines which charm changes should be
// delivered as upgrades.
func (f *filter) upgradeChanged() (err error) {
	if f.life != state.Alive {
		log.Debugf("worker/uniter/filter: charm check skipped, unit is dying")
		f.outUpgrade = nil
		return nil
	}
	if *f.upgradeAvailable.url != *f.upgradeRequested.url {
		if f.upgradeAvailable.force || !f.upgradeRequested.force {
			log.Debugf("worker/uniter/filter: preparing new upgrade event")
			if f.upgrade == nil || *f.upgrade != *f.upgradeAvailable.url {
				f.upgrade = f.upgradeAvailable.url
			}
			f.outUpgrade = f.outUpgradeOn
			return nil
		}
	}
	log.Debugf("worker/uniter/filter: no new charm event")
	return nil
}

// relationsChanged responds to service relation changes.
func (f *filter) relationsChanged(ids []int) {
outer:
	for _, id := range ids {
		for _, existing := range f.relations {
			if id == existing {
				continue outer
			}
		}
		f.relations = append(f.relations, id)
	}
	if len(f.relations) != 0 {
		sort.Ints(f.relations)
		f.outRelations = f.outRelationsOn
	}
}

// serviceCharm holds information about a charm.
type serviceCharm struct {
	url   *charm.URL
	force bool
}

// nothing is marginally more pleasant to read than "struct{}{}".
var nothing = struct{}{}
