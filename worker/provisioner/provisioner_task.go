// Copyright 2012, 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package provisioner

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/juju/errors"
	"github.com/juju/utils"
	"github.com/juju/utils/set"
	"github.com/juju/version"
	"gopkg.in/juju/names.v2"
	worker "gopkg.in/juju/worker.v1"

	apiprovisioner "github.com/juju/juju/api/provisioner"
	"github.com/juju/juju/apiserver/common/networkingcommon"
	"github.com/juju/juju/apiserver/params"
	"github.com/juju/juju/cloudconfig/instancecfg"
	"github.com/juju/juju/constraints"
	"github.com/juju/juju/controller"
	"github.com/juju/juju/controller/authentication"
	"github.com/juju/juju/environs"
	"github.com/juju/juju/environs/config"
	"github.com/juju/juju/environs/imagemetadata"
	"github.com/juju/juju/environs/simplestreams"
	"github.com/juju/juju/instance"
	"github.com/juju/juju/network"
	providercommon "github.com/juju/juju/provider/common"
	"github.com/juju/juju/state"
	"github.com/juju/juju/state/multiwatcher"
	"github.com/juju/juju/status"
	"github.com/juju/juju/storage"
	coretools "github.com/juju/juju/tools"
	"github.com/juju/juju/watcher"
	"github.com/juju/juju/worker/catacomb"
	"github.com/juju/juju/wrench"
)

type ProvisionerTask interface {
	worker.Worker

	// SetHarvestMode sets a flag to indicate how the provisioner task
	// should harvest machines. See config.HarvestMode for
	// documentation of behavior.
	SetHarvestMode(mode config.HarvestMode)
}

type ProvisionerGetter interface {
	DistributionGroupByMachineId(...names.MachineTag) ([]apiprovisioner.DistributionGroupResult, error)
	Machines(...names.MachineTag) ([]apiprovisioner.MachineResult, error)
	MachinesWithTransientErrors() ([]apiprovisioner.MachineStatusResult, error)
}

// ToolsFinder is an interface used for finding tools to run on
// provisioned instances.
type ToolsFinder interface {
	// FindTools returns a list of tools matching the specified
	// version, series, and architecture. If arch is empty, the
	// implementation is expected to use a well documented default.
	FindTools(version version.Number, series string, arch string) (coretools.List, error)
}

func NewProvisionerTask(
	controllerUUID string,
	machineTag names.MachineTag,
	harvestMode config.HarvestMode,
	provisionerGetter ProvisionerGetter,
	toolsFinder ToolsFinder,
	machineWatcher watcher.StringsWatcher,
	retryWatcher watcher.NotifyWatcher,
	broker environs.InstanceBroker,
	auth authentication.AuthenticationProvider,
	imageStream string,
	retryStartInstanceStrategy RetryStrategy,
) (ProvisionerTask, error) {
	machineChanges := machineWatcher.Changes()
	workers := []worker.Worker{machineWatcher}
	var retryChanges watcher.NotifyChannel
	if retryWatcher != nil {
		retryChanges = retryWatcher.Changes()
		workers = append(workers, retryWatcher)
	}
	task := &provisionerTask{
		controllerUUID:             controllerUUID,
		machineTag:                 machineTag,
		provisionerGetter:          provisionerGetter,
		toolsFinder:                toolsFinder,
		machineChanges:             machineChanges,
		retryChanges:               retryChanges,
		broker:                     broker,
		auth:                       auth,
		harvestMode:                harvestMode,
		harvestModeChan:            make(chan config.HarvestMode, 1),
		machines:                   make(map[string]*apiprovisioner.Machine),
		availabilityZoneMachines:   make([]*availabilityZoneMachine, 0),
		imageStream:                imageStream,
		retryStartInstanceStrategy: retryStartInstanceStrategy,
	}
	err := catacomb.Invoke(catacomb.Plan{
		Site: &task.catacomb,
		Work: task.loop,
		Init: workers,
	})
	if err != nil {
		return nil, errors.Trace(err)
	}
	return task, nil
}

type provisionerTask struct {
	controllerUUID             string
	machineTag                 names.MachineTag
	provisionerGetter          ProvisionerGetter
	toolsFinder                ToolsFinder
	machineChanges             watcher.StringsChannel
	retryChanges               watcher.NotifyChannel
	broker                     environs.InstanceBroker
	catacomb                   catacomb.Catacomb
	auth                       authentication.AuthenticationProvider
	imageStream                string
	harvestMode                config.HarvestMode
	harvestModeChan            chan config.HarvestMode
	retryStartInstanceStrategy RetryStrategy
	// instance id -> instance
	instances map[instance.Id]instance.Instance
	// machine id -> machine
	machines                 map[string]*apiprovisioner.Machine
	availabilityZoneMachines []*availabilityZoneMachine
	azMachinesMutex          sync.RWMutex
}

// Kill implements worker.Worker.Kill.
func (task *provisionerTask) Kill() {
	task.catacomb.Kill(nil)
}

// Wait implements worker.Worker.Wait.
func (task *provisionerTask) Wait() error {
	return task.catacomb.Wait()
}

