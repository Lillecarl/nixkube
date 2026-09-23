# SPDX-License-Identifier: MIT
"""NRI container cleanup and garbage collection."""

import shutil
from pathlib import Path

import anyio.abc
import structlog

from ..constants import HOST_ROOT, NRI_BUILD_CANCEL_TIMEOUT, NRI_CONTAINERS
from ..cri import list_container_ids
from ..supervision import detach
from .builds import BuildRegistry

logger = structlog.get_logger("nixkube.nri.cleanup")


def schedule_garbage_collection(
    tasks: anyio.abc.TaskGroup,
    cri_socket: Path,
    removed_id: str | None = None,
    building: BuildRegistry | None = None,
) -> None:
    """Collect in the background, so the NRI reply does not wait for it.

    containerd puts a deadline on every NRI request, and a StateChange is a
    notification: the runtime wants the acknowledgement, not the work. This
    sweep removes a hardlink farm and asks the CRI what is still alive,
    which on a busy node is far slower than an acknowledgement should be.

    Measured: one REMOVE_CONTAINER answered past the deadline, containerd
    dropped the connection, and the plugin re-registered. Every container
    created in that window starts with no /nix, which is the failure this
    plugin exists to prevent.

    The sweep also cancels the removed container's own build and waits for
    it, which is the other reason it cannot run in the handler.
    """
    detach(
        tasks,
        garbage_collect_stale_volumes,
        cri_socket,
        removed_id,
        building,
        name="nri_gc",
    )


async def garbage_collect_stale_volumes(
    cri_socket: Path,
    removed_id: str | None = None,
    building: BuildRegistry | None = None,
) -> None:
    """Remove volumes for containers no longer in CRI.

    Queries the CRI to get the list of active containers and removes any
    stale volume directories for containers that are no longer running.

    ``removed_id`` is the container this sweep was triggered by, and it is
    removed by name rather than left to the CRI comparison. At the moment
    NRI delivers REMOVE_CONTAINER the runtime still lists that container,
    so a sweep that trusted the CRI alone would keep its directory and only
    collect it on the *next* removal. Measured: on an idle node the last
    container removed kept its hardlink farm indefinitely, which holds
    store paths against garbage collection.

    The CRI comparison stays, as the backstop it always was -- it is what
    collects directories left behind by a crash or an abrupt shutdown,
    where no REMOVE_CONTAINER arrives at all.

    ``building`` holds the containers whose build task is still filling a
    volume, and neither rule touches one of those. A build walks its closure
    with a checkpoint every 256 entries, so on a large closure it is minutes
    long and interleaved with everything else on the loop -- including this
    sweep. Removing underneath it deleted the destination mid-link, and the
    resulting `FileNotFoundError` names the *source* store path, so it read
    as a corrupt store rather than as a race (issue #64). It is read here
    and not at schedule time because a build may start after the sweep is
    queued.

    The container being removed is the one case where waiting is not needed.
    Its build has nothing left to build for, so the sweep cancels it and
    waits for it to unwind before collecting -- otherwise that volume is
    skipped and nothing collects it until some *other* container is removed,
    which on an idle node is never.

    A directory skipped by the sweep is not leaked for long: the container is
    gone from the CRI, so the next sweep collects it.
    """
    if building is None:
        building = BuildRegistry()

    if removed_id:
        # Before the skip below reads it. A build whose container is gone has
        # nothing to finish, and the volume cannot be collected until it
        # stops writing into it.
        await building.stop(removed_id, NRI_BUILD_CANCEL_TIMEOUT)

        volume_dir = NRI_CONTAINERS / removed_id
        try:
            if removed_id in building:
                logger.info("gc_skipped_building", volume=removed_id)
            elif volume_dir.is_dir():
                shutil.rmtree(volume_dir)
                logger.debug("gc_removed_volume", volume=removed_id)
        except Exception:
            logger.warning("gc_remove_failed", volume=str(volume_dir), exc_info=True)

    try:
        # Get list of active containers from CRI
        # Access socket through host mount since we're in a container
        socket_path = HOST_ROOT / cri_socket.relative_to("/")
        active_ids = await list_container_ids(socket_path)
        logger.debug("gc_active_containers", count=len(active_ids))

        # Clean up volumes for containers not in active list
        if NRI_CONTAINERS.exists():
            stale_count = 0
            for volume_dir in NRI_CONTAINERS.iterdir():
                if volume_dir.name in building:
                    continue
                if volume_dir.is_dir() and volume_dir.name not in active_ids:
                    try:
                        shutil.rmtree(volume_dir)
                        stale_count += 1
                        logger.debug("gc_removed_stale_volume", volume=volume_dir.name)
                    except Exception:
                        logger.warning(
                            "gc_remove_failed",
                            volume=str(volume_dir),
                            exc_info=True,
                        )
            if stale_count > 0:
                logger.info("gc_cleaned_nri_volumes", count=stale_count)
    except Exception:
        logger.warning("gc_failed", exc_info=True)
