// Copyright 2015 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package controller

import (
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/juju/cmd"
	"github.com/juju/errors"
	"github.com/juju/gnuflag"
	"gopkg.in/juju/names.v2"

	"github.com/juju/juju/api/base"
	"github.com/juju/juju/apiserver/params"
	"github.com/juju/juju/cmd/juju/common"
	"github.com/juju/juju/cmd/modelcmd"
	"github.com/juju/juju/cmd/output"
	"github.com/juju/juju/jujuclient"
)

// NewListModelsCommand returns a command to list models.
func NewListModelsCommand() cmd.Command {
	return modelcmd.WrapController(&modelsCommand{})
}

// modelsCommand returns the list of all the models the
// current user can access on the current controller.
type modelsCommand struct {
	modelcmd.ControllerCommandBase
	out          cmd.Output
	all          bool
	loggedInUser string
	user         string
	listUUID     bool
	exactTime    bool
	modelAPI     ModelManagerAPI
	sysAPI       ModelsSysAPI
	oldAPI       bool
}

var listModelsDoc = `
The models listed here are either models you have created yourself, or
models which have been shared with you. Default values for user and
controller are, respectively, the current user and the current controller.
The active model is denoted by an asterisk.

Examples:

    juju models
    juju models --user bob

See also:
    add-model
    share-model
    unshare-model
`

// ModelManagerAPI defines the methods on the model manager API that
// the models command calls.
type ModelManagerAPI interface {
	Close() error
	ListModels(user string) ([]base.UserModel, error)
	ListModelsWithInfo(user names.UserTag, includeUsersAndMachines bool) ([]params.ModelInfoResult, error)
	ModelInfo([]names.ModelTag) ([]params.ModelInfoResult, error)
	BestAPIVersion() int
}

// ModelsSysAPI defines the methods on the controller manager API that the
// list models command calls.
type ModelsSysAPI interface {
	Close() error
	AllModels() ([]base.UserModel, error)
}

// Info implements Command.Info
func (c *modelsCommand) Info() *cmd.Info {
	return &cmd.Info{
		Name:    "models",
		Purpose: "Lists models a user can access on a controller.",
		Doc:     listModelsDoc,
		Aliases: []string{"list-models"},
	}
}

func (c *modelsCommand) getModelManagerAPI() (ModelManagerAPI, error) {
	if c.modelAPI != nil {
		return c.modelAPI, nil
	}
	return c.NewModelManagerAPIClient()
}

func (c *modelsCommand) getSysAPI() (ModelsSysAPI, error) {
	if c.sysAPI != nil {
		return c.sysAPI, nil
	}
	return c.NewControllerAPIClient()
}

// SetFlags implements Command.SetFlags.
func (c *modelsCommand) SetFlags(f *gnuflag.FlagSet) {
	c.ControllerCommandBase.SetFlags(f)
	f.StringVar(&c.user, "user", "", "The user to list models for (administrative users only)")
	f.BoolVar(&c.all, "all", false, "Lists all models, regardless of user accessibility (administrative users only)")
	f.BoolVar(&c.listUUID, "uuid", false, "Display UUID for models")
	f.BoolVar(&c.exactTime, "exact-time", false, "Use full timestamps")
	f.BoolVar(&c.oldAPI, "oldapi", false, "Use the old API to compare")
	c.out.AddFlags(f, "tabular", map[string]cmd.Formatter{
		"yaml":    cmd.FormatYaml,
		"json":    cmd.FormatJson,
		"tabular": c.formatTabular,
	})
}

// ModelSet contains the set of models known to the client,
// and UUID of the current model.
type ModelSet struct {
	Models []common.ModelInfo `yaml:"models" json:"models"`

	// CurrentModel is the name of the current model, qualified for the
	// user for which we're listing models. i.e. for the user admin,
	// and the model admin/foo, this field will contain "foo"; for
	// bob and the same model, the field will contain "admin/foo".
	CurrentModel string `yaml:"current-model,omitempty" json:"current-model,omitempty"`

	// CurrentModelQualified is the fully qualified name for the current
	// model, i.e. having the format $owner/$model.
	CurrentModelQualified string `yaml:"-" json:"-"`
}

