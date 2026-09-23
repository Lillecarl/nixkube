# SPDX-License-Identifier: MIT

"""Configuration constants and environment variable parsing.

This module is the single source of truth for all nixkube configuration. All environment
variables are read here with their defaults, and exported as module-level constants for
use throughout the application. Configuration is centralized to prevent scattered env var
reads and ensure consistent defaults across the codebase.
"""

import os
import sys
from importlib import metadata
from pathlib import Path


def _parse_int_env(name: str, default: str) -> int:
    """Parse an integer environment variable, exiting with a clear error on invalid input."""
    val = os.environ.get(name, default)
    try:
        return int(val)
    except ValueError:
        print(
            f"Configuration error: {name}={val!r} must be an integer",
            file=sys.stderr,
        )
        sys.exit(1)


def _parse_float_env(name: str, default: str) -> float:
    """Parse a float environment variable, exiting with a clear error on invalid input."""
    val = os.environ.get(name, default)
    try:
        return float(val)
    except ValueError:
        print(
            f"Configuration error: {name}={val!r} must be a number",
            file=sys.stderr,
        )
        sys.exit(1)


CSI_PLUGIN_NAME = "nixkube"
try:
    CSI_VENDOR_VERSION = metadata.version("nixkube")
except metadata.PackageNotFoundError:
    # When running tests or in development, package may not be installed
    CSI_VENDOR_VERSION = "dev"

# Exit code from mount command when target is already mounted
MOUNT_ALREADY_MOUNTED = 32

# Paths we base everything on.
# Remember that these are CSI pod paths not node paths.
NIX_ROOT = Path("/")
CSI_ROOT = NIX_ROOT / "nix/var/nix-csi"
CSI_VOLUMES = CSI_ROOT / "volumes"
NRI_CONTAINERS = CSI_ROOT / "containers"
CSI_GCROOTS = NIX_ROOT / "nix/var/nix/gcroots/nix-csi"

# Configurable via kubenix option: nodeBuildTimeout (default: 300)
# Set via NIX_BUILD_TIMEOUT environment variable
NIX_BUILD_TIMEOUT: float = _parse_float_env("NIX_BUILD_TIMEOUT", "300")

# Builder configuration
# Set via environment variables from kubenix when builders are enabled
BUILDERS_ENABLED = os.environ.get("BUILDERS_ENABLED", "false").lower() == "true"

NAMESPACE = os.environ.get("KUBE_NAMESPACE", "nixkube")
BUILDERS_SERVICE = "nixkube-builders"

# Simple string check is fine - value controlled by easykubenix (always "true" or "false")
PYNIXD_ENABLED = os.environ.get("PYNIXD_ENABLED", "false") == "true"

# Whether to enable NRI plugin
# Set via NRI_ENABLED environment variable (default: true)
NRI_ENABLED = os.environ.get("NRI_ENABLED", "true") == "true"

# Whether to enable compatibility driver (nix.csi.store alongside nixkube)
# Set via ENABLE_COMPAT_DRIVER environment variable (default: false)
ENABLE_COMPAT_DRIVER = os.environ.get("ENABLE_COMPAT_DRIVER", "false") == "true"

# Prometheus metrics endpoint.
#
# On by default: a DaemonSet that cannot be scraped is one nobody can see, and
# the endpoint is read-only and carries no cluster data -- every series is
# about this node. `METRICS_ADDR` is every interface, because the scraper
# reaches a pod over the pod network and not over localhost.
#
# `"::"` and not `"0.0.0.0"`: a single-stack IPv6 cluster gives the pod a v6
# address and nothing else, and an IPv4 bind answers a scrape of it with
# ECONNREFUSED. A v6 socket also serves IPv4 while the kernel keeps its
# default `net.ipv6.bindv6only=0`, and `metrics.serve` falls back to IPv4
# where it does not.
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "true") == "true"
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9099"))
METRICS_ADDR = os.environ.get("METRICS_ADDR", "::")

# Verify store paths before mounting to detect corruption early
# Set via VERIFY_STORE_PATHS environment variable
VERIFY_STORE_PATHS = os.environ.get("VERIFY_STORE_PATHS", "false") == "true"

# CSI socket path for gRPC server
CSI_SOCKET_PATH = os.environ.get("CSI_SOCKET_PATH", "/csi/csi.sock")

# NRI runtime socket — containerd's multiplex socket we connect to
NRI_RUNTIME_SOCKET = os.environ.get("NRI_RUNTIME_SOCKET", "/var/run/nri/nri.sock")

# NRI plugin identity — sent in RegisterPlugin; must match the index prefix
# that containerd expects (two-digit zero-padded number, e.g. "69").
# Default "69" is high enough to avoid collision with early-stage plugins (00-50).
NRI_PLUGIN_NAME = os.environ.get("NRI_PLUGIN_NAME", "nixkube")
NRI_PLUGIN_IDX = os.environ.get("NRI_PLUGIN_IDX", "69")

