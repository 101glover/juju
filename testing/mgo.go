// Copyright 2012, 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package testing

import (
	"bufio"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"io/ioutil"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	stdtesting "testing"
	"time"

	"labix.org/v2/mgo"
	gc "launchpad.net/gocheck"

	"launchpad.net/juju-core/cert"
	"launchpad.net/juju-core/log"
	"launchpad.net/juju-core/utils"
)

var (
	// MgoServer is a shared mongo server used by tests.
	MgoServer = &MgoInstance{}
)

type MgoInstance struct {
	// Addr holds the address of the shared MongoDB server set up by
	// MgoTestPackage.
	Addr string

	// MgoPort holds the port used by the shared MongoDB server.
	Port int

	// Server holds the running MongoDB command.
	Server *exec.Cmd

	// Exited receives a value when the mongodb server exits.
	Exited <-chan struct{}

	// Dir holds the directory that MongoDB is running in.
	Dir string

	// params is a list of additional parameters that will be passed to
	// the mongod application
	Params []string
}

// We specify a timeout to mgo.Dial, to prevent
// mongod failures hanging the tests.
const mgoDialTimeout = 15 * time.Second

// MgoSuite is a suite that deletes all content from the shared MongoDB
// server at the end of every test and supplies a connection to the shared
// MongoDB server.
type MgoSuite struct {
	Session *mgo.Session
}

// startMgoServer starts a MongoDB server in a temporary directory.
func (inst *MgoInstance) Start() error {
	dbdir, err := ioutil.TempDir("", "test-mgo")
	if err != nil {
		return err
	}
	pemPath := filepath.Join(dbdir, "server.pem")
	err = ioutil.WriteFile(pemPath, []byte(ServerCert+ServerKey), 0600)
	if err != nil {
		return fmt.Errorf("cannot write cert/key PEM: %v", err)
	}
	inst.Port = FindTCPPort()
	inst.Addr = fmt.Sprintf("localhost:%d", inst.Port)
	inst.Dir = dbdir
	if err := inst.runMgoServer(); err != nil {
		inst.Addr = ""
		inst.Port = 0
		os.RemoveAll(inst.Dir)
		inst.Dir = ""
		return err
	}

	// wait until it's running
	deadline := time.Now().Add(time.Second * 10)
	for {
		err := inst.ping()
		if err == nil || time.Now().After(deadline) {
			return err
		}
	}
}

func (inst *MgoInstance) ping() error {
	session := inst.MgoDialDirect()
	defer session.Close()
	return session.Ping()
}

// runMgoServer runs the MongoDB server at the
// address and directory already configured.
func (inst *MgoInstance) runMgoServer() error {
	if inst.Server != nil {
		panic("mongo server is already running")
	}
	mgoport := strconv.Itoa(inst.Port)
	mgoargs := []string{
		"--auth",
		"--dbpath", inst.Dir,
		"--sslOnNormalPorts",
		"--sslPEMKeyFile", filepath.Join(inst.Dir, "server.pem"),
		"--sslPEMKeyPassword", "ignored",
		"--bind_ip", "localhost",
		"--port", mgoport,
		"--nssize", "1",
		"--noprealloc",
		"--smallfiles",
		"--nojournal",
		"--nounixsocket",
	}
	if inst.Params != nil {
		mgoargs = append(mgoargs, inst.Params...)
	}
	server := exec.Command("mongod", mgoargs...)
	out, err := server.StdoutPipe()
	if err != nil {
		return err
	}
	server.Stderr = server.Stdout
	exited := make(chan struct{})
	go func() {
		lines := readLines(out, 20)
		err := server.Wait()
		exitErr, _ := err.(*exec.ExitError)
		if err == nil || exitErr != nil && exitErr.Exited() {
			// mongodb has exited without being killed, so print the
			// last few lines of its log output.
			for _, line := range lines {
				log.Infof("mongod: %s", line)
			}
		}
		close(exited)
	}()
	inst.Exited = exited
	if err := server.Start(); err != nil {
		return err
	}
	inst.Server = server

	return nil
}

func (inst *MgoInstance) mgoKill() {
	inst.Server.Process.Kill()
	<-inst.Exited
	inst.Server = nil
	inst.Exited = nil
}

func (inst *MgoInstance) Destroy() {
	if inst.Server != nil {
		inst.mgoKill()
		os.RemoveAll(inst.Dir)
		inst.Addr, inst.Dir = "", ""
	}
}

// MgoRestart restarts the mongo server, useful for
// testing what happens when a state server goes down.
func (inst *MgoInstance) MgoRestart() {
	inst.mgoKill()
	if err := inst.Start(); err != nil {
		panic(err)
	}
}

// MgoTestPackage should be called to register the tests for any package that
// requires a MongoDB server.
func MgoTestPackage(t *stdtesting.T) {
	if err := MgoServer.Start(); err != nil {
		t.Fatal(err)
	}
	defer MgoServer.Destroy()
	gc.TestingT(t)
}

func (s *MgoSuite) SetUpSuite(c *gc.C) {
	if MgoServer.Addr == "" {
		panic("MgoSuite tests must be run with MgoTestPackage")
	}
	mgo.SetStats(true)
	// Make tests that use password authentication faster.
	utils.FastInsecureHash = true
}

