// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package firewaller_test

import (
	jc "github.com/juju/testing/checkers"
	gc "gopkg.in/check.v1"
	"gopkg.in/juju/names.v2"

	"github.com/juju/juju/api/firewaller"
	"github.com/juju/juju/apiserver/params"
	"github.com/juju/juju/watcher/watchertest"
)

type serviceSuite struct {
	firewallerSuite

	apiService *firewaller.Application
}

var _ = gc.Suite(&serviceSuite{})

func (s *serviceSuite) SetUpTest(c *gc.C) {
	s.firewallerSuite.SetUpTest(c)

	var err error
	apiUnit, err := s.firewaller.Unit(s.units[0].Tag().(names.UnitTag))
	s.apiService, err = apiUnit.Application()
	c.Assert(err, jc.ErrorIsNil)
}

func (s *serviceSuite) TearDownTest(c *gc.C) {
	s.firewallerSuite.TearDownTest(c)
}

func (s *serviceSuite) TestName(c *gc.C) {
	c.Assert(s.apiService.Name(), gc.Equals, s.service.Name())
}

func (s *serviceSuite) TestTag(c *gc.C) {
	c.Assert(s.apiService.Tag(), gc.Equals, names.NewApplicationTag(s.service.Name()))
}

func (s *serviceSuite) TestWatch(c *gc.C) {
	c.Assert(s.apiService.Life(), gc.Equals, params.Alive)

	w, err := s.apiService.Watch()
	c.Assert(err, jc.ErrorIsNil)
	wc := watchertest.NewNotifyWatcherC(c, w, s.BackingState.StartSync)
	defer wc.AssertStops()

	// Initial event.
	wc.AssertOneChange()

	// Change something and check it's detected.
	err = s.service.SetExposed()
	c.Assert(err, jc.ErrorIsNil)
	wc.AssertOneChange()

	// Destroy the service and check it's detected.
	err = s.service.Destroy()
	c.Assert(err, jc.ErrorIsNil)
	wc.AssertOneChange()
}

func (s *serviceSuite) TestRefresh(c *gc.C) {
	c.Assert(s.apiService.Life(), gc.Equals, params.Alive)

	err := s.service.Destroy()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(s.apiService.Life(), gc.Equals, params.Alive)

	err = s.apiService.Refresh()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(s.apiService.Life(), gc.Equals, params.Dying)
}

func (s *serviceSuite) TestIsExposed(c *gc.C) {
	err := s.service.SetExposed()
	c.Assert(err, jc.ErrorIsNil)

	isExposed, err := s.apiService.IsExposed()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(isExposed, jc.IsTrue)

	err = s.service.ClearExposed()
	c.Assert(err, jc.ErrorIsNil)

	isExposed, err = s.apiService.IsExposed()
	c.Assert(err, jc.ErrorIsNil)
	c.Assert(isExposed, jc.IsFalse)
}
