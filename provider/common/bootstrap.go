// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package common

import (
	"fmt"
	"net"
	"os"
	"time"

	"launchpad.net/loggo"
	"launchpad.net/tomb"

	coreCloudinit "launchpad.net/juju-core/cloudinit"
	"launchpad.net/juju-core/cloudinit/sshinit"
	"launchpad.net/juju-core/constraints"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/bootstrap"
	"launchpad.net/juju-core/environs/cloudinit"
	"launchpad.net/juju-core/instance"
	coretools "launchpad.net/juju-core/tools"
)

var logger = loggo.GetLogger("juju.provider.common")

// Bootstrap is a common implementation of the Bootstrap method defined on
// environs.Environ; we strongly recommend that this implementation be used
// when writing a new provider.
func Bootstrap(ctx environs.BootstrapContext, env environs.Environ, cons constraints.Value) (err error) {
	// TODO make safe in the case of racing Bootstraps
	// If two Bootstraps are called concurrently, there's
	// no way to make sure that only one succeeds.

	bootstrapContext := &bootstrapContext{ctx, nil}
	defer bootstrapContext.setInterruptHandler(nil)

	var inst instance.Instance
	defer func() { handleBootstrapError(err, bootstrapContext, inst, env) }()

	// Create an empty bootstrap state file so we can get its URL.
	// It will be updated with the instance id and hardware characteristics
	// after the bootstrap instance is started.
	stateFileURL, err := bootstrap.CreateStateFile(env.Storage())
	if err != nil {
		return err
	}
	machineConfig := environs.NewBootstrapMachineConfig(stateFileURL)

	selectedTools, err := EnsureBootstrapTools(env, env.Config().DefaultSeries(), cons.Arch)
	if err != nil {
		return err
	}

	fmt.Fprintln(ctx.Stderr(), "Launching instance")
	inst, hw, err := env.StartInstance(cons, selectedTools, machineConfig)
	if err != nil {
		return fmt.Errorf("cannot start bootstrap instance: %v", err)
	}
	fmt.Fprintf(ctx.Stderr(), " - %s\n", inst.Id())

	var characteristics []instance.HardwareCharacteristics
	if hw != nil {
		characteristics = []instance.HardwareCharacteristics{*hw}
	}
	err = bootstrap.SaveState(
		env.Storage(),
		&bootstrap.BootstrapState{
			StateInstances:  []instance.Id{inst.Id()},
			Characteristics: characteristics,
		})
	if err != nil {
		return fmt.Errorf("cannot save state: %v", err)
	}
	return FinishBootstrap(bootstrapContext, inst, machineConfig)
}

type bootstrapContext struct {
	environs.BootstrapContext
	ch chan os.Signal
}

func (c *bootstrapContext) setInterruptHandler(f func()) {
	oldch := c.ch
	if f != nil {
		c.ch = make(chan os.Signal, 1)
		c.InterruptNotify(c.ch)
		go func(ch <-chan os.Signal) {
			for {
				if _, ok := <-ch; ok {
					f()
				} else {
					break
				}
			}
		}(c.ch)
	} else {
		c.ch = nil
	}
	if oldch != nil {
		c.StopInterruptNotify(oldch)
		close(oldch)
	}
}

// handelBootstrapError cleans up after a failed bootstrap.
func handleBootstrapError(err error, ctx *bootstrapContext, inst instance.Instance, env environs.Environ) {
	if err == nil {
		return
	}

	ctx.setInterruptHandler(func() {
		fmt.Fprintln(ctx.Stderr(), "Cleaning up failed bootstrap")
	})

	if inst != nil {
		fmt.Fprintln(ctx.Stderr(), "Stopping instance...")
		if stoperr := env.StopInstances([]instance.Instance{inst}); stoperr != nil {
			logger.Errorf("cannot stop failed bootstrap instance %q: %v", inst.Id(), stoperr)
		} else {
			// set to nil so we know we can safely delete the state file
			inst = nil
		}
	}
	// We only delete the bootstrap state file if either we didn't
	// start an instance, or we managed to cleanly stop it.
	if inst == nil {
		if rmerr := bootstrap.DeleteStateFile(env.Storage()); rmerr != nil {
			logger.Errorf("cannot delete bootstrap state file: %v", rmerr)
		}
	}
}