func (task *provisionerTask) loop() error {

	// Don't allow the harvesting mode to change until we have read at
	// least one set of changes, which will populate the task.machines
	// map. Otherwise we will potentially see all legitimate instances
	// as unknown.
	var harvestModeChan chan config.HarvestMode

	// When the watcher is started, it will have the initial changes be all
	// the machines that are relevant. Also, since this is available straight
	// away, we know there will be some changes right off the bat.
	for {
		select {
		case <-task.catacomb.Dying():
			logger.Infof("Shutting down provisioner task %s", task.machineTag)
			return task.catacomb.ErrDying()
		case ids, ok := <-task.machineChanges:
			if !ok {
				return errors.New("machine watcher closed channel")
			}
			if err := task.processMachines(ids); err != nil {
				return errors.Annotate(err, "failed to process updated machines")
			}
			// We've seen a set of changes. Enable modification of
			// harvesting mode.
			harvestModeChan = task.harvestModeChan
		case harvestMode := <-harvestModeChan:
			if harvestMode == task.harvestMode {
				break
			}
			logger.Infof("harvesting mode changed to %s", harvestMode)
			task.harvestMode = harvestMode
			if harvestMode.HarvestUnknown() {
				logger.Infof("harvesting unknown machines")
				if err := task.processMachines(nil); err != nil {
					return errors.Annotate(err, "failed to process machines after safe mode disabled")
				}
			}
		case <-task.retryChanges:
			if err := task.processMachinesWithTransientErrors(); err != nil {
				return errors.Annotate(err, "failed to process machines with transient errors")
			}
		}
	}
}

// SetHarvestMode implements ProvisionerTask.SetHarvestMode().
func (task *provisionerTask) SetHarvestMode(mode config.HarvestMode) {
	select {
	case task.harvestModeChan <- mode:
	case <-task.catacomb.Dying():
	}
}

func (task *provisionerTask) processMachinesWithTransientErrors() error {
	results, err := task.provisionerGetter.MachinesWithTransientErrors()
	if err != nil {
		return nil
	}
	logger.Tracef("processMachinesWithTransientErrors(%v)", results)
	var pending []*apiprovisioner.Machine
	for _, result := range results {
		if result.Status.Error != nil {
			logger.Errorf("cannot retry provisioning of machine %q: %v", result.Machine.Id(), result.Status.Error)
			continue
		}
		machine := result.Machine
		if err := machine.SetStatus(status.Pending, "", nil); err != nil {
			logger.Errorf("cannot reset status of machine %q: %v", machine.Id(), err)
			continue
		}
		if err := machine.SetInstanceStatus(status.Provisioning, "", nil); err != nil {
			logger.Errorf("cannot reset instance status of machine %q: %v", machine.Id(), err)
			continue
		}
		task.machines[machine.Tag().String()] = machine
		pending = append(pending, machine)
	}
	return task.startMachines(pending)
}

func (task *provisionerTask) processMachines(ids []string) error {
	logger.Tracef("processMachines(%v)", ids)

	// Populate the tasks maps of current instances and machines.
	if err := task.populateMachineMaps(ids); err != nil {
		return err
	}

	// Find machines without an instance id or that are dead
	pending, dead, maintain, err := task.pendingOrDeadOrMaintain(ids)
	if err != nil {
		return err
	}

	// Stop all machines that are dead
	stopping := task.instancesForDeadMachines(dead)

	// Find running instances that have no machines associated
	unknown, err := task.findUnknownInstances(stopping)
	if err != nil {
		return err
	}
	if !task.harvestMode.HarvestUnknown() {
		logger.Infof(
			"%s is set to %s; unknown instances not stopped %v",
			config.ProvisionerHarvestModeKey,
			task.harvestMode.String(),
			instanceIds(unknown),
		)
		unknown = nil
	}
	if task.harvestMode.HarvestNone() || !task.harvestMode.HarvestDestroyed() {
		logger.Infof(
			`%s is set to "%s"; will not harvest %s`,
			config.ProvisionerHarvestModeKey,
			task.harvestMode.String(),
			instanceIds(stopping),
		)
		stopping = nil
	}

	if len(stopping) > 0 {
		logger.Infof("stopping known instances %v", stopping)
	}
	if len(unknown) > 0 {
		logger.Infof("stopping unknown instances %v", instanceIds(unknown))
	}
	// It's important that we stop unknown instances before starting
	// pending ones, because if we start an instance and then fail to
	// set its InstanceId on the machine we don't want to start a new
	// instance for the same machine ID.
	if err := task.stopInstances(append(stopping, unknown...)); err != nil {
		return err
	}

	// Remove any dead machines from state.
	for _, machine := range dead {
		logger.Infof("removing dead machine %q", machine)
		if err := machine.MarkForRemoval(); err != nil {
			logger.Errorf("failed to remove dead machine %q", machine)
		}
		delete(task.machines, machine.Id())
	}

	// Any machines that require maintenance get pinged
	task.maintainMachines(maintain)

	// Start an instance for the pending ones
	return task.startMachines(pending)
}