# NRI host mount path for bind mounts (default: /var/lib/nix-csi)
# Set via HOST_MOUNT_PATH environment variable from kubenix
HOST_MOUNT_PATH = Path(os.environ.get("HOST_MOUNT_PATH", "/var/lib/nix-csi"))

# Host root filesystem mounted into the daemonset container.
HOST_ROOT = Path(os.environ.get("HOST_ROOT", "/host"))

# Host /proc mounted into the daemonset for accessing container namespaces.
HOST_PROC_PATH = str(HOST_ROOT / "proc")

# Kubelet pods directory for discovering active volumes
KUBELET_PODS_PATH = Path("/var/lib/kubelet/pods")

# CSI pod metadata for event reporting (from downwardAPI)
# These are required at runtime but may be absent in test environments
KUBE_POD_NAME = os.environ.get("KUBE_POD_NAME", "unknown")
KUBE_POD_UID = os.environ.get("KUBE_POD_UID", "unknown")
KUBE_NODE_NAME = os.environ.get("KUBE_NODE_NAME", "unknown")

# mount(2) flags (from sys/mount.h)
MS_RDONLY = 1
MS_REMOUNT = 32
MS_BIND = 4096
MS_REC = 16384

# umount2(2) flag: detach the subtree now and release it when nobody uses it.
# A plain umount2 of a mount that carries submounts fails with EBUSY.
MNT_DETACH = 2

# GC loop configuration
# Set via GC_KEEP_SECONDS / GC_INTERVAL_SECONDS environment variables
GC_KEEP_SECONDS: int = _parse_int_env("GC_KEEP_SECONDS", "3600")
GC_INTERVAL_SECONDS: int = _parse_int_env("GC_INTERVAL_SECONDS", "3600")

# Every nix call in the GC loop needs a deadline. The loop awaits one cycle at
# a time, so a call that never returns ends collection on that node until the
# pod restarts, and a restart re-enters the same call. Measured on a four-node
# Talos cluster: 33 hours, zero completed cycles, stores at 1.59 to 4.37 GB.
# Issue #38.
#
# The copy is the generous one: it can move a whole node's store over SSH.
GC_COPY_TIMEOUT_SECONDS: int = _parse_int_env("GC_COPY_TIMEOUT_SECONDS", "1800")
GC_PATH_INFO_TIMEOUT_SECONDS: int = _parse_int_env(
    "GC_PATH_INFO_TIMEOUT_SECONDS", "300"
)
GC_DELETE_TIMEOUT_SECONDS: int = _parse_int_env("GC_DELETE_TIMEOUT_SECONDS", "1800")

# How long to wait for pynixd to answer a ping before treating it as absent.
#
# **Short on purpose.** Every `NodePublishVolume` asks this before it builds,
# so the wait is paid per mount, and the only thing the answer decides is
# whether to pass `--extra-substituters`. A pynixd that needs more than a few
# seconds to say hello is not one a mount should wait on: the node builds or
# substitutes from elsewhere instead, which is slower for that path and
# faster than the alternative. An in-cluster SSH ping answers in well under a
# second when pynixd is there at all.
CACHE_PING_TIMEOUT_SECONDS: float = _parse_float_env("CACHE_PING_TIMEOUT_SECONDS", "5")

# Cycles without one completing before the loop says so at error level. Both
# failures of #38 were silent, and on a Talos node the first visible symptom
# is the kubelet evicting pods for disk.
GC_STALL_CYCLES: int = _parse_int_env("GC_STALL_CYCLES", "3")

# How long to wait for the CRI to answer ListContainers.
#
# The volume sweep awaits this, so a runtime that never answers ends volume
# collection on that node -- the shape issue #38 measured for the nix calls in
# the GC loop, on a socket those timeouts do not cover. The plugin also asks
# once at start-up, where a hang means it never registers and every container
# created afterwards gets no /nix.
CRI_LIST_TIMEOUT_SECONDS: float = _parse_float_env("CRI_LIST_TIMEOUT_SECONDS", "30")

# How long the volume sweep waits for a cancelled build to unwind before it
# treats the volume as still in use and leaves it to the next sweep.
#
# The hardlink walk leaves at its next checkpoint, which is every 256 entries,
# so it is gone in well under a second. The wait is for the `nix build`
# subprocess above it, which has to be signalled and reaped.
NRI_BUILD_CANCEL_TIMEOUT: float = _parse_float_env("NRI_BUILD_CANCEL_TIMEOUT", "30")

# Paths baked in at build time by makeWrapperArgs (empty in dev/test environments)
SETUP_BINSH = os.environ.get("SETUP_BINSH", "")
SETUP_CACERTS = os.environ.get("SETUP_CACERTS", "")
SETUP_USRBINENV = os.environ.get("SETUP_USRBINENV", "")
