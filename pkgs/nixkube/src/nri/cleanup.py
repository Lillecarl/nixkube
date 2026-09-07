# SPDX-License-Identifier: MIT
"""NRI container cleanup and garbage collection."""

import asyncio
import shutil
from pathlib import Path

import structlog

from ..constants import HOST_ROOT, NRI_CONTAINERS
from ..cri import list_container_ids

logger = structlog.get_logger("nixkube.nri.cleanup")


def schedule_garbage_collection(
    cri_socket: Path, removed_id: str | None = None
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
    """
    task = asyncio.create_task(garbage_collect_stale_volumes(cri_socket, removed_id))
    task.add_done_callback(
        lambda t: (
            logger.error("gc_task_failed", exc_info=t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )


async def garbage_collect_stale_volumes(
    cri_socket: Path, removed_id: str | None = None
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
    """
    if removed_id:
        volume_dir = NRI_CONTAINERS / removed_id
        try:
            if volume_dir.is_dir():
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
