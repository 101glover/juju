// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package maas

import (
	"encoding/base64"
	"fmt"
	"launchpad.net/gomaasapi"
	"launchpad.net/juju-core/constraints"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/cloudinit"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/environs/tools"
	"launchpad.net/juju-core/instance"
	"launchpad.net/juju-core/log"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/api"
	"launchpad.net/juju-core/utils"
	"net/url"
	"sync"
	"time"
)

const (
	jujuDataDir = "/var/lib/juju"
	// We're using v1.0 of the MAAS API.
	apiVersion = "1.0"
)

var longAttempt = utils.AttemptStrategy{
	Total: 3 * time.Minute,
	Delay: 1 * time.Second,
}

type maasEnviron struct {
	name string

	// ecfgMutex protects the *Unlocked fields below.
	ecfgMutex sync.Mutex

	ecfgUnlocked       *maasEnvironConfig
	maasClientUnlocked *gomaasapi.MAASObject
	storageUnlocked    environs.Storage
}

var _ environs.Environ = (*maasEnviron)(nil)

func NewEnviron(cfg *config.Config) (*maasEnviron, error) {
	env := new(maasEnviron)
	err := env.SetConfig(cfg)
	if err != nil {
		return nil, err
	}
	env.name = cfg.Name()
	env.storageUnlocked = NewStorage(env)
	return env, nil
}

func (env *maasEnviron) Name() string {
	return env.name
}

// makeMachineConfig sets up a basic machine configuration for use with
// userData().  You may still need to supply more information, but this takes
// care of the fixed entries and the ones that are always needed.
func (env *maasEnviron) makeMachineConfig(machineID, machineNonce string,
	stateInfo *state.Info, apiInfo *api.Info) *cloudinit.MachineConfig {
	return &cloudinit.MachineConfig{
		// Fixed entries.
		DataDir: jujuDataDir,

		// Parameter entries.
		MachineId:    machineID,
		MachineNonce: machineNonce,
		StateInfo:    stateInfo,
		APIInfo:      apiInfo,
	}
}

// startBootstrapNode starts the juju bootstrap node for this environment.
func (env *maasEnviron) startBootstrapNode(cons constraints.Value) (instance.Instance, error) {
	// The bootstrap instance gets machine id "0".  This is not related to
	// instance ids or MAAS system ids.  Juju assigns the machine ID.
	const machineID = "0"
	mcfg := env.makeMachineConfig(machineID, state.BootstrapNonce, nil, nil)
	mcfg.StateServer = true

	log.Debugf("environs/maas: bootstrapping environment %q", env.Name())
	possibleTools, err := environs.FindBootstrapTools(env, cons)
	if err != nil {
		return nil, err
	}
	inst, err := env.obtainNode(machineID, cons, possibleTools, mcfg)
	if err != nil {
		return nil, fmt.Errorf("cannot start bootstrap instance: %v", err)
	}
	return inst, nil
}

// Bootstrap is specified in the Environ interface.
func (env *maasEnviron) Bootstrap(cons constraints.Value) error {
	// TODO(fwereade): this should check for an existing environment before
	// starting a new one -- even given raciness, it's better than nothing.
	if err := environs.VerifyStorage(env.Storage()); err != nil {
		return fmt.Errorf("provider storage is not writeable")
	}

	inst, err := env.startBootstrapNode(cons)
	if err != nil {
		return err
	}
	err = env.saveState(&bootstrapState{StateInstances: []instance.Id{inst.Id()}})
	if err != nil {
		if err := env.releaseInstance(inst); err != nil {
			log.Errorf("environs/maas: cannot release failed bootstrap instance: %v", err)
		}
		return fmt.Errorf("cannot save state: %v", err)
	}

	// TODO make safe in the case of racing Bootstraps
	// If two Bootstraps are called concurrently, there's
	// no way to make sure that only one succeeds.
	return nil
}

