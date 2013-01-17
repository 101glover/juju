package provisioner

import (
	"fmt"
	"sync"

	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/log"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/watcher"
	"launchpad.net/juju-core/trivial"
	"launchpad.net/juju-core/worker"
	"launchpad.net/tomb"
)

// Provisioner represents a running provisioning worker.
type Provisioner struct {
	st      *state.State
	info    *state.Info
	environ environs.Environ
	tomb    tomb.Tomb

	// machine.Id => environs.Instance
	instances map[string]environs.Instance
	// instance.Id => machine id
	machines map[state.InstanceId]string

	configObserver
}

type configObserver struct {
	sync.Mutex
	observer chan<- *config.Config
}

// nofity notifies the observer of a configuration change.
func (o *configObserver) notify(cfg *config.Config) {
	o.Lock()
	if o.observer != nil {
		o.observer <- cfg
	}
	o.Unlock()
}

// NewProvisioner returns a new Provisioner. When new machines
// are added to the state, it allocates instances from the environment
// and allocates them to the new machines.
func NewProvisioner(st *state.State) *Provisioner {
	p := &Provisioner{
		st:        st,
		instances: make(map[string]environs.Instance),
		machines:  make(map[state.InstanceId]string),
	}
	go func() {
		defer p.tomb.Done()
		p.tomb.Kill(p.loop())
	}()
	return p
}

func (p *Provisioner) loop() error {
	environWatcher := p.st.WatchEnvironConfig()
	defer watcher.Stop(environWatcher, &p.tomb)

	var err error
	p.environ, err = worker.WaitForEnviron(environWatcher, p.tomb.Dying())
	if err != nil {
		return err
	}

	// Get a new StateInfo from the environment: the one used to
	// launch the agent may refer to localhost, which will be
	// unhelpful when attempting to run an agent on a new machine.
	if p.info, err = p.environ.StateInfo(); err != nil {
		return err
	}

	// Call processMachines to stop any unknown instances before watching machines.
	if err := p.processMachines(nil); err != nil {
		return err
	}

	// Start responding to changes in machines, and to any further updates
	// to the environment config.
	machinesWatcher := p.st.WatchMachines()
	defer watcher.Stop(machinesWatcher, &p.tomb)
	for {
		select {
		case <-p.tomb.Dying():
			return tomb.ErrDying
		case cfg, ok := <-environWatcher.Changes():
			if !ok {
				return watcher.MustErr(environWatcher)
			}
			if err := p.setConfig(cfg); err != nil {
				log.Printf("worker/provisioner: loaded invalid environment configuration: %v", err)
			}
		case ids, ok := <-machinesWatcher.Changes():
			if !ok {
				return watcher.MustErr(machinesWatcher)
			}
			// TODO(dfc) fire process machines periodically to shut down unknown
			// instances.
			if err := p.processMachines(ids); err != nil {
				return err
			}
		}
	}
	panic("not reached")
}

// setConfig updates the environment configuration and notifies
// the config observer.
func (p *Provisioner) setConfig(config *config.Config) error {
	if err := p.environ.SetConfig(config); err != nil {
		return err
	}
	p.configObserver.notify(config)
	return nil
}

// Err returns the reason why the Provisioner has stopped or tomb.ErrStillAlive
// when it is still alive.
func (p *Provisioner) Err() (reason error) {
	return p.tomb.Err()
}

// Wait waits for the Provisioner to exit.
func (p *Provisioner) Wait() error {
	return p.tomb.Wait()
}

func (p *Provisioner) String() string {
	return "provisioning worker"
}

// Stop stops the Provisioner and returns any error encountered while
// provisioning.
func (p *Provisioner) Stop() error {
	p.tomb.Kill(nil)
	return p.tomb.Wait()
}

func (p *Provisioner) processMachines(ids []string) error {
	// Find machines without an instance id or that are dead
	pending, dead, err := p.pendingOrDead(ids)
	if err != nil {
		return err
	}

	// Start an instance for the pending ones
	if err := p.startMachines(pending); err != nil {
		return err
	}

	// Stop all machines that are dead
	stopping, err := p.instancesForMachines(dead)
	if err != nil {
		return err
	}

	// Find running instances that have no machines associated
	unknown, err := p.findUnknownInstances()
	if err != nil {
		return err
	}

	return p.stopInstances(append(stopping, unknown...))
}

