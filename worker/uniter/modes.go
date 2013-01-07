package uniter

import (
	"errors"
	"fmt"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/log"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/watcher"
	"launchpad.net/juju-core/worker"
	"launchpad.net/juju-core/worker/uniter/charm"
	"launchpad.net/juju-core/worker/uniter/hook"
	"launchpad.net/tomb"
)

// Mode defines the signature of the functions that implement the possible
// states of a running Uniter.
type Mode func(u *Uniter) (Mode, error)

// ModeInit is the initial Uniter mode.
func ModeInit(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeInit", &err)()
	log.Printf("worker/uniter: updating unit addresses")
	cfg, err := u.st.EnvironConfig()
	if err != nil {
		return nil, err
	}
	provider, err := environs.Provider(cfg.Type())
	if err != nil {
		return nil, err
	}
	if private, err := provider.PrivateAddress(); err != nil {
		return nil, err
	} else if err = u.unit.SetPrivateAddress(private); err != nil {
		return nil, err
	}
	if public, err := provider.PublicAddress(); err != nil {
		return nil, err
	} else if err = u.unit.SetPublicAddress(public); err != nil {
		return nil, err
	}
	log.Printf("reconciling relation state")
	if err := u.restoreRelations(); err != nil {
		return nil, err
	}
	return ModeContinue, nil
}

// ModeContinue determines what action to take based on persistent uniter state.
func ModeContinue(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeContinue", &err)()

	// If we haven't yet loaded state, do so.
	if u.s == nil {
		log.Printf("loading uniter state")
		if u.s, err = u.sf.Read(); err == ErrNoStateFile {
			// When no state exists, start from scratch.
			log.Printf("worker/uniter: charm is not deployed")
			sch, _, err := u.service.Charm()
			if err != nil {
				return nil, err
			}
			return ModeInstalling(sch), nil
		} else if err != nil {
			return nil, err
		}
	}

	// Filter out states not related to charm deployment.
	switch u.s.Op {
	case Continue:
		log.Printf("worker/uniter: continuing after %q hook", u.s.Hook.Kind)
		switch u.s.Hook.Kind {
		case hook.Stop:
			return ModeTerminating, nil
		case hook.UpgradeCharm:
			return ModeConfigChanged, nil
		case hook.ConfigChanged:
			if !u.s.Started {
				return ModeStarting, nil
			}
		}
		if !u.ranConfigChanged {
			return ModeConfigChanged, nil
		}
		return ModeAbide, nil
	case RunHook:
		if u.s.OpStep == Queued {
			log.Printf("worker/uniter: found queued %q hook", u.s.Hook.Kind)
			if err = u.runHook(*u.s.Hook); err != nil && err != errHookFailed {
				return nil, err
			}
			return ModeContinue, nil
		}
		if u.s.OpStep == Done {
			log.Printf("worker/uniter: found uncommitted %q hook", u.s.Hook.Kind)
			if err = u.commitHook(*u.s.Hook); err != nil {
				return nil, err
			}
			return ModeContinue, nil
		}
		log.Printf("worker/uniter: awaiting error resolution for %q hook", u.s.Hook.Kind)
		return ModeHookError, nil
	}

	// Resume interrupted deployment operations.
	sch, err := u.st.Charm(u.s.CharmURL)
	if err != nil {
		return nil, err
	}
	if u.s.Op == Install {
		log.Printf("worker/uniter: resuming charm install")
		return ModeInstalling(sch), nil
	} else if u.s.Op == Upgrade {
		log.Printf("worker/uniter: resuming charm upgrade")
		return ModeUpgrading(sch), nil
	}
	panic(fmt.Errorf("unhandled uniter operation %q", u.s.Op))
}

// ModeInstalling is responsible for the initial charm deployment.
func ModeInstalling(sch *state.Charm) Mode {
	name := fmt.Sprintf("ModeInstalling %s", sch.URL())
	return func(u *Uniter) (next Mode, err error) {
		defer modeContext(name, &err)()
		if err = u.deploy(sch, Install); err != nil {
			return nil, err
		}
		return ModeContinue, nil
	}
}