func instanceIds(instances []instance.Instance) []string {
	ids := make([]string, 0, len(instances))
	for _, inst := range instances {
		ids = append(ids, string(inst.Id()))
	}
	return ids
}

// populateMachineMaps updates task.instances. Also updates
// task.machines map if a list of IDs is given.
func (task *provisionerTask) populateMachineMaps(ids []string) error {
	task.instances = make(map[instance.Id]instance.Instance)

	instances, err := task.broker.AllInstances()
	if err != nil {
		return errors.Annotate(err, "failed to get all instances from broker")
	}
	for _, i := range instances {
		task.instances[i.Id()] = i
	}

	// Update the machines map with new data for each of the machines in the
	// change list.
	machineTags := make([]names.MachineTag, len(ids))
	for i, id := range ids {
		machineTags[i] = names.NewMachineTag(id)
	}
	machines, err := task.provisionerGetter.Machines(machineTags...)
	if err != nil {
		return errors.Annotatef(err, "failed to get machines %v", ids)
	}
	for i, machine := range machines {
		switch {
		case err == nil:
			task.machines[machine.Machine.Id()] = machine.Machine
		case params.IsCodeNotFoundOrCodeUnauthorized(machine.Err):
			logger.Debugf("machine %q not found in state", ids[i])
			delete(task.machines, ids[i])
		default:
			return errors.Annotatef(machine.Err, "failed to get machine %v", ids[i])
		}
	}
	return nil
}

// pendingOrDead looks up machines with ids and returns those that do not
// have an instance id assigned yet, and also those that are dead.
func (task *provisionerTask) pendingOrDeadOrMaintain(ids []string) (pending, dead, maintain []*apiprovisioner.Machine, err error) {
	for _, id := range ids {
		machine, found := task.machines[id]
		if !found {
			logger.Infof("machine %q not found", id)
			continue
		}
		var classification MachineClassification
		classification, err = classifyMachine(machine)
		if err != nil {
			return // return the error
		}
		switch classification {
		case Pending:
			pending = append(pending, machine)
		case Dead:
			dead = append(dead, machine)
		case Maintain:
			maintain = append(maintain, machine)
		}
	}
	logger.Tracef("pending machines: %v", pending)
	logger.Tracef("dead machines: %v", dead)
	return
}

type ClassifiableMachine interface {
	Life() params.Life
	InstanceId() (instance.Id, error)
	EnsureDead() error
	Status() (status.Status, string, error)
	InstanceStatus() (status.Status, string, error)
	Id() string
}

type MachineClassification string

const (
	None     MachineClassification = "none"
	Pending  MachineClassification = "Pending"
	Dead     MachineClassification = "Dead"
	Maintain MachineClassification = "Maintain"
)

func classifyMachine(machine ClassifiableMachine) (
	MachineClassification, error) {
	switch machine.Life() {
	case params.Dying:
		if _, err := machine.InstanceId(); err == nil {
			return None, nil
		} else if !params.IsCodeNotProvisioned(err) {
			return None, errors.Annotatef(err, "failed to load dying machine id:%s, details:%v", machine.Id(), machine)
		}
		logger.Infof("killing dying, unprovisioned machine %q", machine)
		if err := machine.EnsureDead(); err != nil {
			return None, errors.Annotatef(err, "failed to ensure machine dead id:%s, details:%v", machine.Id(), machine)
		}
		fallthrough
	case params.Dead:
		return Dead, nil
	}
	instId, err := machine.InstanceId()
	if err != nil {
		if !params.IsCodeNotProvisioned(err) {
			return None, errors.Annotatef(err, "failed to load machine id:%s, details:%v", machine.Id(), machine)
		}
		machineStatus, _, err := machine.Status()
		if err != nil {
			logger.Infof("cannot get machine id:%s, details:%v, err:%v", machine.Id(), machine, err)
			return None, nil
		}
		if machineStatus == status.Pending {
			logger.Infof("found machine pending provisioning id:%s, details:%v", machine.Id(), machine)
			return Pending, nil
		}
		instanceStatus, _, err := machine.InstanceStatus()
		if err != nil {
			logger.Infof("cannot read instance status id:%s, details:%v, err:%v", machine.Id(), machine, err)
			return None, nil
		}
		if instanceStatus == status.Provisioning {
			logger.Infof("found machine provisioning id:%s, details:%v", machine.Id(), machine)
			return Pending, nil
		}
		return None, nil
	}
	logger.Infof("machine %s already started as instance %q", machine.Id(), instId)

	if state.ContainerTypeFromId(machine.Id()) != "" {
		return Maintain, nil
	}
	return None, nil
}

