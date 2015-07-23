// Copyright 2015 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package context

import (
	"github.com/juju/cmd"
	"github.com/juju/errors"

	"github.com/juju/juju/process"
)

// RegisterCommandInfo is the info for the proc-launch command.
var RegisterCommandInfo = cmdInfo{
	Name:      "proc-register",
	ExtraArgs: []string{"proc-details"},
	Summary:   "register a workload process",
	Doc: `
"register" is used while a hook is running to let Juju know that
a workload process has been manually started. The information used
to start the process must be provided when "register" is run.

The process name must correspond to one of the processes defined in
the charm's metadata.yaml.
`,
}

// ProcRegistrationCommand implements the register command.
type ProcRegistrationCommand struct {
	registeringCommand
}

// NewProcRegistrationCommand returns a new ProcRegistrationCommand.
func NewProcRegistrationCommand(ctx HookContext) (*ProcRegistrationCommand, error) {
	base, err := newRegisteringCommand(ctx)
	if err != nil {
		return nil, errors.Trace(err)
	}
	c := &ProcRegistrationCommand{
		registeringCommand: *base,
	}
	c.cmdInfo = RegisterCommandInfo
	return c, nil
}

// Init implements cmd.Command.
func (c *ProcRegistrationCommand) Init(args []string) error {
	if len(args) != 2 {
		return errors.Errorf("expected <name> <proc-details>, got: %v", args)
	}
	return c.init(args[0], args[1])
}

func (c *ProcRegistrationCommand) init(name, detailsStr string) error {
	if err := c.registeringCommand.init(name); err != nil {
		return errors.Trace(err)
	}

	details, err := process.UnmarshalDetails([]byte(detailsStr))
	if err != nil {
		return errors.Trace(err)
	}
	c.Details = details

	return nil
}

// Run implements cmd.Command.
func (c *ProcRegistrationCommand) Run(ctx *cmd.Context) error {
	// TODO(wwitzel3) should charmer have direct access to pInfo.Status?
	if err := c.register(ctx); err != nil {
		return errors.Trace(err)
	}
	return nil
}