// ModeUpgrading is responsible for upgrading the charm.
func ModeUpgrading(sch *state.Charm) Mode {
	name := fmt.Sprintf("ModeUpgrading %s", sch.URL())
	return func(u *Uniter) (next Mode, err error) {
		defer modeContext(name, &err)()
		if err = u.deploy(sch, Upgrade); err == charm.ErrConflict {
			return ModeConflicted(sch), nil
		} else if err != nil {
			return nil, err
		}
		return ModeContinue, nil
	}
}

// ModeConfigChanged runs the "config-changed" hook.
func ModeConfigChanged(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeConfigChanged", &err)()
	if !u.s.Started {
		if err = u.unit.SetStatus(state.UnitInstalled, ""); err != nil {
			return nil, err
		}
	}
	u.f.DiscardConfigEvent()
	if err := u.runHook(hook.Info{Kind: hook.ConfigChanged}); err == errHookFailed {
		return ModeHookError, nil
	} else if err != nil {
		return nil, err
	}
	return ModeContinue, nil
}

// ModeStarting runs the "start" hook.
func ModeStarting(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeStarting", &err)()
	if err := u.runHook(hook.Info{Kind: hook.Start}); err == errHookFailed {
		return ModeHookError, nil
	} else if err != nil {
		return nil, err
	}
	return ModeContinue, nil
}

// ModeStopping runs the "stop" hook.
func ModeStopping(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeStopping", &err)()
	if err := u.runHook(hook.Info{Kind: hook.Stop}); err == errHookFailed {
		return ModeHookError, nil
	} else if err != nil {
		return nil, err
	}
	return ModeContinue, nil
}

// ModeTerminating marks the unit dead and returns ErrDead.
func ModeTerminating(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeTerminating", &err)()
	if err = u.unit.SetStatus(state.UnitStopped, ""); err != nil {
		return nil, err
	}
	w := u.unit.Watch()
	defer watcher.Stop(w, &u.tomb)
	for {
		select {
		case <-u.tomb.Dying():
			return nil, tomb.ErrDying
		case _, ok := <-w.Changes():
			if !ok {
				return nil, watcher.MustErr(w)
			}
			if err := u.unit.Refresh(); err != nil {
				return nil, err
			}
			if u.unit.HasSubordinates() {
				continue
			}
			if err := u.unit.EnsureDead(); err == state.ErrUnitHasSubordinates {
				continue
			} else if err != nil {
				return nil, err
			}
			return nil, worker.ErrDead
		}
	}
	panic("unreachable")
}

// ModeAbide is the Uniter's usual steady state. It watches for and responds to:
// * service configuration changes
// * charm upgrade requests
// * relation changes
// * unit death
func ModeAbide(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeAbide", &err)()
	if u.s.Op != Continue {
		return nil, fmt.Errorf("insane uniter state: %#v", u.s)
	}
	if err = u.unit.SetStatus(state.UnitStarted, ""); err != nil {
		return nil, err
	}
	url, err := charm.ReadCharmURL(u.charm)
	if err != nil {
		return nil, err
	}
	u.f.WantUpgradeEvent(url, false)
	for _, r := range u.relationers {
		r.StartHooks()
	}
	defer func() {
		for _, r := range u.relationers {
			if e := r.StopHooks(); e != nil && err == nil {
				err = e
			}
		}
	}()
	select {
	case <-u.f.UnitDying():
		return modeAbideDyingLoop(u)
	default:
	}
	return modeAbideAliveLoop(u)
}

// modeAbideAliveLoop handles all state changes for ModeAbide when the unit
// is in an Alive state.
func modeAbideAliveLoop(u *Uniter) (Mode, error) {
	for {
		hi := hook.Info{}
		select {
		case <-u.Dying():
			return nil, tomb.ErrDying
		case <-u.f.UnitDying():
			return modeAbideDyingLoop(u)
		case <-u.f.ConfigEvents():
			hi = hook.Info{Kind: hook.ConfigChanged}
		case hi = <-u.relationHooks:
		case ids := <-u.f.RelationsEvents():
			added, err := u.updateRelations(ids)
			if err != nil {
				return nil, err
			}
			for _, r := range added {
				r.StartHooks()
			}
			continue
		case upgrade := <-u.f.UpgradeEvents():
			return ModeUpgrading(upgrade), nil
		}
		if err := u.runHook(hi); err == errHookFailed {
			return ModeHookError, nil
		} else if err != nil {
			return nil, err
		}
	}
	panic("unreachable")
}

