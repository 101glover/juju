package juju

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"io/ioutil"
	"launchpad.net/juju-core/charm"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/log"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/trivial"
	"net/url"
	"os"
	"time"
)

// Conn holds a connection to a juju environment and its
// associated state.
type Conn struct {
	Environ environs.Environ
	State   *state.State
}

var redialStrategy = trivial.AttemptStrategy{
	Total: 60 * time.Second,
	Delay: 250 * time.Millisecond,
}

// NewConn returns a new Conn that uses the
// given environment. The environment must have already
// been bootstrapped.
func NewConn(environ environs.Environ) (*Conn, error) {
	info, _, err := environ.StateInfo()
	if err != nil {
		return nil, err
	}
	password := environ.Config().AdminSecret()
	if password == "" {
		return nil, fmt.Errorf("cannot connect without admin-secret")
	}
	info.Password = password
	st, err := state.Open(info)
	if err == state.ErrUnauthorized {
		// We can't connect with the administrator password,;
		// perhaps this was the first connection and the
		// password has not been changed yet.
		info.Password = trivial.PasswordHash(password)

		// We try for a while because we might succeed in
		// connecting to mongo before the state has been
		// initialized and the initial password set.
		for a := redialStrategy.Start(); a.Next(); {
			st, err = state.Open(info)
			if err != state.ErrUnauthorized {
				break
			}
		}
		if err != nil {
			return nil, err
		}
		if err := st.SetAdminMongoPassword(password); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}
	conn := &Conn{
		Environ: environ,
		State:   st,
	}
	if err := conn.updateSecrets(); err != nil {
		conn.Close()
		return nil, fmt.Errorf("unable to push secrets: %v", err)
	}
	return conn, nil
}

// NewConnFromName returns a Conn pointing at the environName environment, or the
// default environment if not specified.
func NewConnFromName(environName string) (*Conn, error) {
	environ, err := environs.NewFromName(environName)
	if err != nil {
		return nil, err
	}
	return NewConn(environ)
}

// Close terminates the connection to the environment and releases
// any associated resources.
func (c *Conn) Close() error {
	return c.State.Close()
}

// updateSecrets writes secrets into the environment when there are none.
// This is done because environments such as ec2 offer no way to securely
// deliver the secrets onto the machine, so the bootstrap is done with the
// whole environment configuration but without secrets, and then secrets
// are delivered on the first communication with the running environment.
func (c *Conn) updateSecrets() error {
	secrets, err := c.Environ.Provider().SecretAttrs(c.Environ.Config())
	if err != nil {
		return err
	}
	cfg, err := c.State.EnvironConfig()
	if err != nil {
		return err
	}
	attrs := cfg.AllAttrs()
	for k := range secrets {
		if _, exists := attrs[k]; exists {
			// Environment already has secrets. Won't send again.
			return nil
		}
	}
	cfg, err = cfg.Apply(secrets)
	if err != nil {
		return err
	}
	return c.State.SetEnvironConfig(cfg)
}

// AddService creates a new service with the given name to run the given
// charm.  If svcName is empty, the charm name will be used.
func (conn *Conn) AddService(name string, ch *state.Charm) (*state.Service, error) {
	if name == "" {
		name = ch.URL().Name // TODO ch.Meta().Name ?
	}
	svc, err := conn.State.AddService(name, ch)
	if err != nil {
		return nil, err
	}
	meta := ch.Meta()
	for rname, rel := range meta.Peers {
		ep := state.Endpoint{
			name,
			rel.Interface,
			rname,
			state.RolePeer,
			rel.Scope,
		}
		if _, err := conn.State.AddRelation(ep); err != nil {
			return nil, fmt.Errorf("cannot add peer relation %q to service %q: %v", rname, name, err)
		}
	}
	return svc, nil
}

// PutCharm uploads the given charm to provider storage, and adds a
// state.Charm to the state.  The charm is not uploaded if a charm with
// the same URL already exists in the state.
// If bumpRevision is true, the charm must be a local directory,
// and the revision number will be incremented before pushing.
func (conn *Conn) PutCharm(curl *charm.URL, repo charm.Repository, bumpRevision bool) (*state.Charm, error) {
	if curl.Revision == -1 {
		rev, err := repo.Latest(curl)
		if err != nil {
			return nil, fmt.Errorf("cannot get latest charm revision: %v", err)
		}
		curl = curl.WithRevision(rev)
	}
	ch, err := repo.Get(curl)
	if err != nil {
		return nil, fmt.Errorf("cannot get charm: %v", err)
	}
	if bumpRevision {
		chd, ok := ch.(*charm.Dir)
		if !ok {
			return nil, fmt.Errorf("cannot increment version of charm %q: not a directory", curl)
		}
		if err = chd.SetDiskRevision(chd.Revision() + 1); err != nil {
			return nil, fmt.Errorf("cannot increment version of charm %q: %v", curl, err)
		}
		curl = curl.WithRevision(chd.Revision())
	}
	if sch, err := conn.State.Charm(curl); err == nil {
		return sch, nil
	}
	return conn.addCharm(curl, ch)
}