// FinishBootstrap completes the bootstrap process by connecting
// to the instance via SSH and carrying out the cloud-config.
//
// Note: FinishBootstrap is exposed so it can be replaced for testing.
var FinishBootstrap = func(ctx_ environs.BootstrapContext, inst instance.Instance, machineConfig *cloudinit.MachineConfig) error {
	var t tomb.Tomb
	ctx := ctx_.(*bootstrapContext)
	ctx.setInterruptHandler(func() { t.Killf("interrupted") })
	dnsName, err := waitSSH(ctx, inst, &t)
	if err != nil {
		return err
	}
	// Bootstrap is synchronous, and will spawn a subprocess
	// to complete the procedure. If the user hits Ctrl-C,
	// SIGINT is sent to the foreground process attached to
	// the terminal, which will be the ssh subprocess at that
	// point.
	ctx.setInterruptHandler(func() {})
	cloudcfg := coreCloudinit.New()
	if err := cloudinit.ConfigureJuju(machineConfig, cloudcfg); err != nil {
		return err
	}
	return sshinit.Configure(sshinit.ConfigureParams{
		Host:   "ubuntu@" + dnsName,
		Config: cloudcfg,
		Stdin:  ctx.Stdin(),
		Stdout: ctx.Stdout(),
		Stderr: ctx.Stderr(),
	})
}

// waitSSH waits for the instance to be assigned a DNS
// entry, then waits until we can connect to it via SSH.
func waitSSH(ctx environs.BootstrapContext, inst instance.Instance, t *tomb.Tomb) (dnsName string, err error) {
	defer t.Done()

	// Wait for a DNS name.
	fmt.Fprint(ctx.Stderr(), "Waiting for DNS name")
	for {
		fmt.Fprintf(ctx.Stderr(), ".")
		dnsName, err = inst.DNSName()
		if err == nil {
			break
		} else if err != instance.ErrNoDNSName {
			fmt.Fprintln(ctx.Stderr())
			return "", t.Killf("getting DNS name: %v", err)
		}
		select {
		case <-time.After(1 * time.Second):
		case <-t.Dying():
			fmt.Fprintln(ctx.Stderr())
			return "", t.Err()
		}
	}
	fmt.Fprintf(ctx.Stderr(), "\n - %v\n", dnsName)

	// Wait until we can open a connection to port 22.
	fmt.Fprintf(ctx.Stderr(), "Attempting to connect to %s:22", dnsName)
	for {
		fmt.Fprintf(ctx.Stderr(), ".")
		conn, err := net.DialTimeout("tcp", dnsName+":22", 5*time.Second)
		if err == nil {
			conn.Close()
			fmt.Fprintln(ctx.Stderr())
			return dnsName, nil
		} else {
			logger.Debugf("connection failed: %v", err)
		}
		select {
		case <-time.After(5 * time.Second):
		case <-t.Dying():
			return "", t.Err()
		}
	}
}

// EnsureBootstrapTools finds tools, syncing with an external tools source as
// necessary; it then selects the newest tools to bootstrap with, and sets
// agent-version.
func EnsureBootstrapTools(env environs.Environ, series string, arch *string) (coretools.List, error) {
	possibleTools, err := bootstrap.EnsureToolsAvailability(env, series, arch)
	if err != nil {
		return nil, err
	}
	return bootstrap.SetBootstrapTools(env, possibleTools)
}

// EnsureNotBootstrapped returns null if the environment is not bootstrapped,
// and an error if it is or if the function was not able to tell.
func EnsureNotBootstrapped(env environs.Environ) error {
	_, err := bootstrap.LoadState(env.Storage())
	// If there is no error loading the bootstrap state, then we are
	// bootstrapped.
	if err == nil {
		return fmt.Errorf("environment is already bootstrapped")
	}
	if err == environs.ErrNotBootstrapped {
		return nil
	}
	return err
}
