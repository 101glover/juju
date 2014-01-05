// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package manual

import (
	"errors"
	"fmt"

	"launchpad.net/juju-core/constraints"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/bootstrap"
	envtools "launchpad.net/juju-core/environs/tools"
	"launchpad.net/juju-core/instance"
	"launchpad.net/juju-core/tools"
	"launchpad.net/juju-core/worker/localstorage"
)

const BootstrapInstanceId = instance.Id(manualInstancePrefix)

// LocalStorageEnviron is an Environ where the bootstrap node
// manages its own local storage.
type LocalStorageEnviron interface {
	environs.Environ
	localstorage.LocalStorageConfig
}

type BootstrapArgs struct {
	Host          string
	DataDir       string
	Environ       LocalStorageEnviron
	PossibleTools tools.List
	Context       environs.BootstrapContext

	// If series and hardware characteristics
	// are known ahead of time, they can be
	// set here and Bootstrap will not attempt
	// to detect them again.
	Series                  string
	HardwareCharacteristics *instance.HardwareCharacteristics
}

func errMachineIdInvalid(machineId string) error {
	return fmt.Errorf("%q is not a valid machine ID", machineId)
}

// NewManualBootstrapEnviron wraps a LocalStorageEnviron with another which
// overrides the Bootstrap method; when Bootstrap is invoked, the specified
// host will be manually bootstrapped.
func Bootstrap(args BootstrapArgs) (err error) {
	if args.Host == "" {
		return errors.New("host argument is empty")
	}
	if args.Environ == nil {
		return errors.New("environ argument is nil")
	}
	if args.DataDir == "" {
		return errors.New("data-dir argument is empty")
	}

	provisioned, err := checkProvisioned(args.Host)
	if err != nil {
		return fmt.Errorf("failed to check provisioned status: %v", err)
	}
	if provisioned {
		return ErrProvisioned
	}

	var series string
	var hc instance.HardwareCharacteristics
	if args.Series != "" && args.HardwareCharacteristics != nil {
		series = args.Series
		hc = *args.HardwareCharacteristics
	} else {
		hc, series, err = DetectSeriesAndHardwareCharacteristics(args.Host)
		if err != nil {
			return fmt.Errorf("error detecting hardware characteristics: %v", err)
		}
	}

	// Filter tools based on detected series/arch.
	logger.Infof("Filtering possible tools: %v", args.PossibleTools)
	possibleTools, err := args.PossibleTools.Match(tools.Filter{
		Arch:   *hc.Arch,
		Series: series,
	})
	if err != nil {
		return err
	}

	// Store the state file. If provisioning fails, we'll remove the file.
	logger.Infof("Saving bootstrap state file to bootstrap storage")
	bootstrapStorage := args.Environ.Storage()
	err = bootstrap.SaveState(
		bootstrapStorage,
		&bootstrap.BootstrapState{
			StateInstances:  []instance.Id{BootstrapInstanceId},
			Characteristics: []instance.HardwareCharacteristics{hc},
		},
	)
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			logger.Errorf("bootstrapping failed, removing state file: %v", err)
			bootstrapStorage.Remove(bootstrap.StateFile)
		}
	}()

	// Get a file:// scheme tools URL for the tools, which will have been
	// copied to the remote machine's storage directory.
	tools := *possibleTools[0]
	storageDir := args.Environ.StorageDir()
	toolsStorageName := envtools.StorageName(tools.Version)
	tools.URL = fmt.Sprintf("file://%s/%s", storageDir, toolsStorageName)

	// Add the local storage configuration.
	agentEnv, err := localstorage.StoreConfig(args.Environ)
	if err != nil {
		return err
	}

	// Finally, provision the machine agent.
	stateFileURL := fmt.Sprintf("file://%s/%s", storageDir, bootstrap.StateFile)
	mcfg := environs.NewBootstrapMachineConfig(stateFileURL)
	if args.DataDir != "" {
		mcfg.DataDir = args.DataDir
	}
	mcfg.Tools = &tools
	err = environs.FinishMachineConfig(mcfg, args.Environ.Config(), constraints.Value{})
	if err != nil {
		return err
	}
	for k, v := range agentEnv {
		mcfg.AgentEnvironment[k] = v
	}
	return provisionMachineAgent(args.Host, mcfg, args.Context.Stdin(), args.Context.Stdout(), args.Context.Stderr())
}