// findUnknownInstances finds instances which are not associated with a machine.
func (p *Provisioner) findUnknownInstances() ([]environs.Instance, error) {
	all, err := p.environ.AllInstances()
	if err != nil {
		return nil, err
	}
	instances := make(map[state.InstanceId]environs.Instance)
	for _, i := range all {
		instances[i.Id()] = i
	}
	// TODO(dfc) this is very inefficient.
	machines, err := p.st.AllMachines()
	if err != nil {
		return nil, err
	}
	for _, m := range machines {
		if m.Life() == state.Dead {
			continue
		}
		instId, err := m.InstanceId()
		if state.IsNotFound(err) {
			continue
		}
		if err != nil {
			return nil, err
		}
		delete(instances, instId)
	}
	var unknown []environs.Instance
	for _, i := range instances {
		unknown = append(unknown, i)
	}
	return unknown, nil
}

// pendingOrDead looks up machines with ids and retuns those that do not
// have an instance id assigned yet, and also those that are dead.
func (p *Provisioner) pendingOrDead(ids []string) (pending, dead []*state.Machine, err error) {
	// TODO(niemeyer): ms, err := st.Machines(alive)
	for _, id := range ids {
		m, err := p.st.Machine(id)
		if state.IsNotFound(err) {
			continue
		}
		if err != nil {
			return nil, nil, err
		}
		switch m.Life() {
		case state.Dead:
			dead = append(dead, m)
			continue
		case state.Dying:
			continue
		}
		instId, err := m.InstanceId()
		if state.IsNotFound(err) {
			pending = append(pending, m)
			continue
		}
		if err != nil {
			return nil, nil, err
		}
		log.Printf("worker/provisioner: machine %v already started as instance %q", m, instId)
	}
	return
}

func (p *Provisioner) startMachines(machines []*state.Machine) error {
	for _, m := range machines {
		if err := p.startMachine(m); err != nil {
			return err
		}
	}
	return nil
}

func (p *Provisioner) startMachine(m *state.Machine) error {
	// TODO(dfc) the state.Info passed to environ.StartInstance remains contentious
	// however as the PA only knows one state.Info, and that info is used by MAs and
	password, err := trivial.RandomPassword()
	if err != nil {
		return fmt.Errorf("cannot make password for new machine: %v", err)
	}
	if err := m.SetMongoPassword(password); err != nil {
		return fmt.Errorf("cannot set password for new machine: %v", err)
	}
	// UAs to locate the ZK for this environment, it is logical to use the same
	// state.Info as the PA.
	info := *p.info
	info.EntityName = m.EntityName()
	info.Password = password
	inst, err := p.environ.StartInstance(m.Id(), &info, nil)
	if err != nil {
		return fmt.Errorf("cannot start instance for new machine: %v", err)
	}
	// assign the instance id to the machine
	if err := m.SetInstanceId(inst.Id()); err != nil {
		return err
	}

	// populate the local cache
	p.instances[m.Id()] = inst
	p.machines[inst.Id()] = m.Id()
	log.Printf("worker/provisioner: started machine %s as instance %s", m, inst.Id())
	return nil
}

func (p *Provisioner) stopInstances(instances []environs.Instance) error {
	// Although calling StopInstance with an empty slice should produce no change in the
	// provider, environs like dummy do not consider this a noop.
	if len(instances) == 0 {
		return nil
	}
	if err := p.environ.StopInstances(instances); err != nil {
		return err
	}

	// cleanup cache
	for _, i := range instances {
		if id, ok := p.machines[i.Id()]; ok {
			delete(p.machines, i.Id())
			delete(p.instances, id)
		}
	}
	return nil
}

// instanceForMachine returns the environs.Instance that represents this machine's instance.
func (p *Provisioner) instanceForMachine(m *state.Machine) (environs.Instance, error) {
	inst, ok := p.instances[m.Id()]
	if ok {
		return inst, nil
	}
	instId, err := m.InstanceId()
	if state.IsNotFound(err) {
		panic("cannot have unset instance ids here")
	}
	if err != nil {
		return nil, err
	}
	// TODO(dfc): Ask for all instances at once.
	insts, err := p.environ.Instances([]state.InstanceId{instId})
	if err != nil {
		return nil, err
	}
	inst = insts[0]
	return inst, nil
}

// instancesForMachines returns a list of environs.Instance that represent
// the list of machines running in the provider. Missing machines are
// omitted from the list.
func (p *Provisioner) instancesForMachines(ms []*state.Machine) ([]environs.Instance, error) {
	var insts []environs.Instance
	for _, m := range ms {
		inst, err := p.instanceForMachine(m)
		if err == environs.ErrNoInstances {
			continue
		}
		if err != nil {
			return nil, err
		}
		insts = append(insts, inst)
	}
	return insts, nil
}
