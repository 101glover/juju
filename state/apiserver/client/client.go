// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package client

import (
	"fmt"
	"launchpad.net/juju-core/charm"
	"launchpad.net/juju-core/juju"
	"launchpad.net/juju-core/state/api"
	"launchpad.net/juju-core/state/api/params"
	"launchpad.net/juju-core/state/statecmd"
)

type API struct {
	st *state.State
	auth common.Authorizer
	resources 
}

// NewAPI creates a new instance of the Machiner API.
func NewAPI(st *state.State, authorizer common.Authorizer) (*API, error) {
	return &API{st, authorizer}, nil
}

// Client returns an object that provides access
// to methods accessible to non-agent clients.
func (r *API) Client(id string) (*API, error) {
	if err := r.requireClient(); err != nil {
		return nil, err
	}
	if id != "" {
		// Safeguard id for possible future use.
		return nil, common.ErrBadId
	}
	return r.client, nil
}

// srvClient serves client-specific API methods.
type srvClient struct {
	root *srvRoot
}

func (c *srvClient) Status() (api.Status, error) {
	ms, err := c.root.srv.state.AllMachines()
	if err != nil {
		return api.Status{}, err
	}
	status := api.Status{
		Machines: make(map[string]api.MachineInfo),
	}
	for _, m := range ms {
		instId, _ := m.InstanceId()
		status.Machines[m.Id()] = api.MachineInfo{
			InstanceId: string(instId),
		}
	}
	return status, nil
}

func (c *srvClient) WatchAll() (params.AllWatcherId, error) {
	w := c.root.srv.state.Watch()
	return params.AllWatcherId{
		AllWatcherId: c.root.resources.Register(w),
	}, nil
}

// ServiceSet implements the server side of Client.ServerSet.
func (c *srvClient) ServiceSet(p params.ServiceSet) error {
	svc, err := c.root.srv.state.Service(p.ServiceName)
	if err != nil {
		return err
	}
	ch, _, err := svc.Charm()
	if err != nil {
		return err
	}
	changes, err := ch.Config().ParseSettingsStrings(p.Options)
	if err != nil {
		return err
	}
	return svc.UpdateConfigSettings(changes)
}

// ServiceSetYAML implements the server side of Client.ServerSetYAML.
func (c *srvClient) ServiceSetYAML(p params.ServiceSetYAML) error {
	svc, err := c.root.srv.state.Service(p.ServiceName)
	if err != nil {
		return err
	}
	ch, _, err := svc.Charm()
	if err != nil {
		return err
	}
	changes, err := ch.Config().ParseSettingsYAML([]byte(p.Config), p.ServiceName)
	if err != nil {
		return err
	}
	return svc.UpdateConfigSettings(changes)
}

// ServiceGet returns the configuration for a service.
func (c *srvClient) ServiceGet(args params.ServiceGet) (params.ServiceGetResults, error) {
	return statecmd.ServiceGet(c.root.srv.state, args)
}

// Resolved implements the server side of Client.Resolved.
func (c *srvClient) Resolved(p params.Resolved) error {
	unit, err := c.root.srv.state.Unit(p.UnitName)
	if err != nil {
		return err
	}
	return unit.Resolve(p.Retry)
}

// ServiceExpose changes the juju-managed firewall to expose any ports that
// were also explicitly marked by units as open.
func (c *srvClient) ServiceExpose(args params.ServiceExpose) error {
	return statecmd.ServiceExpose(c.root.srv.state, args)
}

// ServiceUnexpose changes the juju-managed firewall to unexpose any ports that
// were also explicitly marked by units as open.
func (c *srvClient) ServiceUnexpose(args params.ServiceUnexpose) error {
	return statecmd.ServiceUnexpose(c.root.srv.state, args)
}

var CharmStore charm.Repository = charm.Store

// ServiceDeploy fetches the charm from the charm store and deploys it. Local
// charms are not supported.
func (c *srvClient) ServiceDeploy(args params.ServiceDeploy) error {
	curl, err := charm.ParseURL(args.CharmUrl)
	if err != nil {
		return err
	}
	if curl.Schema != "cs" {
		return fmt.Errorf(`charm url has unsupported schema %q`, curl.Schema)
	}
	if curl.Revision < 0 {
		return fmt.Errorf("charm url must include revision")
	}
	conn, err := juju.NewConnFromState(c.root.srv.state)
	if err != nil {
		return err
	}
	ch, err := conn.PutCharm(curl, CharmStore, false)
	if err != nil {
		return err
	}
	var settings charm.Settings
	if len(args.ConfigYAML) > 0 {
		settings, err = ch.Config().ParseSettingsYAML([]byte(args.ConfigYAML), args.ServiceName)
	} else if len(args.Config) > 0 {
		settings, err = ch.Config().ParseSettingsStrings(args.Config)
	}
	if err != nil {
		return err
	}
	_, err = conn.DeployService(juju.DeployServiceParams{
		ServiceName:    args.ServiceName,
		Charm:          ch,
		NumUnits:       args.NumUnits,
		ConfigSettings: settings,
		Constraints:    args.Constraints,
		ForceMachineId: args.ForceMachineId,
	})
	return err
}

