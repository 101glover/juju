package environs

import (
	"errors"
	"io"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/state"
	"launchpad.net/juju-core/state/api"
)

// A EnvironProvider represents a computing and storage provider.
type EnvironProvider interface {
	// Open opens the environment and returns it.
	Open(cfg *config.Config) (Environ, error)

	// Validate ensures that config is a valid configuration for this
	// provider, applying changes to it if necessary, and returns the
	// validated configuration.
	// If old is not nil, it holds the previous environment configuration
	// for consideration when validating changes.
	Validate(cfg, old *config.Config) (valid *config.Config, err error)

	// Boilerplate returns a default configuration for the environment in yaml format.
	// The text should be a key followed by some number of attributes:
	//    `environName:
	//        type: environTypeName
	//        attr1: val1
	//    `
	// The text is used as a template (see the template package) with one extra template
	// function available, rand, which expands to a random hexadecimal string when invoked.
	BoilerplateConfig() string

	// SecretAttrs filters the supplied configuration returning only values
	// which are considered sensitive.
	SecretAttrs(cfg *config.Config) (map[string]interface{}, error)

	// PublicAddress returns this machine's public host name.
	PublicAddress() (string, error)

	// PrivateAddress returns this machine's private host name.
	PrivateAddress() (string, error)

	// InstanceId returns this machine's instance id.
	InstanceId() (state.InstanceId, error)
}

var ErrNoDNSName = errors.New("DNS name not allocated")

// Instance represents the provider-specific notion of a machine.
type Instance interface {
	// Id returns a provider-generated identifier for the Instance.
	Id() state.InstanceId

	// DNSName returns the DNS name for the instance.
	// If the name is not yet allocated, it will return
	// an ErrNoDNSName error.
	DNSName() (string, error)

	// WaitDNSName returns the DNS name for the instance,
	// waiting until it is allocated if necessary.
	WaitDNSName() (string, error)

	// OpenPorts opens the given ports on the instance, which
	// should have been started with the given machine id.
	OpenPorts(machineId string, ports []state.Port) error

	// ClosePorts closes the given ports on the instance, which
	// should have been started with the given machine id.
	ClosePorts(machineId string, ports []state.Port) error

	// Ports returns the set of ports open on the instance, which
	// should have been started with the given machine id.
	// The ports are returned as sorted by state.SortPorts.
	Ports(machineId string) ([]state.Port, error)
}

var ErrNoInstances = errors.New("no instances found")
var ErrPartialInstances = errors.New("only some instances were found")

// NotFoundError records an error when something has not been found.
type NotFoundError struct {
	// error is the underlying error.
	error
}

// A StorageReader can retrieve and list files from a storage provider.
type StorageReader interface {
	// Get opens the given storage file and returns a ReadCloser
	// that can be used to read its contents.  It is the caller's
	// responsibility to close it after use.  If the name does not
	// exist, it should return a *NotFoundError.
	Get(name string) (io.ReadCloser, error)

	// List lists all names in the storage with the given prefix, in
	// alphabetical order.  The names in the storage are considered
	// to be in a flat namespace, so the prefix may include slashes
	// and the names returned are the full names for the matching
	// entries.
	List(prefix string) ([]string, error)

	// URL returns a URL that can be used to access the given storage file.
	URL(name string) (string, error)
}

// A StorageWriter adds and removes files in a storage provider.
type StorageWriter interface {
	// Put reads from r and writes to the given storage file.
	// The length must give the total length of the file.
	Put(name string, r io.Reader, length int64) error

	// Remove removes the given file from the environment's
	// storage. It should not return an error if the file does
	// not exist.
	Remove(name string) error
}

// Storage represents storage that can be both
// read and written.
type Storage interface {
	StorageReader
	StorageWriter
}

// An Environ represents a juju environment as specified
// in the environments.yaml file.
//
// Due to the limitations of some providers (for example ec2), the
// results of the Environ methods may not be fully sequentially
// consistent. In particular, while a provider may retry when it
// gets an error for an operation, it will not retry when
// an operation succeeds, even if that success is not
// consistent with a previous operation.
//
type Environ interface {
	// Name returns the Environ's name.
	Name() string

	// Bootstrap initializes the state for the environment, possibly
	// starting one or more instances.  If the configuration's
	// AdminSecret is non-empty, the adminstrator password on the
	// newly bootstrapped state will be set to a hash of it (see
	// trivial.PasswordHash), When first connecting to the
	// environment via the juju package, the password hash will be
	// automatically replaced by the real password.
	//
	// The supplied constraints are used to choose the initial instance
	// specification, and will be stored in the new environment's state.
	//
	// The stateServerCertand stateServerKey parameters hold
	// both the certificate and the respective private key to be
	// used by the initial state server, in PEM format.
	Bootstrap(cons state.Constraints, stateServerCert, stateServerKey []byte) error

	// StateInfo returns information on the state initialized
	// by Bootstrap.
	StateInfo() (*state.Info, *api.Info, error)

	// Config returns the current configuration of this Environ.
	Config() *config.Config

	// SetConfig updates the Environs configuration.
	// Calls to SetConfig do not affect the configuration of
	// values previously obtained from Storage and PublicStorage.
	SetConfig(cfg *config.Config) error

	// StartInstance asks for a new instance to be created,
	// associated with the provided machine identifier.  The given
	// info describes the juju state for the new instance to connect
	// to.  Using the same machine id as another running instance
	// can lead to undefined results. The juju tools that will run
	// on the new machine are given by tools - if nil,
	// the Environ will find a set of tools compatible with the
	// current version.
	StartInstance(machineId string, cons state.Constraints, info *state.Info, apiInfo *api.Info, tools *state.Tools) (Instance, error)

	// StopInstances shuts down the given instances.
	StopInstances([]Instance) error

	// Instances returns a slice of instances corresponding to the
	// given instance ids.  If no instances were found, but there
	// was no other error, it will return ErrNoInstances.  If
	// some but not all the instances were found, the returned slice
	// will have some nil slots, and an ErrPartialInstances error
	// will be returned.
	Instances(ids []state.InstanceId) ([]Instance, error)

	// AllInstances returns all instances currently known to the
	// environment.
	AllInstances() ([]Instance, error)

	// Storage returns storage specific to the environment.
	Storage() Storage

	// PublicStorage returns storage shared between environments.
	PublicStorage() StorageReader

	// Destroy shuts down all known machines and destroys the
	// rest of the environment. A list of instances known to
	// be part of the environment can be given with insts.
	// This is because recently started machines might not
	// yet be visible in the environment, so this method
	// can wait until they are.
	//
	// When Destroy has been called, any Environ referring to the
	// same remote environment may become invalid
	Destroy(insts []Instance) error

	// AssignmentPolicy returns the environment's unit assignment policy.
	AssignmentPolicy() state.AssignmentPolicy

	// OpenPorts opens the given ports for the whole environment.
	// Must only be used if the environment was setup with the
	// FwGlobal firewall mode.
	OpenPorts(ports []state.Port) error

	// ClosePorts closes the given ports for the whole environment.
	// Must only be used if the environment was setup with the
	// FwGlobal firewall mode.
	ClosePorts(ports []state.Port) error

	// Ports returns the ports opened for the whole environment.
	// Must only be used if the environment was setup with the
	// FwGlobal firewall mode.
	Ports() ([]state.Port, error)

	// Provider returns the EnvironProvider that created this Environ.
	Provider() EnvironProvider
}