// findUnknownInstances finds instances which are not associated with a machine.
func (task *provisionerTask) findUnknownInstances(stopping []instance.Instance) ([]instance.Instance, error) {
	// Make a copy of the instances we know about.
	instances := make(map[instance.Id]instance.Instance)
	for k, v := range task.instances {
		instances[k] = v
	}

	for _, m := range task.machines {
		instId, err := m.InstanceId()
		switch {
		case err == nil:
			delete(instances, instId)
		case params.IsCodeNotProvisioned(err):
		case params.IsCodeNotFoundOrCodeUnauthorized(err):
		default:
			return nil, err
		}
	}
	// Now remove all those instances that we are stopping already as we
	// know about those and don't want to include them in the unknown list.
	for _, inst := range stopping {
		delete(instances, inst.Id())
	}
	var unknown []instance.Instance
	for _, inst := range instances {
		unknown = append(unknown, inst)
	}
	return unknown, nil
}

// instancesForDeadMachines returns a list of instance.Instance that represent
// the list of dead machines running in the provider. Missing machines are
// omitted from the list.
func (task *provisionerTask) instancesForDeadMachines(deadMachines []*apiprovisioner.Machine) []instance.Instance {
	var instances []instance.Instance
	for _, machine := range deadMachines {
		instId, err := machine.InstanceId()
		if err == nil {
			keep, _ := machine.KeepInstance()
			if keep {
				logger.Debugf("machine %v is dead but keep-instance is true", instId)
				continue
			}
			instance, found := task.instances[instId]
			// If the instance is not found we can't stop it.
			if found {
				instances = append(instances, instance)
			}
		}
	}
	return instances
}

func (task *provisionerTask) stopInstances(instances []instance.Instance) error {
	// Although calling StopInstance with an empty slice should produce no change in the
	// provider, environs like dummy do not consider this a noop.
	if len(instances) == 0 {
		return nil
	}
	if wrench.IsActive("provisioner", "stop-instances") {
		return errors.New("wrench in the works")
	}

	ids := make([]instance.Id, len(instances))
	for i, inst := range instances {
		ids[i] = inst.Id()
	}
	if err := task.broker.StopInstances(ids...); err != nil {
		return errors.Annotate(err, "broker failed to stop instances")
	}
	return nil
}

func (task *provisionerTask) constructInstanceConfig(
	machine *apiprovisioner.Machine,
	auth authentication.AuthenticationProvider,
	pInfo *params.ProvisioningInfo,
) (*instancecfg.InstanceConfig, error) {

	stateInfo, apiInfo, err := auth.SetupAuthentication(machine)
	if err != nil {
		return nil, errors.Annotate(err, "failed to setup authentication")
	}

	// Generated a nonce for the new instance, with the format: "machine-#:UUID".
	// The first part is a badge, specifying the tag of the machine the provisioner
	// is running on, while the second part is a random UUID.
	uuid, err := utils.NewUUID()
	if err != nil {
		return nil, errors.Annotate(err, "failed to generate a nonce for machine "+machine.Id())
	}

	nonce := fmt.Sprintf("%s:%s", task.machineTag, uuid)
	instanceConfig, err := instancecfg.NewInstanceConfig(
		names.NewControllerTag(controller.Config(pInfo.ControllerConfig).ControllerUUID()),
		machine.Id(),
		nonce,
		task.imageStream,
		pInfo.Series,
		apiInfo,
	)
	if err != nil {
		return nil, errors.Trace(err)
	}

	instanceConfig.Tags = pInfo.Tags
	if len(pInfo.Jobs) > 0 {
		instanceConfig.Jobs = pInfo.Jobs
	}

	if multiwatcher.AnyJobNeedsState(instanceConfig.Jobs...) {
		publicKey, err := simplestreams.UserPublicSigningKey()
		if err != nil {
			return nil, err
		}
		instanceConfig.Controller = &instancecfg.ControllerConfig{
			PublicImageSigningKey: publicKey,
			MongoInfo:             stateInfo,
		}
		instanceConfig.Controller.Config = make(map[string]interface{})
		for k, v := range pInfo.ControllerConfig {
			instanceConfig.Controller.Config[k] = v
		}
	}

	return instanceConfig, nil
}

