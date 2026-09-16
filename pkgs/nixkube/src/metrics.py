# SPDX-License-Identifier: MIT
"""Prometheus metrics for the nixkube daemon.

Served on `METRICS_ADDR:METRICS_PORT`, which `cli.py` starts once at the top
of `async_main`. A DaemonSet pod answers for its own node, so every series
here is about this node and not about the cluster.

The server is the threaded one that `prometheus_client` ships. The daemon is
asyncio and a thread is off-pattern, but the alternative is an ASGI server and
a second HTTP stack in the image for one read-only route.
"""

from __future__ import annotations

import os

import structlog
from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily

from .constants import NIX_ROOT

logger = structlog.get_logger("nixkube.metrics")

# --- Garbage collection ---

GC_CYCLES = Counter(
    "nixkube_gc_cycles_total",
    "Garbage collection cycles this daemon has finished",
    ["result"],  # ok, error
)

GC_PATHS_DELETED = Counter(
    "nixkube_gc_paths_deleted_total",
    "Store paths deleted by garbage collection",
)

GC_CYCLE_DURATION = Histogram(
    "nixkube_gc_cycle_duration_seconds",
    "Time one garbage collection cycle took",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)

# Zero until the first cycle finishes, so an alert reads it as
# `== 0 or time() - it > N`. That is the shape `node_boot_time_seconds` and
# the textfile collector use for the same thing.
#
# **This is the series that would have surfaced #38 on day one.** A timestamp
# that stops advancing says the collector is stuck, which no size gauge says
# on its own: the store grew for 33 hours while every other number looked
# ordinary.
GC_LAST_SUCCESS = Gauge(
    "nixkube_gc_last_success_timestamp_seconds",
    "When a garbage collection cycle last finished, in unix seconds",
)

# --- The store itself ---
#
# Free: `_run_gc_cycle` already reads `nix path-info --all --json` for
# `registrationTime`, and `narSize` rides along in the same answer. So these
# cost nothing beyond the call the GC cycle already makes, and they move on
# the GC cadence rather than the scrape one.

# **`nar_bytes`, and not `store_bytes`.** A NAR size is what the path weighs on
# the wire. It ignores the hard links and the block overhead of the store on
# disk, so it is not the number `du` gives. Measured on a workstation: summing
# `narSize` over `ValidPaths` answered 952 GB for a file system of 268 GB. The
# name says `nar` so that nobody subtracts it from
# `nixkube_store_filesystem_size_bytes` beside it. It is the right series for
# watching growth, and the wrong one for how full the disk is.
STORE_NAR_BYTES = Gauge(
    "nixkube_store_nar_bytes",
    "Sum of the NAR size of every path in this node's store, which over-counts disk use",
)

STORE_PATHS = Gauge(
    "nixkube_store_paths",
    "Paths in this node's store",
)

# --- Volumes ---
#
# A CSI volume is published when a pod starts and unpublished when it stops,
# so these two counters and the gauge derived from them say how much of this
# node's work nixkube is carrying.

VOLUME_MOUNTS = Counter(
    "nixkube_volume_mounts_total",
    "Volumes this daemon has mounted",
    ["result"],  # ok, error
)

VOLUME_UNMOUNTS = Counter(
    "nixkube_volume_unmounts_total",
    "Volumes this daemon has unmounted",
    ["result"],  # ok, error
)

VOLUME_PREPARE_DURATION = Histogram(
    "nixkube_volume_prepare_duration_seconds",
    "Time spent realising the closure of one volume before it is mounted",
    buckets=(0.5, 1, 5, 15, 30, 60, 120, 300),
)


class StoreSpaceCollector:
    """How much room this node's store has, read when a scrape asks.

    A collector, and not a gauge that something keeps up to date: the answer
    costs one `statvfs`, and nothing else in nixkube needs it, so reading it
    on the scrape is cheaper and fresher than a periodic write.

    **This is the file system that holds the store, and not the sum of what is
    in it.** Those are different numbers, and the difference is large: on a
    development machine `sum(narSize)` over Nix's `ValidPaths` answered 952 GB
    for a file system of 268 GB, because NAR sizes add up without the
    deduplication and the hard links the store on disk has.

    That sum is also too slow to serve. It took 584 ms over 141,749 paths,
    against 0.010 ms for this `statvfs`. A node scraped every 15 s cannot pay
    half a second, and the number it bought would answer the wrong question.

    `NIX_ROOT` is `/` and these are pod paths, so this reads the store the
    DaemonSet shares with its node.
    """

    def collect(self):
        """Yield the two numbers, or nothing when the store is unreachable."""
        path = NIX_ROOT / "nix/store"
        try:
            stat = os.statvfs(path)
        except OSError:
            # An unreadable store is not a reason to fail the scrape: every
            # other series is still an answer. These two go absent, which a
            # dashboard shows as absent.
            logger.warning("store_space_unreadable", path=str(path), exc_info=True)
            return

        yield GaugeMetricFamily(
            "nixkube_store_filesystem_size_bytes",
            "Total size of the file system that holds /nix/store on this node",
            value=stat.f_blocks * stat.f_frsize,
        )
        # `f_bavail`, and not `f_bfree`. The reserved blocks are not available
        # to nixkube, and a dashboard reading `f_bfree` reports room on a node
        # where the next build fails.
        yield GaugeMetricFamily(
            "nixkube_store_filesystem_available_bytes",
            "Space on that file system available to this daemon",
            value=stat.f_bavail * stat.f_frsize,
        )


REGISTRY.register(StoreSpaceCollector())


def serve(port: int, addr: str) -> None:
    """Start the metrics endpoint, and let the daemon run without it on failure.

    A node that cannot bind the metrics port still has a CSI driver to run,
    and refusing to start one because the other is unavailable would turn an
    observability gap into an outage.
    """
    try:
        start_http_server(port, addr=addr)
    except OSError:
        logger.warning("metrics_server_failed", port=port, addr=addr, exc_info=True)
        return
    logger.info("metrics_serving", port=port, addr=addr)