// readLines reads lines from the given reader and returns
// the last n non-empty lines, ignoring empty lines.
func readLines(r io.Reader, n int) []string {
	br := bufio.NewReader(r)
	lines := make([]string, n)
	i := 0
	for {
		line, err := br.ReadString('\n')
		if line = strings.TrimRight(line, "\n"); line != "" {
			lines[i%n] = line
			i++
		}
		if err != nil {
			break
		}
	}
	final := make([]string, 0, n+1)
	if i > n {
		final = append(final, fmt.Sprintf("[%d lines omitted]", i-n))
	}
	for j := 0; j < n; j++ {
		if line := lines[(j+i)%n]; line != "" {
			final = append(final, line)
		}
	}
	return final
}

func (s *MgoSuite) TearDownSuite(c *gc.C) {
	utils.FastInsecureHash = false
}

// MgoDial returns a new connection to the shared MongoDB server.
func (inst *MgoInstance) MgoDial() *mgo.Session {
	return inst.dial(false)
}

// MgoDialDirect returns a new direct connection to the shared MongoDB server. This
// must be used if you're connecting to a replicaset that hasn't been initiated
// yet.
func (inst *MgoInstance) MgoDialDirect() *mgo.Session {
	return inst.dial(true)
}

func (inst *MgoInstance) dial(direct bool) *mgo.Session {
	pool := x509.NewCertPool()
	xcert, err := cert.ParseCert([]byte(CACert))
	if err != nil {
		panic(err)
	}
	pool.AddCert(xcert)
	tlsConfig := &tls.Config{
		RootCAs:    pool,
		ServerName: "anything",
	}
	session, err := mgo.DialWithInfo(&mgo.DialInfo{
		Direct: direct,
		Addrs:  []string{inst.Addr},
		Dial: func(addr net.Addr) (net.Conn, error) {
			return tls.Dial("tcp", addr.String(), tlsConfig)
		},
		Timeout: mgoDialTimeout,
	})
	if err != nil {
		panic(err)
	}
	return session
}

func (s *MgoSuite) SetUpTest(c *gc.C) {
	mgo.ResetStats()
	s.Session = MgoServer.MgoDial()
}

// MgoReset deletes all content from the shared MongoDB server.
func (inst *MgoInstance) MgoReset() {
	session := inst.MgoDial()
	defer session.Close()

	dbnames, ok := resetAdminPasswordAndFetchDBNames(session)
	if ok {
		log.Infof("MgoReset successfully reset admin password")
	} else {
		// We restart it to regain access.  This should only
		// happen when tests fail.
		log.Noticef("testing: restarting MongoDB server after unauthorized access")
		inst.Destroy()
		if err := inst.Start(); err != nil {
			panic(err)
		}
		return
	}
	for _, name := range dbnames {
		switch name {
		case "admin", "local", "config":
		default:
			if err := session.DB(name).DropDatabase(); err != nil {
				panic(fmt.Errorf("Cannot drop MongoDB database %v: %v", name, err))
			}
		}
	}
}

// resetAdminPasswordAndFetchDBNames logs into the database with a
// plausible password and returns all the database's db names. We need
// to try several passwords because we don't know what state the mongo
// server is in when MgoReset is called. If the test has set a custom
// password, we're out of luck, but if they are using
// DefaultStatePassword, we can succeed.
func resetAdminPasswordAndFetchDBNames(session *mgo.Session) ([]string, bool) {
	// First try with no password
	dbnames, err := session.DatabaseNames()
	if err == nil {
		return dbnames, true
	}
	if !isUnauthorized(err) {
		panic(err)
	}
	// Then try the two most likely passwords in turn.
	for _, password := range []string{
		DefaultMongoPassword,
		utils.UserPasswordHash(DefaultMongoPassword, utils.CompatSalt),
	} {
		admin := session.DB("admin")
		if err := admin.Login("admin", password); err != nil {
			log.Infof("failed to log in with password %q", password)
			continue
		}
		dbnames, err := session.DatabaseNames()
		if err == nil {
			if err := admin.RemoveUser("admin"); err != nil {
				panic(err)
			}
			return dbnames, true
		}
		if !isUnauthorized(err) {
			panic(err)
		}
		log.Infof("unauthorized access when getting database names; password %q", password)
	}
	return nil, false
}

// isUnauthorized is a copy of the same function in state/open.go.
func isUnauthorized(err error) bool {
	if err == nil {
		return false
	}
	// Some unauthorized access errors have no error code,
	// just a simple error string.
	if err.Error() == "auth fails" {
		return true
	}
	if err, ok := err.(*mgo.QueryError); ok {
		return err.Code == 10057 ||
			err.Message == "need to login" ||
			err.Message == "unauthorized"
	}
	return false
}

func (s *MgoSuite) TearDownTest(c *gc.C) {
	MgoServer.MgoReset()
	s.Session.Close()
	for i := 0; ; i++ {
		stats := mgo.GetStats()
		if stats.SocketsInUse == 0 && stats.SocketsAlive == 0 {
			break
		}
		if i == 20 {
			c.Fatal("Test left sockets in a dirty state")
		}
		c.Logf("Waiting for sockets to die: %d in use, %d alive", stats.SocketsInUse, stats.SocketsAlive)
		time.Sleep(500 * time.Millisecond)
	}
}

// FindTCPPort finds an unused TCP port and returns it.
// Use of this function has an inherent race condition - another
// process may claim the port before we try to use it.
// We hope that the probability is small enough during
// testing to be negligible.
func FindTCPPort() int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	l.Close()
	return l.Addr().(*net.TCPAddr).Port
}
