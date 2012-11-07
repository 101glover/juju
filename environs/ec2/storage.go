package ec2

import (
	"fmt"
	"io"
	"launchpad.net/goamz/s3"
	"launchpad.net/juju-core/environs"
	"sync"
	"time"
)

// storage implements environs.Storage on
// an ec2.bucket.
type storage struct {
	bucketMutex sync.Mutex
	madeBucket  bool
	bucket      *s3.Bucket
}

// makeBucket makes the environent's control bucket, the
// place where bootstrap information and deployed charms
// are stored. To avoid two round trips on every PUT operation,
// we do this only once for each environ.
func (s *storage) makeBucket() error {
	s.bucketMutex.Lock()
	defer s.bucketMutex.Unlock()
	if s.madeBucket {
		return nil
	}
	// PutBucket always return a 200 if we recreate an existing bucket for the
	// original s3.amazonaws.com endpoint. For all other endpoints PutBucket
	// returns 409 with a known subcode.
	if err := s.bucket.PutBucket(s3.Private); err != nil && s3ErrCode(err) != "BucketAlreadyOwnedByYou" {
		return err
	}

	s.madeBucket = true
	return nil
}

func (s *storage) Put(file string, r io.Reader, length int64) error {
	if err := s.makeBucket(); err != nil {
		return fmt.Errorf("cannot make S3 control bucket: %v", err)
	}
	err := s.bucket.PutReader(file, r, length, "binary/octet-stream", s3.Private)
	if err != nil {
		return fmt.Errorf("cannot write file %q to control bucket: %v", file, err)
	}
	return nil
}

func (s *storage) Get(file string) (r io.ReadCloser, err error) {
	for a := shortAttempt.Start(); a.Next(); {
		r, err = s.bucket.GetReader(file)
		if s3ErrorStatusCode(err) == 404 {
			continue
		}
		return
	}
	return r, maybeNotFound(err)
}

func (s *storage) URL(name string) (string, error) {
	// 10 years should be good enough.
	return s.bucket.SignedURL(name, time.Now().AddDate(1, 0, 0)), nil
}

// s3ErrorStatusCode returns the HTTP status of the S3 request error,
// if it is an error from an S3 operation, or 0 if it was not.
func s3ErrorStatusCode(err error) int {
	if err, _ := err.(*s3.Error); err != nil {
		return err.StatusCode
	}
	return 0
}

// s3ErrCode returns the text status code of the S3 error code.
func s3ErrCode(err error) string {
	if err, ok := err.(*s3.Error); ok {
		return err.Code
	}
	return ""
}

func (s *storage) Remove(file string) error {
	err := s.bucket.Del(file)
	// If we can't delete the object because the bucket doesn't
	// exist, then we don't care.
	if s3ErrorStatusCode(err) == 404 {
		return nil
	}
	return err
}

func (s *storage) List(prefix string) ([]string, error) {
	// TODO cope with more than 1000 objects in the bucket.
	resp, err := s.bucket.List(prefix, "", "", 0)
	if err != nil {
		// If the bucket is not found, it's not an error
		// because it's only created when the first
		// file is put.
		if s3ErrorStatusCode(err) == 404 {
			return nil, nil
		}
		return nil, err
	}
	var names []string
	for _, key := range resp.Contents {
		names = append(names, key.Key)
	}
	return names, nil
}

func (s *storage) deleteAll() error {
	names, err := s.List("")
	if err != nil {
		return err
	}
	// Remove all the objects in parallel so that we incur less round-trips.
	// If we're in danger of having hundreds of objects,
	// we'll want to change this to limit the number
	// of concurrent operations.
	var wg sync.WaitGroup
	wg.Add(len(names))
	errc := make(chan error, len(names))
	for _, name := range names {
		name := name
		go func() {
			if err := s.Remove(name); err != nil {
				errc <- err
			}
			wg.Done()
		}()
	}
	wg.Wait()
	select {
	case err := <-errc:
		return fmt.Errorf("cannot delete all provider state: %v", err)
	default:
	}

	s.bucketMutex.Lock()
	defer s.bucketMutex.Unlock()
	// Even DelBucket fails, it won't harm if we try again - the operation
	// might have succeeded even if we get an error.
	s.madeBucket = false
	err = s.bucket.DelBucket()
	if s3ErrorStatusCode(err) == 404 {
		return nil
	}
	return err
}

func maybeNotFound(err error) error {
	if s3ErrorStatusCode(err) == 404 {
		return &environs.NotFoundError{err}
	}
	return err
}