// Run implements Command.Run
func (c *modelsCommand) Run(ctx *cmd.Context) error {
	controllerName, err := c.ControllerName()
	if err != nil {
		return errors.Trace(err)
	}
	accountDetails, err := c.CurrentAccountDetails()
	if err != nil {
		return err
	}
	c.loggedInUser = accountDetails.User

	if c.user == "" {
		c.user = accountDetails.User
	}
	if !names.IsValidUser(c.user) {
		return errors.NotValidf("user %q", c.user)
	}

	now := time.Now()

	modelmanagerAPI, err := c.getModelManagerAPI()
	if err != nil {
		return errors.Trace(err)
	}
	defer modelmanagerAPI.Close()

	var modelInfo []common.ModelInfo
	// TODO (anastasiamac 2017-11-6) oldAPI will need to be removed before merging into develop.
	if !c.oldAPI && modelmanagerAPI.BestAPIVersion() > 3 {
		// New code path
		modelInfo, err = c.getNewModelInfo(ctx, modelmanagerAPI, controllerName, now)
		if err != nil {
			return errors.Annotate(err, "unable to get model details")
		}
	} else {
		// First get a list of the models.
		var models []base.UserModel
		if c.all {
			models, err = c.getAllModels()
		} else {
			models, err = c.getUserModels(modelmanagerAPI)
		}
		if err != nil {
			return errors.Annotate(err, "cannot list models")
		}

		// TODO(perrito666) 2016-05-02 lp:1558657
		// And now get the full details of the models.
		modelInfo, err = c.getModelInfo(modelmanagerAPI, controllerName, now, models)
		if err != nil {
			return errors.Annotate(err, "cannot get model details")
		}
	}
	// update client store here too...
	modelsToStore := make(map[string]jujuclient.ModelDetails, len(modelInfo))
	for _, model := range modelInfo {
		modelsToStore[model.Name] = jujuclient.ModelDetails{model.UUID}
	}
	if err := c.ClientStore().SetModels(controllerName, modelsToStore); err != nil {
		return errors.Trace(err)
	}

	modelSet := ModelSet{Models: modelInfo}
	current, err := c.ClientStore().CurrentModel(controllerName)
	if err == nil {
		// It is not a problem if we could not get current model -
		// it could have been destroyed since last time we've used this client.
		// However, if we do find it, we'd want to mark it in the output.
		modelSet.CurrentModelQualified = current
		modelSet.CurrentModel = current
		if c.user != "" {
			userForListing := names.NewUserTag(c.user)
			unqualifiedModelName, owner, err := jujuclient.SplitModelName(current)
			if err == nil {
				modelSet.CurrentModel = common.OwnerQualifiedModelName(
					unqualifiedModelName, owner, userForListing,
				)
			}
		}
	}

	if err := c.out.Write(ctx, modelSet); err != nil {
		return err
	}
	if len(modelInfo) == 0 && c.out.Name() == "tabular" {
		// When the output is tabular, we inform the user when there
		// are no models available, and tell them how to go about
		// creating or granting access to them.
		fmt.Fprintln(ctx.Stderr, noModelsMessage)
	}
	return nil
}

func (c *modelsCommand) getNewModelInfo(ctx *cmd.Context, client ModelManagerAPI, controllerName string, now time.Time) ([]common.ModelInfo, error) {
	// Tabular format does not display model machines' nor model users' details.
	includeUsersAndMachines := c.out.Name() != "tabular"
	results, err := client.ListModelsWithInfo(names.NewUserTag(c.user), includeUsersAndMachines)
	if err != nil {
		return nil, errors.Trace(err)
	}
	info := []common.ModelInfo{}
	for _, result := range results {
		// Since we do not want to throw away all results if we have an
		// an issue with a model, we will display errors in Stderr
		// and will continue processing the rest.
		if result.Error != nil {
			ctx.Infof(result.Error.Error())
			continue
		}
		model, err := common.ModelInfoFromParams(*result.Result, now)
		if err != nil {
			ctx.Infof(err.Error())
			continue
		}
		model.ControllerName = controllerName
		info = append(info, model)
	}
	return info, nil
}

