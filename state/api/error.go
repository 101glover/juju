// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package api

import (
	"launchpad.net/juju-core/rpc"
)

// Error is the type of error returned by any call
// to the state API.
type Error struct {
	Message string
	Code    string
}

func (e *Error) Error() string {
	return e.Message
}

func (e *Error) ErrorCode() string {
	return e.Code
}

var _ rpc.ErrorCoder = (*Error)(nil)

// The Code constants hold error codes for some kinds of error.
const (
	CodeNotFound            = "not found"
	CodeUnauthorized        = "unauthorized access"
	CodeCannotEnterScope    = "cannot enter scope"
	CodeCannotEnterScopeYet = "cannot enter scope yet"
	CodeExcessiveContention = "excessive contention"
	CodeUnitHasSubordinates = "unit has subordinates"
	CodeNotAssigned         = "not assigned"
	CodeStopped             = "stopped"
)

// ErrCode returns the error code associated with
// the given error, or the empty string if there
// is none.
func ErrCode(err error) string {
	if err, _ := err.(rpc.ErrorCoder); err != nil {
		return err.ErrorCode()
	}
	return ""
}

// clientError maps errors returned from an RPC call into local errors with
// appropriate values.
func clientError(err error) error {
	rerr, ok := err.(*rpc.RequestError)
	if !ok {
		return err
	}
	// We use our own error type rather than rpc.ServerError
	// because we don't want the code or the "server error" prefix
	// within the error message. Also, it's best not to make clients
	// know that we're using the rpc package.
	return &Error{
		Message: rerr.Message,
		Code:    rerr.Code,
	}
}
