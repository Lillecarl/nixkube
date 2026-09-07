# SPDX-License-Identifier: MIT
import asyncio
import shutil
from functools import wraps
from pathlib import Path
from typing import Any

import structlog
from grpclib_nri import NriPlugin as NriPluginBase
from grpclib_nri import NriServer
from kr8s.asyncio.objects import Pod
from nri import nri_pb2

from ..cache import schedule_copy_to_cache
from ..constants import (
    HOST_MOUNT_PATH,
    HOST_ROOT,
    NRI_CONTAINERS,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    NRI_RUNTIME_SOCKET,
)
from ..cri import get_cri_socket, list_container_ids
from ..events import report_event
from ..nix import fetch_packages, get_build_args, get_current_system
from ..volume import prepare_volume
from .annotations import extract_container_store_paths, parse_nix_rw, parse_store_mounts
from .cleanup import garbage_collect_stale_volumes
from .mount import kernel_supports_ro, kernel_supports_rw, mount_in_container
from .zmq import ZeroMQServer

# ============================================================================
# NRI POD CREATION LIFECYCLE
# ============================================================================
#
# The NRI plugin injects Nix stores into containers via a multi-phase process
# coordinated through OCI hooks and ZeroMQ sockets. The overall flow:
#
# PHASE 1: CreateContainer Hook (NRI synchronous)
# ────────────────────────────────────────────────
# 1. Parse pod annotations (nixkube/pod, nixkube/{container}, with system variants)
#    - Extract store paths from annotations, container args, and env vars
#    - Parse store mount paths (e.g., nixkube/pod-path: /container/path → /nix/store/...)
#    - Parse RW flag (e.g., nixkube/pod-rw: "true" for overlayfs instead of RO bind)
#
# 2. Skip if /nix already mounted
#    - If CSI driver already mounted /nix, NRI skips (CSI takes precedence)
#    - Prevents collision between two Nix injection methods
#
# 3. Inject createRuntime OCI hook
#    - Creates a hook that will fire during container init, before exec
#    - Executes: chroot ${HOST_MOUNT_PATH} wait (nri-wait binary from nixkube dependencies)
#    - Hook will report container PID+bundle via ZeroMQ REQ/REP sockets
#    - Uses OCI hooks (not NRI request handlers) because they lack forced low timeouts
#    - Uses pkgsStatic.chroot to create a comfortable isolated execution environment
#
# 4. Spawn background build task (starts immediately, runs async)
#    - Task ID = container_id, added to pending_builds set
#    - Begins realizing all store paths via nix build immediately
#    - Will hardlink closure into volume at /nix/var/nixkube/containers/{container_id}/nix
#    - This build starts NOW and runs concurrently with OCI hook execution
#
#
# PHASE 2: OCI Hook Execution (during container init)
# ─────────────────────────────────────────────────────
# The createRuntime hook fires while the container init process is alive with its
# mount namespace established (but before pivot_root, so host paths are accessible).
#
# 1. nri-wait binary starts (executed via chrooted coreutils-static.chroot)
#    - Connects to ZeroMQ REQ socket at /nix/var/nixkube/wait-req.sock
#    - Sends RegisterContainerRequest with PID (getpid) and bundle path (from OCI state)
#
# 2. ZeroMQ REQ/REP coordination
#    - Build task waits for pid_event[container_id] via zmq_server.wait_for_pid()
#    - Once nri-wait sends PID+bundle, zmq_server stores them and signals the event
#    - nri-wait's response is "container ready" (REP socket reply)
#    - nri-wait then waits for a follow-up signal (via PUB socket) before exiting
#
# 3. Heartbeat via PUB socket
#    - Build task spawns a _pump_build_progress task that publishes every 10 seconds
#    - nri-wait subscribes and resets its 30-second timeout on each message
#    - Ensures slow builds don't timeout during the mount operation
#
#
# PHASE 3: Build Task Waits & Mounts
# ────────────────────────────────────
# After spawning the OCI hook, build task:
#
# 1. Realizes store paths
#    - Calls nix build with extra args (builders, cache endpoints)
#    - Outputs at /nix/var/nixkube/containers/{container_id}/nix
#
# 2. Hardslink closure
#    - Calls prepare_volume() to hardlink all closure paths into the volume
#    - Creates upper/ and work/ dirs if RW overlayfs requested
#
# 3. Wait for PID+bundle
#    - Blocks on zmq_server.wait_for_pid(container_id, timeout=30)
#    - When nri-wait reports, returns (pid, bundle_path)
#
# 4. Spawn mount subprocess
#    - Calls mount_in_container(pid, bundle, nix_tree_path, store_mounts, nix_rw)
#    - Uses multiprocessing.spawn to avoid contaminating asyncio event loop with setns(2)
#    - Passes precomputed mount FDs created in the original namespace
#
#
# PHASE 4: Mount Subprocess (FD-based namespace operations)
# ───────────────────────────────────────────────────────────
# The subprocess is spawned with file descriptors for isolated namespace ops:
#
# 1. Create detached mount FDs (while in daemonset namespace)
#    - /nix (RO): open_tree clones the prepared nix tree as detached fd
#      - Survives setns(2) so the source path is never visible inside container
#      - Later remounted RO with move_mount(2)
#    - /nix (RW): fsopen/fsconfig build a detached overlayfs fd
#      - lowerdir=hardlink tree, upperdir=volume/upper, workdir=volume/work
#
# 2. Switch to container namespace
#    - setns(2) to enter the container's mount namespace via PID
#    - fchdir+chroot to enter the rootfs (using fd opened before setns)
#    - Now inside the container's mount namespace and rootfs
#
# 3. Attach /nix mount
#    - move_mount(2) attaches the detached /nix fd at /nix
#    - If RO: followed by MS_REMOUNT|MS_RDONLY
#
# 4. Attach store mounts
#    - For each store_mount (container_path → /nix/store/...) requested in annotations:
#      - Use traditional mount(2) MS_BIND from /nix/store/... to container_path
#      - Done after setns so /nix/store/... paths are reachable as bind-mount sources
#
# 5. Return to nri-wait
#    - After all mounts attached, subprocess exits
#    - nri-wait (waiting on PUB socket) receives "container ready" signal
#    - nri-wait exits the hook, allowing container init to continue to exec
#
#
# PHASE 5: Container Execution & Cleanup
# ─────────────────────────────────────────
# 1. Container runs with /nix and store mounts injected
#
# 2. On container removal (StateChange REMOVE_CONTAINER event)
#    - garbage_collect_stale_volumes(cri_socket): Queries CRI for active containers,
#      removes any stale volumes (including the one being removed) that are orphaned
#      by crashed containers or abrupt shutdowns
#
# ============================================================================