func (c *modelsCommand) getModelInfo(client ModelManagerAPI, controllerName string, now time.Time, userModels []base.UserModel) ([]common.ModelInfo, error) {
	tags := make([]names.ModelTag, len(userModels))
	for i, m := range userModels {
		tags[i] = names.NewModelTag(m.UUID)
	}
	results, err := client.ModelInfo(tags)
	if err != nil {
		return nil, errors.Trace(err)
	}

	info := []common.ModelInfo{}
	for i, result := range results {
		if result.Error != nil {
			if params.IsCodeUnauthorized(result.Error) {
				// If we get this, then the model was removed
				// between the initial listing and the call
				// to query its details.
				continue
			}
			return nil, errors.Annotatef(
				result.Error, "getting model %s (%q) info",
				userModels[i].UUID, userModels[i].Name,
			)
		}

		model, err := common.ModelInfoFromParams(*result.Result, now)
		if err != nil {
			return nil, errors.Trace(err)
		}
		model.ControllerName = controllerName
		info = append(info, model)
	}
	return info, nil
}

func (c *modelsCommand) getAllModels() ([]base.UserModel, error) {
	client, err := c.getSysAPI()
	if err != nil {
		return nil, errors.Trace(err)
	}
	defer client.Close()
	return client.AllModels()
}

func (c *modelsCommand) getUserModels(client ModelManagerAPI) ([]base.UserModel, error) {
	return client.ListModels(c.user)
}

// formatTabular takes an interface{} to adhere to the cmd.Formatter interface
func (c *modelsCommand) formatTabular(writer io.Writer, value interface{}) error {
	modelSet, ok := value.(ModelSet)
	if !ok {
		return errors.Errorf("expected value of type %T, got %T", modelSet, value)
	}
	// We need the tag of the user for which we're listing models,
	// and for the logged-in user. We use these below when formatting
	// the model display names.
	loggedInUser := names.NewUserTag(c.loggedInUser)
	userForLastConn := loggedInUser
	var currentUser names.UserTag
	if c.user != "" {
		currentUser = names.NewUserTag(c.user)
		userForLastConn = currentUser
	}

	tw := output.TabWriter(writer)
	w := output.Wrapper{tw}
	controllerName, err := c.ControllerName()
	if err != nil {
		return errors.Trace(err)
	}
	w.Println("Controller: " + controllerName)
	w.Println()
	w.Print("Model")
	if c.listUUID {
		w.Print("UUID")
	}
	// Only owners, or users with write access or above get to see machines and cores.
	haveMachineInfo := false
	for _, m := range modelSet.Models {
		if haveMachineInfo = len(m.Machines) > 0; haveMachineInfo {
			break
		}
	}
	if haveMachineInfo {
		w.Println("Cloud/Region", "Status", "Machines", "Cores", "Access", "Last connection")
		offset := 0
		if c.listUUID {
			offset++
		}
		tw.SetColumnAlignRight(3 + offset)
		tw.SetColumnAlignRight(4 + offset)
	} else {
		w.Println("Cloud/Region", "Status", "Access", "Last connection")
	}
	for _, model := range modelSet.Models {
		cloudRegion := strings.Trim(model.Cloud+"/"+model.CloudRegion, "/")
		owner := names.NewUserTag(model.Owner)
		name := model.Name
		if currentUser == owner {
			// No need to display fully qualified model name to its owner.
			name = model.ShortName
		}
		if model.Name == modelSet.CurrentModelQualified {
			name += "*"
			w.PrintColor(output.CurrentHighlight, name)
		} else {
			w.Print(name)
		}
		if c.listUUID {
			w.Print(model.UUID)
		}
		userForAccess := loggedInUser
		if c.user != "" {
			userForAccess = names.NewUserTag(c.user)
		}
		status := "-"
		if model.Status != nil {
			status = model.Status.Current.String()
		}
		w.Print(cloudRegion, status)
		if haveMachineInfo {
			machineInfo := fmt.Sprintf("%d", len(model.Machines))
			cores := uint64(0)
			for _, m := range model.Machines {
				cores += m.Cores
			}
			coresInfo := "-"
			if cores > 0 {
				coresInfo = fmt.Sprintf("%d", cores)
			}
			w.Print(machineInfo, coresInfo)
		}
		access := model.Users[userForAccess.Id()].Access
		if access == "" {
			access = "-"
		}
		lastConnection := model.Users[userForLastConn.Id()].LastConnection
		if lastConnection == "" {
			lastConnection = "never connected"
		}
		w.Println(access, lastConnection)
	}
	tw.Flush()
	return nil
}