func constructStartInstanceParams(
	controllerUUID string,
	machine *apiprovisioner.Machine,
	instanceConfig *instancecfg.InstanceConfig,
	provisioningInfo *params.ProvisioningInfo,
	possibleTools coretools.List,
	availabilityZone string,
) (environs.StartInstanceParams, error) {

	volumes := make([]storage.VolumeParams, len(provisioningInfo.Volumes))
	for i, v := range provisioningInfo.Volumes {
		volumeTag, err := names.ParseVolumeTag(v.VolumeTag)
		if err != nil {
			return environs.StartInstanceParams{}, errors.Trace(err)
		}
		if v.Attachment == nil {
			return environs.StartInstanceParams{}, errors.Errorf("volume params missing attachment")
		}
		machineTag, err := names.ParseMachineTag(v.Attachment.MachineTag)
		if err != nil {
			return environs.StartInstanceParams{}, errors.Trace(err)
		}
		if machineTag != machine.Tag() {
			return environs.StartInstanceParams{}, errors.Errorf("volume attachment params has invalid machine tag")
		}
		if v.Attachment.InstanceId != "" {
			return environs.StartInstanceParams{}, errors.Errorf("volume attachment params specifies instance ID")
		}
		volumes[i] = storage.VolumeParams{
			volumeTag,
			v.Size,
			storage.ProviderType(v.Provider),
			v.Attributes,
			v.Tags,
			&storage.VolumeAttachmentParams{
				AttachmentParams: storage.AttachmentParams{
					Machine:  machineTag,
					ReadOnly: v.Attachment.ReadOnly,
				},
				Volume: volumeTag,
			},
		}
	}
	volumeAttachments := make([]storage.VolumeAttachmentParams, len(provisioningInfo.VolumeAttachments))
	for i, v := range provisioningInfo.VolumeAttachments {
		volumeTag, err := names.ParseVolumeTag(v.VolumeTag)
		if err != nil {
			return environs.StartInstanceParams{}, errors.Trace(err)
		}
		machineTag, err := names.ParseMachineTag(v.MachineTag)
		if err != nil {
			return environs.StartInstanceParams{}, errors.Trace(err)
		}
		if machineTag != machine.Tag() {
			return environs.StartInstanceParams{}, errors.Errorf("volume attachment params has invalid machine tag")
		}
		if v.InstanceId != "" {
			return environs.StartInstanceParams{}, errors.Errorf("volume attachment params specifies instance ID")
		}
		if v.VolumeId == "" {
			return environs.StartInstanceParams{}, errors.Errorf("volume attachment params does not specify volume ID")
		}
		volumeAttachments[i] = storage.VolumeAttachmentParams{
			AttachmentParams: storage.AttachmentParams{
				Provider: storage.ProviderType(v.Provider),
				Machine:  machineTag,
				ReadOnly: v.ReadOnly,
			},
			Volume:   volumeTag,
			VolumeId: v.VolumeId,
		}
	}

	var subnetsToZones map[network.Id][]string
	if provisioningInfo.SubnetsToZones != nil {
		// Convert subnet provider ids from string to network.Id.
		subnetsToZones = make(map[network.Id][]string, len(provisioningInfo.SubnetsToZones))
		for providerId, zones := range provisioningInfo.SubnetsToZones {
			subnetsToZones[network.Id(providerId)] = zones
		}
	}

	var endpointBindings map[string]network.Id
	if len(provisioningInfo.EndpointBindings) != 0 {
		endpointBindings = make(map[string]network.Id)
		for endpoint, space := range provisioningInfo.EndpointBindings {
			endpointBindings[endpoint] = network.Id(space)
		}
	}
	possibleImageMetadata := make([]*imagemetadata.ImageMetadata, len(provisioningInfo.ImageMetadata))
	for i, metadata := range provisioningInfo.ImageMetadata {
		possibleImageMetadata[i] = &imagemetadata.ImageMetadata{
			Id:          metadata.ImageId,
			Arch:        metadata.Arch,
			RegionAlias: metadata.Region,
			RegionName:  metadata.Region,
			Storage:     metadata.RootStorageType,
			Stream:      metadata.Stream,
			VirtType:    metadata.VirtType,
			Version:     metadata.Version,
		}
	}

	// convert availabilityZone to Placement format for zones
	var zone string
	if provisioningInfo.Placement == "" && availabilityZone != "" {
		zone = fmt.Sprintf("zone=%s", availabilityZone)
	} else {
		zone = provisioningInfo.Placement
	}

	return environs.StartInstanceParams{
		ControllerUUID:    controllerUUID,
		Constraints:       provisioningInfo.Constraints,
		Tools:             possibleTools,
		InstanceConfig:    instanceConfig,
		Placement:         zone,
		DistributionGroup: machine.DistributionGroup,
		Volumes:           volumes,
		VolumeAttachments: volumeAttachments,
		SubnetsToZones:    subnetsToZones,
		EndpointBindings:  endpointBindings,
		ImageMetadata:     possibleImageMetadata,
		StatusCallback:    machine.SetInstanceStatus,
	}, nil
}

func (task *provisionerTask) maintainMachines(machines []*apiprovisioner.Machine) error {
	for _, m := range machines {
		logger.Infof("maintainMachines: %v", m)
		startInstanceParams := environs.StartInstanceParams{}
		startInstanceParams.InstanceConfig = &instancecfg.InstanceConfig{}
		startInstanceParams.InstanceConfig.MachineId = m.Id()
		if err := task.broker.MaintainInstance(startInstanceParams); err != nil {
			return errors.Annotatef(err, "cannot maintain machine %v", m)
		}
	}
	return nil
}

type availabilityZoneMachine struct {
	zoneName   string
	machineIds set.Strings
	failures   int
}