def nri_error_handler(
    func: Any,
) -> Any:  # decorator wraps arbitrary async handler methods
    """Decorator for NRI handlers: logs exceptions and reports events."""

    @wraps(func)
    async def wrapper(self, stream):
        handler_logger = structlog.get_logger(f"nixkube.nri.{func.__name__.lower()}")
        try:
            return await func(self, stream)
        except Exception as e:
            handler_logger.exception("handler_failed")
            await report_event(
                None,
                reason="InternalError",
                note=f"{func.__name__} failed: {type(e).__name__}",
                logs=str(e),
                event_type="Warning",
            )
            raise

    return wrapper


class NriPlugin(NriPluginBase):
    """NRI plugin with ZeroMQ build coordination."""

    def __init__(self, zmq_server: ZeroMQServer, cri_socket: Path):
        """Initialize the NRI plugin with ZeroMQ and CRI socket coordination.

        Args:
            zmq_server: ZeroMQ server for build task coordination and PID/bundle reporting
            cri_socket: Path to the CRI socket for container introspection
        """
        logger = structlog.get_logger("nixkube.nri.init")
        # Call NirPluginBase init that registers plugin and which events we listen to
        super().__init__(
            [
                nri_pb2.Event.CREATE_CONTAINER,  # Inject stores into containers
                nri_pb2.Event.REMOVE_CONTAINER,  # Cleanup hardlink farm volumes
            ]
        )
        self.zmq_server = zmq_server
        self.cri_socket = cri_socket
        # Find nri-wait binary on PATH (available as nix-csi dependency)
        self.nri_wait_bin = shutil.which("wait")
        logger.debug("nri_wait_resolved", binary=self.nri_wait_bin)

    @nri_error_handler
    async def CreateContainer(self, stream) -> None:
        structlog.contextvars.clear_contextvars()
        logger = structlog.get_logger("nixkube.nri.createcontainer")
        req: nri_pb2.CreateContainerRequest | None = await stream.recv_message()
        assert req is not None
        structlog.contextvars.bind_contextvars(
            pod={"namespace": req.pod.namespace, "name": req.pod.name},
            container={"name": req.container.name, "id": req.container.id},
        )
        logger.debug("create_container")

        # Check if /nix is already mounted (e.g., by nix-csi) to avoid collision
        if any(m.destination == "/nix" for m in req.container.mounts):
            logger.debug("nix_already_mounted")
            resp = nri_pb2.CreateContainerResponse(adjust=nri_pb2.ContainerAdjustment())
            await stream.send_message(resp)
            return

        system = get_current_system()

        store_paths = extract_container_store_paths(req, system)
        if store_paths:
            logger.info(
                "extracted_store_paths",
                count=len(store_paths),
                store_paths=sorted(str(p) for p in store_paths),
            )

        # Parse store mount annotations (nixkube/[container-name/]path), filtered by system
        store_mounts = parse_store_mounts(
            req.pod.annotations, req.container.name, system
        )
        if store_mounts:
            logger.info(
                "parsed_store_mounts",
                count=len(store_mounts),
                store_mounts={str(k): str(v) for k, v in store_mounts.items()},
            )

        # Parse RW flag (nixkube/pod-rw or nixkube/{container-name}-rw), filtered by system
        nix_rw = parse_nix_rw(req.pod.annotations, req.container.name, system)
        if nix_rw:
            logger.info("nix_rw_requested")
            # Verify kernel supports new mount API for overlayfs (6.5+)
            if not kernel_supports_rw():
                logger.error("nix_rw_kernel_unsupported")
                raise RuntimeError(
                    "RW overlay mounts not supported on this kernel (requires Linux 6.5+)"
                )

        adjust = nri_pb2.ContainerAdjustment()

        # Enable NRI build if we have storepaths to inject
        if store_paths:
            container_id = req.container.id

            logger.info("store_injection_enabled", count=len(store_paths))

            try:
                # Create Pod object for event reporting
                pod = Pod(
                    {
                        "metadata": {
                            "name": req.pod.name,
                            "namespace": req.pod.namespace,
                            "uid": req.pod.uid,
                        },
                    },
                    namespace=req.pod.namespace,
                )

                # Inject OCI hook to wait for build completion and report PID+bundle
                assert self.nri_wait_bin is not None, (
                    "nri-wait binary not found on PATH, wait hook won't be able to execute"
                )
                coreutils_container = shutil.which("coreutils")
                assert coreutils_container is not None, "coreutils not found on PATH"
                coreutils_host = HOST_MOUNT_PATH / Path(
                    coreutils_container
                ).relative_to("/")
                hook = nri_pb2.Hook(
                    path=str(coreutils_host),
                    args=[
                        "chroot",  # somehow this works in OCI hooks but not --coreutils-prog=chroot....
                        str(HOST_MOUNT_PATH),
                        self.nri_wait_bin,
                    ],
                    env=[
                        "NRI_QUERY_SOCKET=/nix/var/nixkube/wait-req.sock",
                        "NRI_PUB_SOCKET=/nix/var/nixkube/wait-pub.sock",
                        "NRI_TIMEOUT=30",
                    ],
                )
                adjust.hooks.create_runtime.append(hook)
                logger.info(
                    "hook_injected",
                    nri_wait_bin=self.nri_wait_bin,
                    coreutils_host=coreutils_host,
                )

                # Spawn build task to build store paths and namespace-mount them into the container
                if container_id not in self.zmq_server.pending_builds:
                    self.zmq_server.pending_builds.add(container_id)
                    logger.info("build_task_spawning", count=len(store_paths))
                    # Spawn background task (fire and forget with exception logging)
                    task = asyncio.create_task(
                        self._spawn_build_task(
                            container_id,
                            req.container.name,
                            pod,
                            store_paths,
                            store_mounts,
                            nix_rw,
                        )
                    )

                    def _build_done(t):
                        if t.cancelled():
                            logger.warning("build_task_cancelled")
                        elif t.exception():
                            logger.error("build_task_failed", exc_info=t.exception())
                        else:
                            logger.info("build_task_completed")

                    task.add_done_callback(_build_done)
                else:
                    logger.warning("build_already_pending")

            except Exception:
                logger.exception("volume_setup_failed")

        resp = nri_pb2.CreateContainerResponse(adjust=adjust)
        await stream.send_message(resp)

    @nri_error_handler
    async def StateChange(self, stream) -> None:
        structlog.contextvars.clear_contextvars()
        logger = structlog.get_logger("nixkube.nri.statechange")
        event: nri_pb2.StateChangeEvent | None = await stream.recv_message()
        assert event is not None

        event_name = nri_pb2.Event.Name(event.event)
        structlog.contextvars.bind_contextvars(
            nri_event=event_name,
            pod={"namespace": event.pod.namespace, "name": event.pod.name},
        )

        # Only include container info if this is a container event (container exists and has a name)
        if event.container and event.container.name:
            container_info: dict = {
                "name": event.container.name,
                "state": nri_pb2.ContainerState.Name(event.container.state),
            }
            if event.container.exit_code:
                container_info["exit_code"] = event.container.exit_code
            structlog.contextvars.bind_contextvars(container=container_info)

        logger.info("state_change")

        # Cleanup stale hardlink farm volumes when container is removed
        if event.event == nri_pb2.Event.REMOVE_CONTAINER:
            await garbage_collect_stale_volumes(
                self.cri_socket, removed_id=event.container.id or None
            )

        await stream.send_message(nri_pb2.Empty())

    async def _pump_build_progress(self, container_id: str) -> None:
        """Periodically publish build progress heartbeats to reset nri-wait timeout."""
        logger = structlog.get_logger("nixkube.nri.buildpump")
        try:
            while True:
                await asyncio.sleep(10)
                await self.zmq_server.publish_build_progress(container_id)
        except asyncio.CancelledError:
            logger.debug("progress_pump_cancelled")

    async def _spawn_build_task(
        self,
        container_id: str,
        container_name: str,
        pod: Pod,
        store_paths: set[Path],
        store_mounts: dict[Path, Path] | None = None,
        nix_rw: bool = False,
    ) -> None:
        """Realize store paths, link into the volume, then namespace-mount store mounts.

        Periodically pumps progress updates to reset nri-wait timeout.
        """
        log = structlog.get_logger("nixkube.nri.buildtask").bind(
            container_id=container_id
        )
        log.info("build_task_started", count=len(store_paths))
        pump_task: asyncio.Task | None = None
        try:
            # If no store paths to build, just mark as done
            if not store_paths:
                log.info("no_store_paths")
                self.zmq_server.build_status[container_id] = {"status": "done"}
                await self.zmq_server.publish_build_complete(container_id)
                self.zmq_server.pending_builds.discard(container_id)
                return

            # Start progress pump to keep nri-wait timeout reset during long builds
            pump_task = asyncio.create_task(self._pump_build_progress(container_id))
            log.debug("progress_pump_started")

            # Get extra build args for builders and cache
            extra_args = await get_build_args()

            # Realize storepaths
            volume_path = NRI_CONTAINERS / container_id
            log.debug("fetch_packages_starting", count=len(store_paths))
            await fetch_packages(store_paths, volume_path, extra_args)
            log.debug("fetch_packages_done")

            # Hardlink closure into volume (prepare_volume handles closure expansion)
            await prepare_volume(volume_path, store_paths, None)
            nix_tree_path = volume_path / "nix"

            # Wait for nri-wait to report PID+bundle (arrives when the createRuntime hook fires).
            # We need the PID to enter the container's mount namespace and mount /nix + store mounts.
            log.debug("pid_bundle_waiting")
            container_info = await self.zmq_server.wait_for_pid(container_id)
            pid_info = container_info[0] if container_info else None
            log.debug("pid_bundle_received", pid=pid_info)
            if container_info is None:
                raise RuntimeError(
                    f"No PID/bundle received for container={container_id!r}, cannot mount /nix"
                )
            pid, bundle = container_info

            mounts = []
            if store_mounts:
                for container_path, store_path in store_mounts.items():
                    resolved = store_path.resolve()
                    if not resolved.exists():
                        raise ValueError(
                            f"Invalid store path in annotation: {store_path!r} → {container_path!r} "
                            f"(resolved: {resolved!r} does not exist)"
                        )
                    mounts.append((resolved, container_path))

            log.info("namespace_mounting", pid=pid, bundle=bundle, mounts=len(mounts))
            await mount_in_container(pid, bundle, nix_tree_path, mounts, nix_rw)

            log.info("build_task_completed")
            self.zmq_server.build_status[container_id] = {"status": "done"}
            log.debug("build_status_updated")
            await self.zmq_server.publish_build_complete(container_id)
            self.zmq_server.pending_builds.discard(container_id)
            log.info("removed_from_pending")

            # Copy all packages to cache in background
            schedule_copy_to_cache(store_paths)

            # Report successful build
            await report_event(
                pod,
                reason="BuildSucceeded",
                note=f"Successfully built {len(store_paths)} store path(s)",
                event_type="Normal",
            )
        except Exception as e:
            log.exception("build_task_failed")
            self.zmq_server.pending_builds.discard(container_id)

            # Report failed build
            await report_event(
                pod,
                reason="BuildFailed",
                note=f"Failed to build store paths for container {container_name}",
                logs=str(e),
                event_type="Warning",
            )
            raise
        finally:
            # Cancel progress pump if it's still running
            if pump_task is not None:
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass


