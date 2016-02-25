// Copyright 2016 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package state_test

import (
	"fmt"

	"github.com/juju/errors"
	jc "github.com/juju/testing/checkers"
	jujutxn "github.com/juju/txn"
	gc "gopkg.in/check.v1"

	"github.com/juju/juju/state"
)

// linkLayerDevicesStateSuite contains white-box tests for link-layer network
// devices, which include access to mongo.
type linkLayerDevicesStateSuite struct {
	ConnSuite

	machine *state.Machine

	otherState        *state.State
	otherStateMachine *state.Machine
}

var _ = gc.Suite(&linkLayerDevicesStateSuite{})

func (s *linkLayerDevicesStateSuite) SetUpTest(c *gc.C) {
	s.ConnSuite.SetUpTest(c)

	var err error
	s.machine, err = s.State.AddMachine("quantal", state.JobHostUnits)
	c.Assert(err, jc.ErrorIsNil)

	s.otherState = s.NewStateForModelNamed(c, "other-model")
	s.otherStateMachine, err = s.otherState.AddMachine("quantal", state.JobHostUnits)
	c.Assert(err, jc.ErrorIsNil)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesNoArgs(c *gc.C) {
	err := s.machine.AddLinkLayerDevices() // takes varargs, which includes none.
	expectedError := fmt.Sprintf("cannot add link-layer devices to machine %q: no devices to add", s.machine.Id())
	c.Assert(err, gc.ErrorMatches, expectedError)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesEmptyArgs(c *gc.C) {
	args := state.LinkLayerDeviceArgs{}
	s.assertAddLinkLayerDevicesReturnsNotValidError(c, args, "empty Name not valid")
}

func (s *linkLayerDevicesStateSuite) assertAddLinkLayerDevicesReturnsNotValidError(c *gc.C, args state.LinkLayerDeviceArgs, errorCauseMatches string) {
	err := s.assertAddLinkLayerDevicesFailsValidationForArgs(c, args, errorCauseMatches)
	c.Assert(err, jc.Satisfies, errors.IsNotValid)
}

func (s *linkLayerDevicesStateSuite) assertAddLinkLayerDevicesFailsValidationForArgs(c *gc.C, args state.LinkLayerDeviceArgs, errorCauseMatches string) error {
	expectedError := fmt.Sprintf("invalid device %q: %s", args.Name, errorCauseMatches)
	return s.assertAddLinkLayerDevicesFailsForArgs(c, args, expectedError)
}

func (s *linkLayerDevicesStateSuite) assertAddLinkLayerDevicesFailsForArgs(c *gc.C, args state.LinkLayerDeviceArgs, errorCauseMatches string) error {
	err := s.machine.AddLinkLayerDevices(args)
	expectedError := fmt.Sprintf("cannot add link-layer devices to machine %q: %s", s.machine.Id(), errorCauseMatches)
	c.Assert(err, gc.ErrorMatches, expectedError)
	return err
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesInvalidName(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name: "bad#name",
	}
	s.assertAddLinkLayerDevicesReturnsNotValidError(c, args, `Name "bad#name" not valid`)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesSameNameAndParentName(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:       "foo",
		ParentName: "foo",
	}
	s.assertAddLinkLayerDevicesReturnsNotValidError(c, args, `Name and ParentName must be different`)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesInvalidType(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name: "bar",
		Type: "bad type",
	}
	s.assertAddLinkLayerDevicesReturnsNotValidError(c, args, `Type "bad type" not valid`)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesInvalidParentName(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:       "eth0",
		ParentName: "bad#name",
	}
	s.assertAddLinkLayerDevicesReturnsNotValidError(c, args, `ParentName "bad#name" not valid`)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesInvalidMACAddress(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:       "eth0",
		Type:       state.EthernetDevice,
		MACAddress: "bad mac",
	}
	s.assertAddLinkLayerDevicesReturnsNotValidError(c, args, `MACAddress "bad mac" not valid`)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWhenMachineNotAliveOrGone(c *gc.C) {
	err := s.machine.EnsureDead()
	c.Assert(err, jc.ErrorIsNil)

	args := state.LinkLayerDeviceArgs{
		Name: "eth0",
		Type: state.EthernetDevice,
	}
	s.assertAddLinkLayerDevicesFailsForArgs(c, args, "machine not found or not alive")

	err = s.machine.Remove()
	c.Assert(err, jc.ErrorIsNil)

	s.assertAddLinkLayerDevicesFailsForArgs(c, args, "machine not found or not alive")
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWhenModelNotAlive(c *gc.C) {
	otherModel, err := s.otherState.Model()
	c.Assert(err, jc.ErrorIsNil)
	err = otherModel.Destroy()
	c.Assert(err, jc.ErrorIsNil)

	args := state.LinkLayerDeviceArgs{
		Name: "eth0",
		Type: state.EthernetDevice,
	}
	err = s.otherStateMachine.AddLinkLayerDevices(args)
	expectedError := fmt.Sprintf(
		"cannot add link-layer devices to machine %q: model %q is no longer alive",
		s.otherStateMachine.Id(), otherModel.Name(),
	)
	c.Assert(err, gc.ErrorMatches, expectedError)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithMissingParent(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:       "eth0",
		Type:       state.EthernetDevice,
		ParentName: "br-eth0",
	}
	err := s.assertAddLinkLayerDevicesFailsForArgs(c, args, `parent device "br-eth0" of device "eth0" not found`)
	c.Assert(err, jc.Satisfies, errors.IsNotFound)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesNoParentSuccess(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:        "eth0.42",
		MTU:         9000,
		ProviderID:  "eni-42",
		Type:        state.VLAN_8021QDevice,
		MACAddress:  "aa:bb:cc:dd:ee:f0",
		IsAutoStart: true,
		IsUp:        true,
	}
	s.assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, args)
}

func (s *linkLayerDevicesStateSuite) assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c *gc.C, args state.LinkLayerDeviceArgs) {
	s.assertMachineAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, s.machine, args, s.State.ModelUUID())
}

func (s *linkLayerDevicesStateSuite) assertMachineAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c *gc.C, machine *state.Machine, args state.LinkLayerDeviceArgs, modelUUID string) {
	err := machine.AddLinkLayerDevices(args)
	c.Assert(err, jc.ErrorIsNil)
	result, err := machine.LinkLayerDevice(args.Name)
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(result, gc.NotNil)

	s.checkAddedDeviceMatchesArgs(c, result, args)
	s.checkAddedDeviceMatchesMachineIDAndModelUUID(c, result, s.machine.Id(), modelUUID)
}

