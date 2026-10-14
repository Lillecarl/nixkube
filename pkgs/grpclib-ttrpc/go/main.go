/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * TTRPC Test Server: Minimal TTRPC runtime for testing grpclib-ttrpc clients.
 * Implements the streaming service from github.com/containerd/ttrpc/integration/streaming,
 * following the same logic as the ttrpc integration test suite.
 */

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"time"

	ttrpc "github.com/containerd/ttrpc"
	// Import the service definitions and message types from ttrpc repo
	streaming "github.com/containerd/ttrpc/integration/streaming"
	"github.com/sirupsen/logrus"
	"google.golang.org/protobuf/types/known/emptypb"
)

func main() {
	var (
		socketPath string
		timeout    time.Duration
		verbose    bool
	)

	flag.StringVar(&socketPath, "socket", "", "path to TTRPC socket")
	flag.DurationVar(&timeout, "timeout", 10*time.Second, "timeout for service operations")
	flag.BoolVar(&verbose, "v", false, "verbose logging")
	flag.Parse()

	if socketPath == "" {
		fmt.Fprintf(os.Stderr, "Usage: ttrpc-test-server -socket <path>\n")
		os.Exit(1)
	}

	if verbose {
		logrus.SetLevel(logrus.DebugLevel)
	} else {
		logrus.SetLevel(logrus.InfoLevel)
	}

	logger := logrus.WithField("component", "ttrpc-test-server")
	logger.Infof("Starting TTRPC test server on socket: %s", socketPath)

	// Ensure parent directory exists
	if err := os.MkdirAll(socketPath[:len(socketPath)-len("ttrpc.sock")], 0o755); err == nil || os.IsExist(err) {
		// Ignore errors for simplicity
	}

	// Remove existing socket if present
	os.Remove(socketPath)

	// Create TTRPC server
	server, err := ttrpc.NewServer()
	if err != nil {
		logger.Fatalf("Failed to create TTRPC server: %v", err)
	}
	defer server.Close()

	// Register the test streaming service
	streaming.RegisterTTRPCStreamingService(server, &testStreamingService{})

	// Listen on Unix socket
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		logger.Fatalf("Failed to listen on socket: %v", err)
	}
	defer listener.Close()
	defer os.Remove(socketPath)

	logger.Info("TTRPC test server listening, waiting for clients...")

	// Serve with timeout context
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	if err := server.Serve(ctx, listener); err != nil && !errors.Is(err, ttrpc.ErrServerClosed) && !errors.Is(err, context.DeadlineExceeded) {
		logger.Fatalf("Server error: %v", err)
	}

	logger.Info("TTRPC test server stopped")
}

// testStreamingService implements the Streaming service.
// Reuses the logic from github.com/containerd/ttrpc/integration/streaming_test.go
type testStreamingService struct{}

func (s *testStreamingService) Echo(ctx context.Context, e *streaming.EchoPayload) (*streaming.EchoPayload, error) {
	e.Seq++
	return e, nil
}

func (s *testStreamingService) EchoStream(ctx context.Context, es streaming.TTRPCStreaming_EchoStreamServer) error {
	for {
		var e streaming.EchoPayload
		if err := es.RecvMsg(&e); err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
		e.Seq++
		if err := es.SendMsg(&e); err != nil {
			return err
		}
	}
}

func (s *testStreamingService) SumStream(ctx context.Context, ss streaming.TTRPCStreaming_SumStreamServer) (*streaming.Sum, error) {
	var sum streaming.Sum
	for {
		var part streaming.Part
		if err := ss.RecvMsg(&part); err != nil {
			if err == io.EOF {
				break
			}
			return nil, err
		}
		sum.Sum = sum.Sum + part.Add
		sum.Num++
	}
	return &sum, nil
}

func (s *testStreamingService) DivideStream(ctx context.Context, sum *streaming.Sum, ss streaming.TTRPCStreaming_DivideStreamServer) error {
	if sum.Num == 0 {
		return fmt.Errorf("cannot divide by zero")
	}
	avg := sum.Sum / sum.Num
	remainder := sum.Sum % sum.Num

	// Send average Num times
	for i := 0; i < int(sum.Num); i++ {
		var part streaming.Part
		part.Add = avg
		if i == 0 {
			// Add remainder to first part
			part.Add += remainder
		}
		if err := ss.Send(&part); err != nil {
			return err
		}
	}
	return nil
}

func (s *testStreamingService) EchoNull(ctx context.Context, es streaming.TTRPCStreaming_EchoNullServer) (*emptypb.Empty, error) {
	for {
		var e streaming.EchoPayload
		if err := es.RecvMsg(&e); err != nil {
			if err == io.EOF {
				break
			}
			return nil, err
		}
	}
	return &emptypb.Empty{}, nil
}

func (s *testStreamingService) EchoNullStream(ctx context.Context, es streaming.TTRPCStreaming_EchoNullStreamServer) error {
	for {
		var e streaming.EchoPayload
		if err := es.RecvMsg(&e); err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
		if err := es.SendMsg(&emptypb.Empty{}); err != nil {
			return err
		}
	}
}

func (s *testStreamingService) EmptyPayloadStream(ctx context.Context, _ *emptypb.Empty, ss streaming.TTRPCStreaming_EmptyPayloadStreamServer) error {
	for i := 0; i < 5; i++ {
		if err := ss.Send(&streaming.EchoPayload{
			Seq: uint32(i),
			Msg: fmt.Sprintf("payload %d", i),
		}); err != nil {
			return err
		}
	}
	return nil
}