func (task *provisionerTask) populateAvailabilityZoneMachines() error {
	task.azMachinesMutex.Lock()
	task.azMachinesMutex.Unlock()

	if len(task.availabilityZoneMachines) > 0 {
		task.retryFailedAvailabilityZones()
		return nil
	}

	var zonedEnv providercommon.ZonedEnviron
	var ok bool
	if zonedEnv, ok = task.broker.(providercommon.ZonedEnviron); !ok {
		// Nothing to do if the provider doesn't implement AvailabilityZones
		return errors.NotImplementedf("Provider ZoneEnviron")
	}

	availabilityZoneInstances, err := providercommon.AvailabilityZoneAllocations(zonedEnv, []instance.Id{})
	if err != nil {
		return err
	}

	instanceMachines := make(map[instance.Id]string)
	for _, machine := range task.machines {
		instId, err := machine.InstanceId()
		if err != nil {
			continue
		}
		instanceMachines[instId] = machine.Id()
	}

	// convert instances to machines
	task.availabilityZoneMachines = make([]*availabilityZoneMachine, len(availabilityZoneInstances))
	for i, instances := range availabilityZoneInstances {
		machineIds := set.NewStrings()
		for _, instanceId := range instances.Instances {
			if id, ok := instanceMachines[instanceId]; ok {
				machineIds.Add(id)
			}
		}
		task.availabilityZoneMachines[i] = &availabilityZoneMachine{
			zoneName:   instances.ZoneName,
			machineIds: machineIds,
		}
	}
	return nil
}

func (task *provisionerTask) retryFailedAvailabilityZones() {
	// ToDo ProvisionerParallelization 2017-10-10
	// Perhaps this algorithm can be refined with a time component?

	// If all availability zones are marked failed, clear the failures
	// allow them to be tried again.
	var failed int
	for _, zone := range task.availabilityZoneMachines {
		if zone.failures > 0 {
			failed += 1
		}
	}
	if failed != len(task.availabilityZoneMachines) {
		return
	}
	for _, zone := range task.availabilityZoneMachines {
		zone.failures = 0
	}
}

func (task *provisionerTask) populateDistributionGroupZoneMap(distributionGroupIds []string) []*availabilityZoneMachine {
	var dgAvailabilityZoneMachines []*availabilityZoneMachine
	dgSet := set.NewStrings(distributionGroupIds...)
	for _, azm := range task.availabilityZoneMachines {
		dgAvailabilityZoneMachines = append(dgAvailabilityZoneMachines, &availabilityZoneMachine{
			azm.zoneName,
			azm.machineIds.Intersection(dgSet),
			azm.failures,
		})
	}
	return dgAvailabilityZoneMachines
}

func (task *provisionerTask) machineAvailabilityZoneDistribution(machines []*apiprovisioner.Machine) (map[*apiprovisioner.Machine]string, error) {
	machineZones := make(map[*apiprovisioner.Machine]string, len(machines))

	if len(machines) == 0 {
		return machineZones, nil
	}

	if err := task.populateAvailabilityZoneMachines(); err != nil {
		return machineZones, err
	}

	// Retrieve any DistributionGroups for these machines
	dgMachines := make([]names.MachineTag, len(machines))
	for i, machine := range machines {
		dgMachines[i] = machine.MachineTag()
	}
	distributionGroups, err := task.provisionerGetter.DistributionGroupByMachineId(dgMachines...)
	if err != nil {
		return machineZones, err
	}

	task.azMachinesMutex.Lock()
	task.azMachinesMutex.Unlock()

	// assign an initial az to a machine based on lowest population.
	// if the machine has a distribution group, assign based on lowest
	// az population of the distribution group machines.
	for i, machine := range machines {
		sort.Sort(byPopulationThenName(task.availabilityZoneMachines))

		dgResult := distributionGroups[i]
		if dgResult.Err != nil {
			return machineZones, err
		}

		if len(dgResult.MachineIds) > 0 {
			dgZoneMap := task.populateDistributionGroupZoneMap(dgResult.MachineIds)
			sort.Sort(byPopulationThenName(dgZoneMap))

			for _, dgZoneMachines := range dgZoneMap {
				if dgZoneMachines.failures == 0 {
					machineZones[machine] = dgZoneMachines.zoneName
					for _, azm := range task.availabilityZoneMachines {
						if azm.zoneName == dgZoneMachines.zoneName {
							azm.machineIds.Add(machine.Id())
							break
						}
					}
					break
				}
			}
		} else {
			for _, zoneMachines := range task.availabilityZoneMachines {
				if zoneMachines.failures == 0 {
					machineZones[machine] = zoneMachines.zoneName
					zoneMachines.machineIds.Add(machine.Id())
					break
				}
			}
		}
	}

	return machineZones, nil
}

type byPopulationThenName []*availabilityZoneMachine

func (b byPopulationThenName) Len() int {
	return len(b)
}

func (b byPopulationThenName) Less(i, j int) bool {
	switch {
	case b[i].machineIds.Size() < b[j].machineIds.Size():
		return true
	case b[i].machineIds.Size() == b[j].machineIds.Size():
		return b[i].zoneName < b[j].zoneName
	}
	return false
}

func (b byPopulationThenName) Swap(i, j int) {
	b[i], b[j] = b[j], b[i]
}