// StateInfo is specified in the Environ interface.
// TODO: This function is duplicated between the EC2, OpenStack, MAAS, and
// Azure providers (bug 1195721).
func (env *maasEnviron) StateInfo() (*state.Info, *api.Info, error) {
	// This code is cargo-culted from the openstack/ec2 providers.
	// It's a bit unclear what the "longAttempt" loop is actually for
	// but this should probably be refactored outside of the provider
	// code.
	st, err := env.loadState()
	if err != nil {
		return nil, nil, err
	}
	config := env.Config()
	cert, hasCert := config.CACert()
	if !hasCert {
		return nil, nil, fmt.Errorf("no CA certificate in environment configuration")
	}
	var stateAddrs []string
	var apiAddrs []string
	// Wait for the DNS names of any of the instances
	// to become available.
	log.Debugf("environs/maas: waiting for DNS name(s) of state server instances %v", st.StateInstances)
	for a := longAttempt.Start(); len(stateAddrs) == 0 && a.Next(); {
		insts, err := env.Instances(st.StateInstances)
		if err != nil && err != environs.ErrPartialInstances {
			log.Debugf("environs/maas: error getting state instance: %v", err.Error())
			return nil, nil, err
		}
		log.Debugf("environs/maas: started processing instances: %#v", insts)
		for _, inst := range insts {
			if inst == nil {
				continue
			}
			name, err := inst.DNSName()
			if err != nil {
				continue
			}
			if name != "" {
				statePortSuffix := fmt.Sprintf(":%d", config.StatePort())
				apiPortSuffix := fmt.Sprintf(":%d", config.APIPort())
				stateAddrs = append(stateAddrs, name+statePortSuffix)
				apiAddrs = append(apiAddrs, name+apiPortSuffix)
			}
		}
	}
	if len(stateAddrs) == 0 {
		return nil, nil, fmt.Errorf("timed out waiting for mgo address from %v", st.StateInstances)
	}
	return &state.Info{
			Addrs:  stateAddrs,
			CACert: cert,
		}, &api.Info{
			Addrs:  apiAddrs,
			CACert: cert,
		}, nil
}

// ecfg returns the environment's maasEnvironConfig, and protects it with a
// mutex.
func (env *maasEnviron) ecfg() *maasEnvironConfig {
	env.ecfgMutex.Lock()
	defer env.ecfgMutex.Unlock()
	return env.ecfgUnlocked
}

// Config is specified in the Environ interface.
func (env *maasEnviron) Config() *config.Config {
	return env.ecfg().Config
}

// SetConfig is specified in the Environ interface.
func (env *maasEnviron) SetConfig(cfg *config.Config) error {
	env.ecfgMutex.Lock()
	defer env.ecfgMutex.Unlock()

	// The new config has already been validated by itself, but now we
	// validate the transition from the old config to the new.
	var oldCfg *config.Config
	if env.ecfgUnlocked != nil {
		oldCfg = env.ecfgUnlocked.Config
	}
	cfg, err := env.Provider().Validate(cfg, oldCfg)
	if err != nil {
		return err
	}

	ecfg, err := providerInstance.newConfig(cfg)
	if err != nil {
		return err
	}

	env.ecfgUnlocked = ecfg

	authClient, err := gomaasapi.NewAuthenticatedClient(ecfg.MAASServer(), ecfg.MAASOAuth(), apiVersion)
	if err != nil {
		return err
	}
	env.maasClientUnlocked = gomaasapi.NewMAAS(*authClient)

	return nil
}

// getMAASClient returns a MAAS client object to use for a request, in a
// lock-protected fashion.
func (env *maasEnviron) getMAASClient() *gomaasapi.MAASObject {
	env.ecfgMutex.Lock()
	defer env.ecfgMutex.Unlock()

	return env.maasClientUnlocked
}

// convertConstraints converts the given constraints into an url.Values
// object suitable to pass to MAAS when acquiring a node.
// CpuPower is ignored because it cannot translated into something
// meaningful for MAAS right now.
func convertConstraints(cons constraints.Value) url.Values {
	params := url.Values{}
	if cons.Arch != nil {
		params.Add("arch", *cons.Arch)
	}
	if cons.CpuCores != nil {
		params.Add("cpu_count", fmt.Sprintf("%d", *cons.CpuCores))
	}
	if cons.Mem != nil {
		params.Add("mem", fmt.Sprintf("%d", *cons.Mem))
	}
	if cons.CpuPower != nil {
		log.Warningf("environs/maas: ignoring unsupported constraint 'cpu-power'")
	}
	return params
}

