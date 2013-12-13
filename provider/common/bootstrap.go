// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package common

import (
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"sync"
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
func Bootstrap(env environs.Environ, cons constraints.Value) (err error) {
	// TODO make safe in the case of racing Bootstraps
	// If two Bootstraps are called concurrently, there's
	// no way to make sure that only one succeeds.

	// TODO(axw) 2013-11-22 #1237736
	// Modify environs/Environ Bootstrap method signature
	// to take a new context structure, which contains
	// Std{in,out,err}, and interrupt signal handling.
	ctx := BootstrapContext{Stderr: os.Stderr}

	var inst instance.Instance
	defer func() { handleBootstrapError(err, &ctx, inst, env) }()

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

	fmt.Fprintln(ctx.Stderr, "Launching instance")
	inst, hw, err := env.StartInstance(cons, selectedTools, machineConfig)
	if err != nil {
		return fmt.Errorf("cannot start bootstrap instance: %v", err)
	}
	fmt.Fprintf(ctx.Stderr, " - %s\n", inst.Id())

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
	return FinishBootstrap(&ctx, inst, machineConfig)
}

// handelBootstrapError cleans up after a failed bootstrap.
func handleBootstrapError(err error, ctx *BootstrapContext, inst instance.Instance, env environs.Environ) {
	if err == nil {
		return
	}
	logger.Errorf("bootstrap failed: %v", err)
	ctx.SetInterruptHandler(func() {
		fmt.Fprintln(ctx.Stderr, "Cleaning up failed bootstrap")
	})
	if inst != nil {
		fmt.Fprintln(ctx.Stderr, "Stopping instance...")
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
	ctx.SetInterruptHandler(nil)
}

// FinishBootstrap completes the bootstrap process by connecting
// to the instance via SSH and carrying out the cloud-config.
//
// Note: FinishBootstrap is exposed so it can be replaced for testing.
var FinishBootstrap = func(ctx *BootstrapContext, inst instance.Instance, machineConfig *cloudinit.MachineConfig) error {
	var t tomb.Tomb
	ctx.SetInterruptHandler(func() { t.Killf("interrupted") })
	// TODO: jam 2013-12-04 bug #1257649
	// It would be nice if users had some controll over their bootstrap
	// timeout, since it is unlikely to be a perfect match for all clouds.
	dnsName, err := waitSSH(ctx, inst, &t, DefaultBootstrapSSHTimeout())
	if err != nil {
		return err
	}
	// Bootstrap is synchronous, and will spawn a subprocess
	// to complete the procedure. If the user hits Ctrl-C,
	// SIGINT is sent to the foreground process attached to
	// the terminal, which will be the ssh subprocess at that
	// point.
	ctx.SetInterruptHandler(func() {})
	cloudcfg := coreCloudinit.New()
	if err := cloudinit.ConfigureJuju(machineConfig, cloudcfg); err != nil {
		return err
	}
	return sshinit.Configure("ubuntu@"+dnsName, cloudcfg)
}

// SSHTimeoutOpts lists the amount of time we will wait for various parts of
// the SSH connection to complete. This is similar to DialOpts, see
// http://pad.lv/1258889 about possibly deduplicating them.
type SSHTimeoutOpts struct {
	// Timeout is the amount of time to wait contacting
	// a state server.
	Timeout time.Duration

	// DNSNameDelay is the amount of time between refreshing the DNS name
	DNSNameDelay time.Duration
}

// DefaultBootstrapSSHTimeout is the time we'll wait for SSH to come up on the bootstrap node
func DefaultBootstrapSSHTimeout() SSHTimeoutOpts {
	return SSHTimeoutOpts{
		Timeout:      10 * time.Minute,
		DNSNameDelay: 1 * time.Second,
	}
}

type dnsNamer interface {
	// DNSName returns the DNS name for the instance.
	// If the name is not yet allocated, it will return
	// an ErrNoDNSName error.
	DNSName() (string, error)
}

// waitSSH waits for the instance to be assigned a DNS
// entry, then waits until we can connect to it via SSH.
func waitSSH(ctx *BootstrapContext, inst dnsNamer, t *tomb.Tomb, timeout SSHTimeoutOpts) (dnsName string, err error) {
	defer t.Done()
	globalTimeout := time.After(timeout.Timeout)
	pollDNS := time.NewTimer(0)
	fmt.Fprintln(ctx.Stderr, "Waiting for DNS name")
	var dialResultChan chan error
	var lastErr error
	for {
		if dnsName != "" && dialResultChan == nil {
			addr := dnsName + ":22"
			fmt.Fprintf(ctx.Stderr, "Attempting to connect to %s\n", addr)
			dialResultChan = make(chan error, 1)
			go func() {
				c, err := net.Dial("tcp", addr)
				if err == nil {
					c.Close()
				}
				dialResultChan <- err
			}()
		}
		select {
		case <-pollDNS.C:
			pollDNS.Reset(timeout.DNSNameDelay)
			newDNSName, err := inst.DNSName()
			if err != nil && err != instance.ErrNoDNSName {
				return "", t.Killf("getting DNS name: %v", err)
			} else if err != nil {
				lastErr = err
			} else if newDNSName != dnsName {
				dnsName = newDNSName
				dialResultChan = nil
			}
		case lastErr = <-dialResultChan:
			if lastErr == nil {
				return dnsName, nil
			}
			logger.Debugf("connection failed: %v", lastErr)
			dialResultChan = nil // retry
		case <-globalTimeout:
			format := "waited for %v "
			args := []interface{}{timeout.Timeout}
			if dnsName == "" {
				format += "without getting a DNS name"
			} else {
				format += "without being able to connect to %q"
				args = append(args, dnsName)
			}
			if lastErr != nil {
				format += ": %v"
				args = append(args, lastErr)
			}
			return "", t.Killf(format, args...)
		case <-t.Dying():
			return "", t.Err()
		}
	}
}

// TODO(axw) move this to environs; see
// comment near the top of common.Bootstrap.
type BootstrapContext struct {
	once        sync.Once
	handlerchan chan func()

	Stderr io.Writer
}

func (ctx *BootstrapContext) SetInterruptHandler(f func()) {
	ctx.once.Do(ctx.initHandler)
	ctx.handlerchan <- f
}

func (ctx *BootstrapContext) initHandler() {
	ctx.handlerchan = make(chan func())
	go ctx.handleInterrupt()
}

func (ctx *BootstrapContext) handleInterrupt() {
	signalchan := make(chan os.Signal, 1)
	var s chan os.Signal
	var handler func()
	for {
		select {
		case handler = <-ctx.handlerchan:
			if handler == nil {
				if s != nil {
					signal.Stop(signalchan)
					s = nil
				}
			} else {
				if s == nil {
					s = signalchan
					signal.Notify(signalchan, os.Interrupt)
				}
			}
		case <-s:
			handler()
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