// ServiceSetCharm sets the charm for a given service.
func (c *srvClient) ServiceSetCharm(args params.ServiceSetCharm) error {
	service, err := c.root.srv.state.Service(args.ServiceName)
	if err != nil {
		return err
	}
	curl, err := charm.ParseURL(args.CharmUrl)
	if err != nil {
		return err
	}
	if curl.Schema != "cs" {
		return fmt.Errorf(`charm url has unsupported schema %q`, curl.Schema)
	}
	if curl.Revision < 0 {
		return fmt.Errorf("charm url must include revision")
	}
	conn, err := juju.NewConnFromState(c.root.srv.state)
	if err != nil {
		return err
	}
	ch, err := conn.PutCharm(curl, CharmStore, false)
	if err != nil {
		return err
	}
	return service.SetCharm(ch, args.Force)
}

// AddServiceUnits adds a given number of units to a service.
func (c *srvClient) AddServiceUnits(args params.AddServiceUnits) (params.AddServiceUnitsResults, error) {
	units, err := statecmd.AddServiceUnits(c.root.srv.state, args)
	if err != nil {
		return params.AddServiceUnitsResults{}, err
	}
	unitNames := make([]string, len(units))
	for i, unit := range units {
		unitNames[i] = unit.String()
	}
	return params.AddServiceUnitsResults{Units: unitNames}, nil
}

// DestroyServiceUnits removes a given set of service units.
func (c *srvClient) DestroyServiceUnits(args params.DestroyServiceUnits) error {
	return statecmd.DestroyServiceUnits(c.root.srv.state, args)
}

// ServiceDestroy destroys a given service.
func (c *srvClient) ServiceDestroy(args params.ServiceDestroy) error {
	return statecmd.ServiceDestroy(c.root.srv.state, args)
}

// GetServiceConstraints returns the constraints for a given service.
func (c *srvClient) GetServiceConstraints(args params.GetServiceConstraints) (params.GetServiceConstraintsResults, error) {
	return statecmd.GetServiceConstraints(c.root.srv.state, args)
}

// SetServiceConstraints sets the constraints for a given service.
func (c *srvClient) SetServiceConstraints(args params.SetServiceConstraints) error {
	return statecmd.SetServiceConstraints(c.root.srv.state, args)
}

// AddRelation adds a relation between the specified endpoints and returns the relation info.
func (c *srvClient) AddRelation(args params.AddRelation) (params.AddRelationResults, error) {
	return statecmd.AddRelation(c.root.srv.state, args)
}

// DestroyRelation removes the relation between the specified endpoints.
func (c *srvClient) DestroyRelation(args params.DestroyRelation) error {
	return statecmd.DestroyRelation(c.root.srv.state, args)
}

// CharmInfo returns information about the requested charm.
func (c *srvClient) CharmInfo(args params.CharmInfo) (api.CharmInfo, error) {
	curl, err := charm.ParseURL(args.CharmURL)
	if err != nil {
		return api.CharmInfo{}, err
	}
	charm, err := c.root.srv.state.Charm(curl)
	if err != nil {
		return api.CharmInfo{}, err
	}
	info := api.CharmInfo{
		Revision: charm.Revision(),
		URL:      curl.String(),
		Config:   charm.Config(),
		Meta:     charm.Meta(),
	}
	return info, nil
}

// EnvironmentInfo returns information about the current environment (default
// series and type).
func (c *srvClient) EnvironmentInfo() (api.EnvironmentInfo, error) {
	conf, err := c.root.srv.state.EnvironConfig()
	if err != nil {
		return api.EnvironmentInfo{}, err
	}
	info := api.EnvironmentInfo{
		DefaultSeries: conf.DefaultSeries(),
		ProviderType:  conf.Type(),
		Name:          conf.Name(),
	}
	return info, nil
}

// GetAnnotations returns annotations about a given entity.
func (c *srvClient) GetAnnotations(args params.GetAnnotations) (params.GetAnnotationsResults, error) {
	entity, err := c.root.srv.state.Annotator(args.Tag)
	if err != nil {
		return params.GetAnnotationsResults{}, err
	}
	ann, err := entity.Annotations()
	if err != nil {
		return params.GetAnnotationsResults{}, err
	}
	return params.GetAnnotationsResults{Annotations: ann}, nil
}

// SetAnnotations stores annotations about a given entity.
func (c *srvClient) SetAnnotations(args params.SetAnnotations) error {
	entity, err := c.root.srv.state.Annotator(args.Tag)
	if err != nil {
		return err
	}
	return entity.SetAnnotations(args.Pairs)
}
