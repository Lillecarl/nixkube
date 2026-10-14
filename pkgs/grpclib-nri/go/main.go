/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * NRI Test Server: Minimal NRI runtime for testing grpclib-nri plugins.
 * Accepts a plugin on a Unix socket and verifies the registration handshake.
 */

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/containerd/nri/pkg/adaptation"
	"github.com/containerd/nri/pkg/api"
	"github.com/sirupsen/logrus"
)

const (
	runtimeName    = "grpclib-nri-test"
	runtimeVersion = "0.1.0"
)

func main() {
	var (
		socketPath string
		timeout    time.Duration
		verbose    bool
	)

	flag.StringVar(&socketPath, "socket", "", "path to NRI socket")
	flag.DurationVar(&timeout, "timeout", 10*time.Second, "timeout for plugin registration")
	flag.BoolVar(&verbose, "v", false, "verbose logging")
	flag.Parse()

	if socketPath == "" {
		fmt.Fprintf(os.Stderr, "Usage: nri-test-server -socket <path>\n")
		os.Exit(1)
	}

	if verbose {
		logrus.SetLevel(logrus.DebugLevel)
	} else {
		logrus.SetLevel(logrus.InfoLevel)
	}

	logger := logrus.WithField("component", "nri-test-server")
	logger.Infof("Starting NRI test server on socket: %s", socketPath)

	// Ensure parent directory exists
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o755); err != nil {
		logger.Fatalf("Failed to create socket directory: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	// Create NRI runtime instance
	runtime, err := adaptation.New(
		runtimeName,
		runtimeVersion,
		func(ctx context.Context, cb adaptation.SyncCB) error {
			// Return empty pod/container lists for synchronization
			_, err := cb(ctx, []*api.PodSandbox{}, []*api.Container{})
			return err
		},
		func(ctx context.Context, updates []*api.ContainerUpdate) ([]*api.ContainerUpdate, error) {
			// No-op update handler
			return updates, nil
		},
		adaptation.WithSocketPath(socketPath),
	)
	if err != nil {
		logger.Fatalf("Failed to create NRI runtime: %v", err)
	}

	// Start the runtime
	if err := runtime.Start(); err != nil {
		logger.Fatalf("Failed to start NRI runtime: %v", err)
	}
	defer runtime.Stop()

	logger.Info("NRI runtime started, waiting for plugin registration...")

	// Wait for plugin to register and synchronize
	// The adaptation layer blocks when a plugin registers until it completes Configure + Synchronize
	blockSync := runtime.BlockPluginSync()
	defer blockSync.Unblock()

	// Wait for ctx to timeout or plugin to register
	<-ctx.Done()
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		logger.Fatalf("Timeout waiting for plugin registration")
	}

	logger.Info("Plugin registration successful")
}