func (task *provisionerTask) startMachines(machines []*apiprovisioner.Machine) error {
	if len(machines) == 0 {
		return nil
	}

	machineZones, _ := task.machineAvailabilityZoneDistribution(machines)

	wg := sync.WaitGroup{}
	wg.Add(len(machines))
	errMachines := make(map[*apiprovisioner.Machine]error)

	for _, m := range machines {
		// Make sure we shouldn't be stopping before we start the next machine
		select {
		case <-task.catacomb.Dying():
			return task.catacomb.ErrDying()
		default:
		}

		var zone string
		if z, ok := machineZones[m]; ok {
			zone = z
		}

		go func(machine *apiprovisioner.Machine, zone string) {
			defer wg.Done()
			pInfo, startInstanceParams, err := task.setupToStartMachine(machine, zone)
			if err != nil {
				errMachines[machine] = err
			}
			if err := task.startMachine(machine, pInfo, startInstanceParams); err != nil {
				errMachines[machine] = err
			}
		}(m, zone)
	}

	// ToDo ProvisionerParallelization 2017-10-09
	// Is this necessary?
	wg.Wait()
	if len(errMachines) > 0 {
		for mach, err := range errMachines {
			task.setErrorStatus("cannot start machine %q: %v", mach, err)
		}
	}
	return nil
}

func (task *provisionerTask) setErrorStatus(message string, machine *apiprovisioner.Machine, err error) error {
	logger.Errorf(message, machine, err)
	if err2 := machine.SetInstanceStatus(status.ProvisioningError, err.Error(), nil); err2 != nil {
		// Something is wrong with this machine, better report it back.
		return errors.Annotatef(err2, "cannot set error status for machine %q", machine)
	}
	return err
}

func (task *provisionerTask) setupToStartMachine(machine *apiprovisioner.Machine, zone string) (*params.ProvisioningInfo, environs.StartInstanceParams, error) {

	pInfo, err := machine.ProvisioningInfo()
	if err != nil {
		return nil, environs.StartInstanceParams{}, task.setErrorStatus("fetching provisioning info for machine %q: %v", machine, err)
	}

	instanceCfg, err := task.constructInstanceConfig(machine, task.auth, pInfo)
	if err != nil {
		return nil, environs.StartInstanceParams{}, task.setErrorStatus("creating instance config for machine %q: %v", machine, err)
	}

	assocProvInfoAndMachCfg(pInfo, instanceCfg)

	var arch string
	if pInfo.Constraints.Arch != nil {
		arch = *pInfo.Constraints.Arch
	}

	v, err := machine.ModelAgentVersion()
	if err != nil {
		return nil, environs.StartInstanceParams{}, errors.Trace(err)
	}
	possibleTools, err := task.toolsFinder.FindTools(
		*v,
		pInfo.Series,
		arch,
	)
	if err != nil {
		return nil, environs.StartInstanceParams{}, task.setErrorStatus("cannot find tools for machine %q: %v", machine, err)
	}

	// ToDo ProvisionerParallelization 2017-10-03
	// Fix StartInstanceParams struct to carry the availability zone,
	// overwrite the placement as done currently.
	// DistributionGroups() can be removed.
	startInstanceParams, err := constructStartInstanceParams(
		task.controllerUUID,
		machine,
		instanceCfg,
		pInfo,
		possibleTools,
		zone,
	)
	if err != nil {
		return nil, environs.StartInstanceParams{}, task.setErrorStatus("cannot construct params for machine %q: %v", machine, err)
	}

	return pInfo, startInstanceParams, nil
}