func (conn *Conn) addCharm(curl *charm.URL, ch charm.Charm) (*state.Charm, error) {
	var f *os.File
	name := charm.Quote(curl.String())
	switch ch := ch.(type) {
	case *charm.Dir:
		var err error
		if f, err = ioutil.TempFile("", name); err != nil {
			return nil, err
		}
		defer os.Remove(f.Name())
		defer f.Close()
		err = ch.BundleTo(f)
		if err != nil {
			return nil, fmt.Errorf("cannot bundle charm: %v", err)
		}
		if _, err := f.Seek(0, 0); err != nil {
			return nil, err
		}
	case *charm.Bundle:
		var err error
		if f, err = os.Open(ch.Path); err != nil {
			return nil, fmt.Errorf("cannot read charm bundle: %v", err)
		}
		defer f.Close()
	default:
		return nil, fmt.Errorf("unknown charm type %T", ch)
	}
	h := sha256.New()
	size, err := io.Copy(h, f)
	if err != nil {
		return nil, err
	}
	digest := hex.EncodeToString(h.Sum(nil))
	if _, err := f.Seek(0, 0); err != nil {
		return nil, err
	}
	storage := conn.Environ.Storage()
	log.Printf("writing charm to storage [%d bytes]", size)
	if err := storage.Put(name, f, size); err != nil {
		return nil, fmt.Errorf("cannot put charm: %v", err)
	}
	ustr, err := storage.URL(name)
	if err != nil {
		return nil, fmt.Errorf("cannot get storage URL for charm: %v", err)
	}
	u, err := url.Parse(ustr)
	if err != nil {
		return nil, fmt.Errorf("cannot parse storage URL: %v", err)
	}
	log.Printf("adding charm to state")
	sch, err := conn.State.AddCharm(ch, curl, u, digest)
	if err != nil {
		return nil, fmt.Errorf("cannot add charm: %v", err)
	}
	return sch, nil
}

// AddUnits starts n units of the given service and allocates machines
// to them as necessary.
func (conn *Conn) AddUnits(svc *state.Service, n int) ([]*state.Unit, error) {
	units := make([]*state.Unit, n)
	// TODO what do we do if we fail half-way through this process?
	for i := 0; i < n; i++ {
		policy := conn.Environ.AssignmentPolicy()
		unit, err := svc.AddUnit()
		if err != nil {
			return nil, fmt.Errorf("cannot add unit %d/%d to service %q: %v", i+1, n, svc.Name(), err)
		}
		if err := conn.State.AssignUnit(unit, policy); err != nil {
			return nil, err
		}
		units[i] = unit
	}
	return units, nil
}

// DestroyUnits removes the specified units from the state.
func (conn *Conn) DestroyUnits(names ...string) (err error) {
	defer trivial.ErrorContextf(&err, "cannot destroy units")
	var units []*state.Unit
	for _, name := range names {
		unit, err := conn.State.Unit(name)
		switch {
		case state.IsNotFound(err):
			return fmt.Errorf("unit %q is not alive", name)
		case err != nil:
			return err
		case unit.Life() != state.Alive:
			return fmt.Errorf("unit %q is not alive", name)
		case unit.IsPrincipal():
			units = append(units, unit)
		default:
			return fmt.Errorf("unit %q is a subordinate", name)
		}
	}
	for _, unit := range units {
		if err := unit.EnsureDying(); err != nil {
			return err
		}
	}
	return nil
}

// Resolved marks the unit as having had any previous state transition
// problems resolved, and informs the unit that it may attempt to
// reestablish normal workflow. The retryHooks parameter informs
// whether to attempt to reexecute previous failed hooks or to continue
// as if they had succeeded before.
func (conn *Conn) Resolved(unit *state.Unit, retryHooks bool) error {
	status, _, err := unit.Status()
	if err != nil {
		return err
	}
	if status != state.UnitError {
		return fmt.Errorf("unit %q is not in an error state", unit)
	}
	mode := state.ResolvedNoHooks
	if retryHooks {
		mode = state.ResolvedRetryHooks
	}
	return unit.SetResolved(mode)
}