// acquireNode allocates a node from the MAAS.
func (environ *maasEnviron) acquireNode(cons constraints.Value, possibleTools tools.List) (gomaasapi.MAASObject, *state.Tools, error) {
	retry := utils.AttemptStrategy{
		Total: 5 * time.Second,
		Delay: 200 * time.Millisecond,
	}
	constraintsParams := convertConstraints(cons)
	var result gomaasapi.JSONObject
	var err error
	for a := retry.Start(); a.Next(); {
		client := environ.getMAASClient().GetSubObject("nodes/")
		result, err = client.CallPost("acquire", constraintsParams)
		if err == nil {
			break
		}
	}
	if err != nil {
		return gomaasapi.MAASObject{}, nil, err
	}
	node, err := result.GetMAASObject()
	if err != nil {
		msg := fmt.Errorf("unexpected result from 'acquire' on MAAS API: %v", err)
		return gomaasapi.MAASObject{}, nil, msg
	}
	tools := possibleTools[0]
	log.Warningf("environs/maas: picked arbitrary tools %q", tools)
	return node, tools, nil
}

// startNode installs and boots a node.
func (environ *maasEnviron) startNode(node gomaasapi.MAASObject, series string, userdata []byte) error {
	retry := utils.AttemptStrategy{
		Total: 5 * time.Second,
		Delay: 200 * time.Millisecond,
	}
	userDataParam := base64.StdEncoding.EncodeToString(userdata)
	params := url.Values{
		"distro_series": {series},
		"user_data":     {userDataParam},
	}
	// Initialize err to a non-nil value as a sentinel for the following
	// loop.
	err := fmt.Errorf("(no error)")
	for a := retry.Start(); a.Next() && err != nil; {
		_, err = node.CallPost("start", params)
	}
	return err
}

// obtainNode allocates and starts a MAAS node.  It is used both for the
// implementation of StartInstance, and to initialize the bootstrap node.
func (environ *maasEnviron) obtainNode(machineId string, cons constraints.Value, possibleTools tools.List, mcfg *cloudinit.MachineConfig) (_ *maasInstance, err error) {
	series := possibleTools.Series()
	if len(series) != 1 {
		return nil, fmt.Errorf("expected single series, got %v", series)
	}
	var instance *maasInstance
	if node, tools, err := environ.acquireNode(cons, possibleTools); err != nil {
		return nil, fmt.Errorf("cannot run instances: %v", err)
	} else {
		instance = &maasInstance{&node, environ}
		mcfg.Tools = tools
	}
	defer func() {
		if err != nil {
			if err := environ.releaseInstance(instance); err != nil {
				log.Errorf("environs/maas: error releasing failed instance: %v", err)
			}
		}
	}()

	hostname, err := instance.DNSName()
	if err != nil {
		return nil, err
	}
	info := machineInfo{string(instance.Id()), hostname}
	runCmd, err := info.cloudinitRunCmd()
	if err != nil {
		return nil, err
	}
	if err := environs.FinishMachineConfig(mcfg, environ.Config(), cons); err != nil {
		return nil, err
	}
	userdata, err := userData(mcfg, runCmd)
	if err != nil {
		msg := fmt.Errorf("could not compose userdata for bootstrap node: %v", err)
		return nil, msg
	}
	if err := environ.startNode(*instance.maasObject, series[0], userdata); err != nil {
		return nil, err
	}
	log.Debugf("environs/maas: started instance %q", instance.Id())
	return instance, nil
}

// StartInstance is specified in the Environ interface.
func (environ *maasEnviron) StartInstance(machineID, machineNonce string, series string, cons constraints.Value,
	stateInfo *state.Info, apiInfo *api.Info) (instance.Instance, *instance.HardwareCharacteristics, error) {
	possibleTools, err := environs.FindInstanceTools(environ, series, cons)
	if err != nil {
		return nil, nil, err
	}
	mcfg := environ.makeMachineConfig(machineID, machineNonce, stateInfo, apiInfo)
	// TODO(bug 1193998) - return instance hardware characteristics as well
	inst, err := environ.obtainNode(machineID, cons, possibleTools, mcfg)
	return inst, nil, err
}

