package jujuc

import (
	"fmt"
	"launchpad.net/gnuflag"
	"launchpad.net/juju-core/cmd"
	"strings"
)

// RelationSetCommand implements the relation-set command.
type RelationSetCommand struct {
	ctx        Context
	RelationId int
	Settings   map[string]string
}

func NewRelationSetCommand(ctx Context) cmd.Command {
	return &RelationSetCommand{ctx: ctx, Settings: map[string]string{}}
}

func (c *RelationSetCommand) Info() *cmd.Info {
	return &cmd.Info{
		"relation-set", "key=value [key=value ...]", "set relation settings", "",
	}
}

func (c *RelationSetCommand) SetFlags(f *gnuflag.FlagSet) {
	f.Var(newRelationIdValue(c.ctx, &c.RelationId), "r", "specify a relation by id")
}

func (c *RelationSetCommand) Init(f *gnuflag.FlagSet, args []string) error {
	if err := f.Parse(true, args); err != nil {
		return err
	}
	if c.RelationId == -1 {
		return fmt.Errorf("no relation id specified")
	}
	args = f.Args()
	if len(args) == 0 {
		return fmt.Errorf(`expected "key=value" parameters, got nothing`)
	}
	for _, kv := range args {
		parts := strings.SplitN(kv, "=", 2)
		if len(parts) != 2 || len(parts[0]) == 0 {
			return fmt.Errorf(`expected "key=value", got %q`, kv)
		}
		c.Settings[parts[0]] = parts[1]
	}
	return nil
}

func (c *RelationSetCommand) Run(ctx *cmd.Context) (err error) {
	r, found := c.ctx.Relation(c.RelationId)
	if !found {
		return fmt.Errorf("unknown relation id")
	}
	settings, err := r.Settings()
	for k, v := range c.Settings {
		if v != "" {
			settings.Set(k, v)
		} else {
			settings.Delete(k)
		}
	}
	return nil
}