async def nri_serve() -> None:
    """Run the NRI plugin server with automatic reconnection and kernel checks."""
    logger = structlog.get_logger("nixkube.nri.serve")

    # Test kernel capabilities at startup
    if not kernel_supports_ro():
        # RO support is required; fail hard
        logger.critical("kernel_ro_unsupported")
        await report_event(
            None,
            reason="KernelIncompatible",
            note="NRI plugin failed: kernel does not support open_tree/move_mount (requires Linux 5.2+)",
            event_type="Warning",
        )
        raise RuntimeError("Kernel does not support required mount API syscalls")

    if not kernel_supports_rw():
        # RW support is optional; warn but continue
        logger.warning(
            "kernel_rw_unsupported",
            note="RW /nix mounts will be unavailable (requires Linux 6.5+ for fsopen/fsconfig/fsmount)",
        )
        await report_event(
            None,
            reason="KernelLimited",
            note="NRI plugin running in RO-only mode: kernel does not support new mount API for overlayfs (fsopen/fsconfig/fsmount, requires Linux 6.5+)",
            event_type="Warning",
        )

    # Initialize ZeroMQ server
    zmq_server = ZeroMQServer()
    await zmq_server.initialize()

    # Discover CRI socket and verify connectivity. Without CRI access, garbage
    # collection of stale volumes won't work and the node will fill up.
    cri_socket = await get_cri_socket()
    containers = await list_container_ids(HOST_ROOT / cri_socket.relative_to("/"))
    logger.info("cri_connected", container_count=len(containers))

    # Create plugin instance and NRI server
    plugin = NriPlugin(zmq_server, cri_socket)
    server = NriServer(
        plugin,
        socket_path=Path(NRI_RUNTIME_SOCKET),
        plugin_name=NRI_PLUGIN_NAME,
        plugin_idx=NRI_PLUGIN_IDX,
    )

    # Start ZeroMQ handler in background
    loop = asyncio.get_running_loop()
    loop.create_task(zmq_server.start_request_handler())

    try:
        # Start server (handles reconnection with exponential backoff internally)
        await server.start()
    finally:
        await server.close()
        zmq_server.shutdown()