// modeAbideDyingLoop handles the proper termination of all relations in
// response to a Dying unit.
func modeAbideDyingLoop(u *Uniter) (next Mode, err error) {
	for _, r := range u.relationers {
		if err := r.SetDying(); err != nil {
			return nil, err
		}
	}
	for {
		if len(u.relationers) == 0 {
			return ModeStopping, nil
		}
		hi := hook.Info{}
		select {
		case <-u.Dying():
			return nil, tomb.ErrDying
		case <-u.f.ConfigEvents():
			hi = hook.Info{Kind: hook.ConfigChanged}
		case hi = <-u.relationHooks:
		}
		if err = u.runHook(hi); err == errHookFailed {
			return ModeHookError, nil
		} else if err != nil {
			return nil, err
		}
	}
	panic("unreachable")
}

// ModeHookError is responsible for watching and responding to:
// * user resolution of hook errors
// * charm upgrade requests
func ModeHookError(u *Uniter) (next Mode, err error) {
	defer modeContext("ModeHookError", &err)()
	if u.s.Op != RunHook || u.s.OpStep != Pending {
		return nil, fmt.Errorf("insane uniter state: %#v", u.s)
	}
	msg := fmt.Sprintf("hook failed: %q", u.s.Hook.Kind)
	if err = u.unit.SetStatus(state.UnitError, msg); err != nil {
		return nil, err
	}
	url, err := charm.ReadCharmURL(u.charm)
	if err != nil {
		return nil, err
	}
	u.f.WantResolvedEvent()
	u.f.WantUpgradeEvent(url, true)
	for {
		select {
		case <-u.Dying():
			return nil, tomb.ErrDying
		case rm := <-u.f.ResolvedEvents():
			switch rm {
			case state.ResolvedRetryHooks:
				err = u.runHook(*u.s.Hook)
			case state.ResolvedNoHooks:
				err = u.commitHook(*u.s.Hook)
			default:
				return nil, fmt.Errorf("unknown resolved mode %q", rm)
			}
			if e := u.unit.ClearResolved(); e != nil {
				return nil, e
			}
			if err == errHookFailed {
				continue
			} else if err != nil {
				return nil, err
			}
			return ModeContinue, nil
		case upgrade := <-u.f.UpgradeEvents():
			return ModeUpgrading(upgrade), nil
		}
	}
	panic("unreachable")
}

// ModeConflicted is responsible for watching and responding to:
// * user resolution of charm upgrade conflicts
// * forced charm upgrade requests
func ModeConflicted(sch *state.Charm) Mode {
	return func(u *Uniter) (next Mode, err error) {
		defer modeContext("ModeConflicted", &err)()
		if err = u.unit.SetStatus(state.UnitError, "upgrade failed"); err != nil {
			return nil, err
		}
		u.f.WantResolvedEvent()
		u.f.WantUpgradeEvent(sch.URL(), true)
		for {
			select {
			case <-u.Dying():
				return nil, tomb.ErrDying
			case <-u.f.ResolvedEvents():
				err = u.charm.Snapshotf("Upgrade conflict resolved.")
				if e := u.unit.ClearResolved(); e != nil {
					return nil, e
				}
				if err != nil {
					return nil, err
				}
				return ModeUpgrading(sch), nil
			case upgrade := <-u.f.UpgradeEvents():
				if err := u.charm.Revert(); err != nil {
					return nil, err
				}
				return ModeUpgrading(upgrade), nil
			}
		}
		panic("unreachable")
	}
}

// modeContext returns a function that implements logging and common error
// manipulation for Mode funcs.
func modeContext(name string, err *error) func() {
	log.Printf("worker/uniter: %s starting", name)
	return func() {
		log.Debugf("worker/uniter: %s exiting", name)
		switch *err {
		case nil, tomb.ErrDying, worker.ErrDead:
		default:
			*err = errors.New(name + ": " + (*err).Error())
		}
	}
}