func (s *linkLayerDevicesStateSuite) checkAddedDeviceMatchesArgs(c *gc.C, addedDevice *state.LinkLayerDevice, args state.LinkLayerDeviceArgs) {
	c.Check(addedDevice.Name(), gc.Equals, args.Name)
	c.Check(addedDevice.MTU(), gc.Equals, args.MTU)
	c.Check(addedDevice.ProviderID(), gc.Equals, args.ProviderID)
	c.Check(addedDevice.Type(), gc.Equals, args.Type)
	c.Check(addedDevice.MACAddress(), gc.Equals, args.MACAddress)
	c.Check(addedDevice.IsAutoStart(), gc.Equals, args.IsAutoStart)
	c.Check(addedDevice.IsUp(), gc.Equals, args.IsUp)
	c.Check(addedDevice.ParentName(), gc.Equals, args.ParentName)
}

func (s *linkLayerDevicesStateSuite) checkAddedDeviceMatchesMachineIDAndModelUUID(c *gc.C, addedDevice *state.LinkLayerDevice, machineID, modelUUID string) {
	globalKey := fmt.Sprintf("m#%s#d#%s", machineID, addedDevice.Name())
	c.Check(addedDevice.DocID(), gc.Equals, modelUUID+":"+globalKey)
	c.Check(addedDevice.MachineID(), gc.Equals, machineID)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesNoProviderIDSuccess(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name: "eno0",
		Type: state.EthernetDevice,
	}
	s.assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, args)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithDuplicateProviderIDFailsInSameModel(c *gc.C) {
	args1 := state.LinkLayerDeviceArgs{
		Name:       "eth0.42",
		Type:       state.EthernetDevice,
		ProviderID: "42",
	}
	s.assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, args1)

	args2 := args1
	args2.Name = "br-eth0"
	err := s.assertAddLinkLayerDevicesFailsValidationForArgs(c, args2, `ProviderID\(s\) not unique: 42`)
	c.Assert(err, jc.Satisfies, state.IsProviderIDNotUniqueError)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithDuplicateNameAndProviderIDSucceedsInDifferentModels(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:       "eth0.42",
		Type:       state.EthernetDevice,
		ProviderID: "42",
	}
	s.assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, args)

	s.assertMachineAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, s.otherStateMachine, args, s.otherState.ModelUUID())
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithDuplicateNameAndEmptyProviderIDReturnsAlreadyExistsErrorInSameModel(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name: "eth0.42",
		Type: state.EthernetDevice,
	}
	s.assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, args)

	err := s.assertAddLinkLayerDevicesFailsForArgs(c, args, `device "eth0.42" already exists`)
	c.Assert(err, jc.Satisfies, errors.IsAlreadyExists)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithDuplicateNameAndProviderIDFailsInSameModel(c *gc.C) {
	args := state.LinkLayerDeviceArgs{
		Name:       "foo",
		Type:       state.EthernetDevice,
		ProviderID: "42",
	}
	s.assertAddLinkLayerDevicesSucceedsAndResultMatchesArgs(c, args)

	err := s.assertAddLinkLayerDevicesFailsValidationForArgs(c, args, `ProviderID\(s\) not unique: 42`)
	c.Assert(err, jc.Satisfies, state.IsProviderIDNotUniqueError)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesMultipleArgsWithSameNameFails(c *gc.C) {
	foo1 := state.LinkLayerDeviceArgs{
		Name: "foo",
		Type: state.BridgeDevice,
	}
	foo2 := state.LinkLayerDeviceArgs{
		Name: "foo",
		Type: state.EthernetDevice,
	}
	err := s.machine.AddLinkLayerDevices(foo1, foo2)
	c.Assert(err, gc.ErrorMatches, `.*invalid device "foo": Name specified more than once`)
	c.Assert(err, jc.Satisfies, errors.IsNotValid)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesRefusesToAddParentAndChildrenInTheSameCall(c *gc.C) {
	allArgs := []state.LinkLayerDeviceArgs{{
		Name:       "child1",
		Type:       state.EthernetDevice,
		ParentName: "parent1",
	}, {
		Name: "parent1",
		Type: state.BridgeDevice,
	}}

	err := s.machine.AddLinkLayerDevices(allArgs...)
	c.Assert(err, gc.ErrorMatches, `cannot add link-layer devices to machine "0": `+
		`parent device "parent1" of device "child1" not found`)
	c.Assert(err, jc.Satisfies, errors.IsNotFound)
}

func (s *linkLayerDevicesStateSuite) addLinkLayerDevicesMultipleArgsSucceedsAndEnsureAllAdded(c *gc.C, allArgs []state.LinkLayerDeviceArgs) []*state.LinkLayerDevice {
	err := s.machine.AddLinkLayerDevices(allArgs...)
	c.Assert(err, jc.ErrorIsNil)

	var results []*state.LinkLayerDevice
	machineID, modelUUID := s.machine.Id(), s.State.ModelUUID()
	for _, args := range allArgs {
		device, err := s.machine.LinkLayerDevice(args.Name)
		c.Check(err, jc.ErrorIsNil)
		s.checkAddedDeviceMatchesArgs(c, device, args)
		s.checkAddedDeviceMatchesMachineIDAndModelUUID(c, device, machineID, modelUUID)
		results = append(results, device)
	}
	return results
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesMultipleChildrenOfExistingParentSucceeds(c *gc.C) {
	s.addNamedParentDeviceWithChildren(c, "parent", "child1", "child2")
}

func (s *linkLayerDevicesStateSuite) addNamedParentDeviceWithChildren(c *gc.C, parentName string, childrenNames ...string) (
	parent *state.LinkLayerDevice,
	children []*state.LinkLayerDevice,
) {
	parent = s.addNamedDevice(c, parentName)
	childrenArgs := make([]state.LinkLayerDeviceArgs, len(childrenNames))
	for i, childName := range childrenNames {
		childrenArgs[i] = state.LinkLayerDeviceArgs{
			Name:       childName,
			Type:       state.EthernetDevice,
			ParentName: parentName,
		}
	}

	children = s.addLinkLayerDevicesMultipleArgsSucceedsAndEnsureAllAdded(c, childrenArgs)
	c.Check(children, gc.HasLen, len(childrenNames))
	return parent, children
}

func (s *linkLayerDevicesStateSuite) addSimpleDevice(c *gc.C) *state.LinkLayerDevice {
	return s.addNamedDevice(c, "foo")
}

func (s *linkLayerDevicesStateSuite) addNamedDevice(c *gc.C, name string) *state.LinkLayerDevice {
	args := state.LinkLayerDeviceArgs{
		Name: name,
		Type: state.EthernetDevice,
	}
	err := s.machine.AddLinkLayerDevices(args)
	c.Assert(err, jc.ErrorIsNil)
	device, err := s.machine.LinkLayerDevice(name)
	c.Assert(err, jc.ErrorIsNil)
	return device
}

func (s *linkLayerDevicesStateSuite) TestMachineMethodReturnsNotFoundErrorWhenMissing(c *gc.C) {
	device := s.addSimpleDevice(c)

	err := s.machine.EnsureDead()
	c.Assert(err, jc.ErrorIsNil)
	err = s.machine.Remove()
	c.Assert(err, jc.ErrorIsNil)

	result, err := device.Machine()
	c.Assert(err, gc.ErrorMatches, "machine 0 not found")
	c.Assert(err, jc.Satisfies, errors.IsNotFound)
	c.Assert(result, gc.IsNil)
}

func (s *linkLayerDevicesStateSuite) TestMachineMethodReturnsMachine(c *gc.C) {
	device := s.addSimpleDevice(c)

	result, err := device.Machine()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(result, jc.DeepEquals, s.machine)
}

func (s *linkLayerDevicesStateSuite) TestParentDeviceReturnsLinkLayerDevice(c *gc.C) {
	parent, children := s.addNamedParentDeviceWithChildren(c, "br-eth0", "eth0")

	child := children[0]
	parentCopy, err := child.ParentDevice()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(parentCopy, jc.DeepEquals, parent)
}

func (s *linkLayerDevicesStateSuite) TestMachineLinkLayerDeviceReturnsNotFoundErrorWhenMissing(c *gc.C) {
	result, err := s.machine.LinkLayerDevice("missing")
	c.Assert(result, gc.IsNil)
	c.Assert(err, jc.Satisfies, errors.IsNotFound)
	c.Assert(err, gc.ErrorMatches, `device "missing" on machine "0" not found`)
}

func (s *linkLayerDevicesStateSuite) TestMachineLinkLayerDeviceReturnsLinkLayerDevice(c *gc.C) {
	existingDevice := s.addSimpleDevice(c)

	result, err := s.machine.LinkLayerDevice(existingDevice.Name())
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(result, jc.DeepEquals, existingDevice)
}

func (s *linkLayerDevicesStateSuite) TestMachineAllLinkLayerDevices(c *gc.C) {
	s.assertNoDevicesOnMachine(c, s.machine)
	topParent, secondLevelParents := s.addNamedParentDeviceWithChildren(c, "br-bond0", "bond0")
	secondLevelParent := secondLevelParents[0]

	secondLevelChildrenArgs := []state.LinkLayerDeviceArgs{{
		Name:       "eth0",
		Type:       state.EthernetDevice,
		ParentName: secondLevelParent.Name(),
	}, {
		Name:       "eth1",
		Type:       state.EthernetDevice,
		ParentName: secondLevelParent.Name(),
	}}
	s.addLinkLayerDevicesMultipleArgsSucceedsAndEnsureAllAdded(c, secondLevelChildrenArgs)

	results, err := s.machine.AllLinkLayerDevices()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(results, gc.HasLen, 4)
	for _, result := range results {
		c.Check(result, gc.NotNil)
		c.Check(result.MachineID(), gc.Equals, s.machine.Id())
		c.Check(result.Name(), gc.Matches, `(br-bond0|bond0|eth0|eth1)`)
		if result.Name() == topParent.Name() {
			c.Check(result.ParentName(), gc.Equals, "")
			continue
		}
		c.Check(result.ParentName(), gc.Matches, `(br-bond0|bond0)`)
	}
}

func (s *linkLayerDevicesStateSuite) assertNoDevicesOnMachine(c *gc.C, machine *state.Machine) {
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, machine, 0)
}

func (s *linkLayerDevicesStateSuite) assertAllLinkLayerDevicesOnMachineMatchCount(c *gc.C, machine *state.Machine, expectedCount int) {
	results, err := machine.AllLinkLayerDevices()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(results, gc.HasLen, expectedCount)
}

func (s *linkLayerDevicesStateSuite) TestMachineAllLinkLayerDevicesOnlyReturnsSameModelDevices(c *gc.C) {
	s.assertNoDevicesOnMachine(c, s.machine)
	s.assertNoDevicesOnMachine(c, s.otherStateMachine)

	s.addNamedParentDeviceWithChildren(c, "foo", "foo.42")

	results, err := s.machine.AllLinkLayerDevices()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(results, gc.HasLen, 2)
	c.Assert(results[0].Name(), gc.Equals, "foo")
	c.Assert(results[1].Name(), gc.Equals, "foo.42")

	s.assertNoDevicesOnMachine(c, s.otherStateMachine)
}

func (s *linkLayerDevicesStateSuite) TestLinkLayerDeviceRemoveFailsWithExistingChildren(c *gc.C) {
	parent, _ := s.addNamedParentDeviceWithChildren(c, "parent", "one-child", "another-child")

	err := parent.Remove()
	expectedError := fmt.Sprintf(
		"cannot remove %s: parent device %q has children: another-child, one-child",
		parent, parent.Name(),
	)
	c.Assert(err, gc.ErrorMatches, expectedError)
	c.Assert(err, jc.Satisfies, state.IsParentDeviceHasChildrenError)
}

func (s *linkLayerDevicesStateSuite) TestLinkLayerDeviceRemoveSuccess(c *gc.C) {
	existingDevice := s.addSimpleDevice(c)

	s.removeDeviceAndAssertSuccess(c, existingDevice)
	s.assertNoDevicesOnMachine(c, s.machine)
}

func (s *linkLayerDevicesStateSuite) removeDeviceAndAssertSuccess(c *gc.C, givenDevice *state.LinkLayerDevice) {
	err := givenDevice.Remove()
	c.Assert(err, jc.ErrorIsNil)
}

func (s *linkLayerDevicesStateSuite) TestLinkLayerDeviceRemoveTwiceStillSucceeds(c *gc.C) {
	existingDevice := s.addSimpleDevice(c)

	s.removeDeviceAndAssertSuccess(c, existingDevice)
	s.removeDeviceAndAssertSuccess(c, existingDevice)
	s.assertNoDevicesOnMachine(c, s.machine)
}

func (s *linkLayerDevicesStateSuite) TestMachineRemoveAllLinkLayerDevicesSuccess(c *gc.C) {
	s.assertNoDevicesOnMachine(c, s.machine)
	s.addNamedParentDeviceWithChildren(c, "foo", "bar")

	err := s.machine.RemoveAllLinkLayerDevices()
	c.Assert(err, jc.ErrorIsNil)
	s.assertNoDevicesOnMachine(c, s.machine)
}

func (s *linkLayerDevicesStateSuite) TestMachineRemoveAllLinkLayerDevicesNoErrorIfNoDevicesExist(c *gc.C) {
	s.assertNoDevicesOnMachine(c, s.machine)

	err := s.machine.RemoveAllLinkLayerDevices()
	c.Assert(err, jc.ErrorIsNil)
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesRollbackWithDuplicateProviderIDs(c *gc.C) {
	parent := s.addNamedDevice(c, "parent")
	insertingArgs := []state.LinkLayerDeviceArgs{{
		Name:       "child1",
		Type:       state.EthernetDevice,
		ProviderID: "child1-id",
		ParentName: parent.Name(),
	}, {
		Name:       "child2",
		Type:       state.BridgeDevice,
		ProviderID: "child2-id",
		ParentName: parent.Name(),
	}}

	assertThreeExistAndRemoveChildren := func(childrenNames ...string) {
		s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 3)
		for _, childName := range childrenNames {
			child, err := s.machine.LinkLayerDevice(childName)
			c.Check(err, jc.ErrorIsNil)
			c.Check(child.Remove(), jc.ErrorIsNil)
		}
	}

	hooks := []jujutxn.TestHook{{
		Before: func() {
			// Add the same devices to trigger ErrAborted in the first attempt.
			s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // only the parent exists
			err := s.machine.AddLinkLayerDevices(insertingArgs...)
			c.Assert(err, jc.ErrorIsNil)
		},
		After: func() {
			assertThreeExistAndRemoveChildren("child1", "child2")
		},
	}, {
		Before: func() {
			// Add devices with same ProviderIDs but different names.
			s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // only the parent exists
			insertingAlternateArgs := insertingArgs
			insertingAlternateArgs[0].Name = "other-child1"
			insertingAlternateArgs[1].Name = "other-child2"
			err := s.machine.AddLinkLayerDevices(insertingAlternateArgs...)
			c.Assert(err, jc.ErrorIsNil)
		},
		After: func() {
			assertThreeExistAndRemoveChildren("other-child1", "other-child2")
		},
	}}
	defer state.SetTestHooks(c, s.State, hooks...).Check()

	err := s.machine.AddLinkLayerDevices(insertingArgs...)
	c.Assert(err, gc.ErrorMatches, `.*ProviderID\(s\) not unique: child1-id, child2-id`)
	c.Assert(err, jc.Satisfies, state.IsProviderIDNotUniqueError)
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // only the parent exists and rollback worked.
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithLightStateChurn(c *gc.C) {
	childArgs, churnHook := s.prepareAddLinkLayerDevicesWithStateChurn(c)
	defer state.SetTestHooks(c, s.State, churnHook).Check()
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // parent only

	err := s.machine.AddLinkLayerDevices(childArgs)
	c.Assert(err, jc.ErrorIsNil)
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 2) // both parent and child remain
}

func (s *linkLayerDevicesStateSuite) prepareAddLinkLayerDevicesWithStateChurn(c *gc.C) (state.LinkLayerDeviceArgs, jujutxn.TestHook) {
	parent := s.addNamedDevice(c, "parent")
	childArgs := state.LinkLayerDeviceArgs{
		Name:       "child",
		Type:       state.EthernetDevice,
		ParentName: parent.Name(),
	}

	churnHook := jujutxn.TestHook{
		Before: func() {
			s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // just the parent
			err := s.machine.AddLinkLayerDevices(childArgs)
			c.Assert(err, jc.ErrorIsNil)
		},
		After: func() {
			s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 2) // parent and child
			child, err := s.machine.LinkLayerDevice("child")
			c.Assert(err, jc.ErrorIsNil)
			err = child.Remove()
			c.Assert(err, jc.ErrorIsNil)
		},
	}

	return childArgs, churnHook
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithModerateStateChurn(c *gc.C) {
	childArgs, churnHook := s.prepareAddLinkLayerDevicesWithStateChurn(c)
	defer state.SetTestHooks(c, s.State, churnHook, churnHook).Check()
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // parent only

	err := s.machine.AddLinkLayerDevices(childArgs)
	c.Assert(err, jc.ErrorIsNil)
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 2) // both parent and child remain
}

func (s *linkLayerDevicesStateSuite) TestAddLinkLayerDevicesWithTooMuchStateChurn(c *gc.C) {
	childArgs, churnHook := s.prepareAddLinkLayerDevicesWithStateChurn(c)
	defer state.SetTestHooks(c, s.State, churnHook, churnHook, churnHook).Check()
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // parent only

	err := s.machine.AddLinkLayerDevices(childArgs)
	c.Assert(errors.Cause(err), gc.Equals, jujutxn.ErrExcessiveContention)
	s.assertAllLinkLayerDevicesOnMachineMatchCount(c, s.machine, 1) // only the parent remains
}