func (task *provisionerTask) startMachine(
	machine *apiprovisioner.Machine,
	provisioningInfo *params.ProvisioningInfo,
	startInstanceParams environs.StartInstanceParams,
) error {
	// TODO (jam): 2017-01-19 Should we be setting this earlier in the cycle?
	if err := machine.SetInstanceStatus(status.Provisioning, "starting", nil); err != nil {
		logger.Errorf("%v", err)
	}

	// ToDo ProvisionerParallelization 2017-10-03
	// Improve the retry loop, newer methodology
	// Is rate limiting handled correctly?

	// Increase attempt by a multiplier of the number of availability aones
	// to account for the retries removed from the providers when the provisioner
	// was parallelized.
	task.azMachinesMutex.RLock()
	var attemptsLeft int
	if len(task.availabilityZoneMachines) > 0 {
		attemptsLeft = task.retryStartInstanceStrategy.retryCount * len(task.availabilityZoneMachines)
	} else {
		// Some providers don't have availability zones
		attemptsLeft = task.retryStartInstanceStrategy.retryCount
	}
	task.azMachinesMutex.RUnlock()

	var result *environs.StartInstanceResult
	for ; attemptsLeft >= 0; attemptsLeft-- {
		attemptResult, err := task.broker.StartInstance(startInstanceParams)
		if err == nil {
			result = attemptResult
			break
		} else if attemptsLeft <= 0 {
			// Set the state to error, so the machine will be skipped
			// next time until the error is resolved, but don't return
			// an error; just keep going with the other machines.
			return task.setErrorStatus("cannot start instance for machine %q: %v", machine, err)
		}

		retryMsg := fmt.Sprintf("failed to start instance (%s), retrying in %v (%d more attempts)",
			err.Error(), task.retryStartInstanceStrategy.retryDelay, attemptsLeft)
		logger.Warningf(retryMsg)
		if err3 := machine.SetInstanceStatus(status.Provisioning, retryMsg, nil); err3 != nil {
			logger.Errorf("%v", err3)
		}

		select {
		case <-task.catacomb.Dying():
			return task.catacomb.ErrDying()
		case <-time.After(task.retryStartInstanceStrategy.retryDelay):
		}

		newZone, err2 := task.availabilityZoneRetry(machine, startInstanceParams.Placement)
		if err2 != nil {
			return err2
		}
		startInstanceParams.Placement = fmt.Sprintf("zone=%s", newZone) // placement
	}

	networkConfig := networkingcommon.NetworkConfigFromInterfaceInfo(result.NetworkInfo)
	volumes := volumesToAPIserver(result.Volumes)
	volumeNameToAttachmentInfo := volumeAttachmentsToAPIserver(result.VolumeAttachments)

	if err := machine.SetInstanceInfo(
		result.Instance.Id(),
		startInstanceParams.InstanceConfig.MachineNonce,
		result.Hardware,
		networkConfig,
		volumes,
		volumeNameToAttachmentInfo,
	); err != nil {
		// We need to stop the instance right away here, set error status and go on.
		if err2 := task.setErrorStatus("cannot register instance for machine %v: %v", machine, err); err2 != nil {
			logger.Errorf("%v", errors.Annotate(err2, "cannot set machine's status"))
		}
		if err2 := task.broker.StopInstances(result.Instance.Id()); err2 != nil {
			logger.Errorf("%v", errors.Annotate(err2, "after failing to set instance info"))
		}
		return errors.Annotate(err, "cannot set instance info")
	}

	logger.Infof(
		"started machine %s as instance %s with hardware %q, network config %+v, volumes %v, volume attachments %v, subnets to zones %v",
		machine,
		result.Instance.Id(),
		result.Hardware,
		networkConfig,
		volumes,
		volumeNameToAttachmentInfo,
		startInstanceParams.SubnetsToZones,
	)
	return nil
}

func stringForAvailabilityZoneMachine(slice []*availabilityZoneMachine) string {
	retVal := ""
	for _, p := range slice {
		retVal = fmt.Sprintf("%s; %+v", retVal, p)
	}
	return retVal
}

func (task *provisionerTask) availabilityZoneRetry(machine *apiprovisioner.Machine, placement string) (string, error) {
	// mark zone don't try and remove from saved knowledge
	placementParts := strings.Split(placement, "=")
	if placementParts[0] != "zone" {
		return "", errors.New("no suggestions to make, placement not a zone.")
	}
	zone := placementParts[1]
	if zone == "" {
		return "", errors.New("no zone provided.")
	}

	task.azMachinesMutex.Lock()
	for _, zoneMachines := range task.availabilityZoneMachines {
		if zone == zoneMachines.zoneName {
			zoneMachines.failures += 1
			zoneMachines.machineIds.Remove(machine.Id())
			break
		}
	}
	task.azMachinesMutex.Unlock()

	// ask for a new zone
	machineZone, err := task.machineAvailabilityZoneDistribution([]*apiprovisioner.Machine{machine})
	if err != nil {
		return "", err
	}
	if machineZone[machine] == zone {
		return "", errors.New("suggesting availability zone which failed.")
	} else if machineZone[machine] == "" {
		// ToDo ProvisionerParallelization 2017-10-10
		// is this really an error.... to return ""???
		return "", errors.New("no new availabilty zone to suggest.")
	}
	return machineZone[machine], nil
}

type provisioningInfo struct {
	Constraints    constraints.Value
	Series         string
	Placement      string
	InstanceConfig *instancecfg.InstanceConfig
	SubnetsToZones map[string][]string
}

func assocProvInfoAndMachCfg(
	provInfo *params.ProvisioningInfo,
	instanceConfig *instancecfg.InstanceConfig,
) *provisioningInfo {
	return &provisioningInfo{
		Constraints:    provInfo.Constraints,
		Series:         provInfo.Series,
		Placement:      provInfo.Placement,
		InstanceConfig: instanceConfig,
		SubnetsToZones: provInfo.SubnetsToZones,
	}
}

func volumesToAPIserver(volumes []storage.Volume) []params.Volume {
	result := make([]params.Volume, len(volumes))
	for i, v := range volumes {
		result[i] = params.Volume{
			v.Tag.String(),
			params.VolumeInfo{
				v.VolumeId,
				v.HardwareId,
				v.WWN,
				"", // pool
				v.Size,
				v.Persistent,
			},
		}
	}
	return result
}

func volumeAttachmentsToAPIserver(attachments []storage.VolumeAttachment) map[string]params.VolumeAttachmentInfo {
	result := make(map[string]params.VolumeAttachmentInfo)
	for _, a := range attachments {
		result[a.Volume.String()] = params.VolumeAttachmentInfo{
			a.DeviceName,
			a.DeviceLink,
			a.BusAddress,
			a.ReadOnly,
		}
	}
	return result
}