// StopInstances is specified in the Environ interface.
func (environ *maasEnviron) StopInstances(instances []instance.Instance) error {
	// Shortcut to exit quickly if 'instances' is an empty slice or nil.
	if len(instances) == 0 {
		return nil
	}
	// Tell MAAS to release each of the instances.  If there are errors,
	// return only the first one (but release all instances regardless).
	// Note that releasing instances also turns them off.
	var firstErr error
	for _, instance := range instances {
		err := environ.releaseInstance(instance)
		if firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// releaseInstance releases a single instance.
func (environ *maasEnviron) releaseInstance(inst instance.Instance) error {
	maasInst := inst.(*maasInstance)
	maasObj := maasInst.maasObject
	_, err := maasObj.CallPost("release", nil)
	if err != nil {
		log.Debugf("environs/maas: error releasing instance %v", maasInst)
	}
	return err
}

// Instances returns the instance.Instance objects corresponding to the given
// slice of instance.Id.  Similar to what the ec2 provider does,
// Instances returns nil if the given slice is empty or nil.
func (environ *maasEnviron) Instances(ids []instance.Id) ([]instance.Instance, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	return environ.instances(ids)
}

// instances is an internal method which returns the instances matching the
// given instance ids or all the instances if 'ids' is empty.
// If the some of the intances could not be found, it returns the instance
// that could be found plus the error environs.ErrPartialInstances in the error
// return.
func (environ *maasEnviron) instances(ids []instance.Id) ([]instance.Instance, error) {
	nodeListing := environ.getMAASClient().GetSubObject("nodes")
	filter := getSystemIdValues(ids)
	listNodeObjects, err := nodeListing.CallGet("list", filter)
	if err != nil {
		return nil, err
	}
	listNodes, err := listNodeObjects.GetArray()
	if err != nil {
		return nil, err
	}
	instances := make([]instance.Instance, len(listNodes))
	for index, nodeObj := range listNodes {
		node, err := nodeObj.GetMAASObject()
		if err != nil {
			return nil, err
		}
		instances[index] = &maasInstance{
			maasObject: &node,
			environ:    environ,
		}
	}
	if len(ids) != 0 && len(ids) != len(instances) {
		return instances, environs.ErrPartialInstances
	}
	return instances, nil
}

// AllInstances returns all the instance.Instance in this provider.
func (environ *maasEnviron) AllInstances() ([]instance.Instance, error) {
	return environ.instances(nil)
}

// Storage is defined by the Environ interface.
func (env *maasEnviron) Storage() environs.Storage {
	env.ecfgMutex.Lock()
	defer env.ecfgMutex.Unlock()
	return env.storageUnlocked
}

// PublicStorage is defined by the Environ interface.
func (env *maasEnviron) PublicStorage() environs.StorageReader {
	// MAAS does not have a shared storage.
	return environs.EmptyStorage
}

func (environ *maasEnviron) Destroy(ensureInsts []instance.Instance) error {
	log.Debugf("environs/maas: destroying environment %q", environ.name)
	insts, err := environ.AllInstances()
	if err != nil {
		return fmt.Errorf("cannot get instances: %v", err)
	}
	found := make(map[instance.Id]bool)
	for _, inst := range insts {
		found[inst.Id()] = true
	}

	// Add any instances we've been told about but haven't yet shown
	// up in the instance list.
	for _, inst := range ensureInsts {
		id := inst.Id()
		if !found[id] {
			insts = append(insts, inst)
			found[id] = true
		}
	}
	err = environ.StopInstances(insts)
	if err != nil {
		return err
	}

	// To properly observe e.storageUnlocked we need to get its value while
	// holding e.ecfgMutex. e.Storage() does this for us, then we convert
	// back to the (*storage) to access the private deleteAll() method.
	st := environ.Storage().(*maasStorage)
	return st.deleteAll()
}

// MAAS does not do firewalling so these port methods do nothing.
func (*maasEnviron) OpenPorts([]instance.Port) error {
	log.Debugf("environs/maas: unimplemented OpenPorts() called")
	return nil
}

func (*maasEnviron) ClosePorts([]instance.Port) error {
	log.Debugf("environs/maas: unimplemented ClosePorts() called")
	return nil
}

func (*maasEnviron) Ports() ([]instance.Port, error) {
	log.Debugf("environs/maas: unimplemented Ports() called")
	return []instance.Port{}, nil
}

func (*maasEnviron) Provider() environs.EnvironProvider {
	return &providerInstance
}
