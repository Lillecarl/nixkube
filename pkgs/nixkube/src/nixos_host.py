# SPDX-License-Identifier: MIT

"""Switch a NixOS host to the system its Node object names. Issue #24.

The node store is not the host store: the container's /nix is
`hostMountPath`/nix, and the host's is /host/nix. So a toplevel is realised
here, copied to the host's nix-daemon, and activated in the host's mount
namespace. With `hostMountPath = /` the two stores are one and the copy finds
nothing to do.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import anyio
import kr8s.asyncio
import structlog

from .constants import HOST_ROOT, KUBE_NODE_NAME, NIX_BUILD_TIMEOUT, NIX_ROOT
from .errors import CommandTimeoutError, SubprocessError
from .events import report_event
from .nix.build import get_build_args
from .store import STORE_PATH_RE
from .subprocessing import try_console

logger = structlog.get_logger("nixkube.nixos_host")

TOPLEVEL_ANNOTATION = "nixkube/toplevel"
CURRENT_ANNOTATION = "nixkube/current-system"

HOST = HOST_ROOT
HOST_DAEMON = f"unix://{HOST}/nix/var/nix/daemon-socket/socket"
# PID 1's, and not a path under /host: /host/run is a bind of the host's /run,
# but a process that runs there still sees the container's /nix.
HOST_MOUNT_NS = HOST / "proc/1/ns/mnt"
SYSTEM_PROFILE = "/nix/var/nix/profiles/system"

# Not under CSI_GCROOTS, which the CSI cleanup sweeps for volumes it no longer
# knows.
GC_ROOT = NIX_ROOT / "nix/var/nix/gcroots/nixkube-host/system"

# A switch restarts units, and a kubelet or containerd restart takes this pod's
# API access with it for a while. The switch itself runs as a host unit, so it
# finishes whatever happens to this process.
SWITCH_TIMEOUT = 900.0


class HostSwitchError(Exception):
    """A step towards the annotated system failed. The host is unchanged
    unless the step was the switch itself."""


def wanted_toplevel(annotations: Mapping[str, str], current: str | None) -> str | None:
    """The system to switch to, or None when there is nothing to do.

    Raises HostSwitchError for an annotation that is not a store path, so a
    typo is reported and not activated.
    """
    desired = annotations.get(TOPLEVEL_ANNOTATION)
    if not desired or desired == current:
        return None
    if not STORE_PATH_RE.fullmatch(desired):
        raise HostSwitchError(f"{TOPLEVEL_ANNOTATION}={desired!r} is not a store path")
    return desired


def switch_unit(toplevel: str) -> str:
    """A unit name per system, so the host journal keeps each switch apart."""
    return f"nixkube-switch-{hashlib.sha256(toplevel.encode()).hexdigest()[:12]}"


def in_host(*args: str) -> list[str]:
    return ["nsenter", f"--mount={HOST_MOUNT_NS}", "--", *args]


async def current_system() -> str | None:
    try:
        return str(await anyio.Path(HOST / "run/current-system").readlink())
    except FileNotFoundError:
        return None


async def _step(what: str, *args: str | Path, timeout: float) -> None:
    try:
        await try_console(*args, timeout=timeout)
    except (SubprocessError, CommandTimeoutError) as e:
        raise HostSwitchError(f"{what} failed:\n{e.combined}") from e


async def host_has(toplevel: str) -> bool:
    """Whether the host store already holds the system: a rollback, or a
    system the host built itself, needs nothing from this node's store."""
    try:
        await try_console(
            "nix", "path-info", "--store", HOST_DAEMON, toplevel, timeout=60
        )
    except (SubprocessError, CommandTimeoutError):
        return False
    return True


async def switch_host(toplevel: str) -> None:
    if not await host_has(toplevel):
        await _realise_into_host(toplevel)
    await _activate(toplevel)


async def _realise_into_host(toplevel: str) -> None:
    log = logger.bind(toplevel=toplevel)
    log.info("host_system_realising")
    GC_ROOT.parent.mkdir(parents=True, exist_ok=True)
    await _step(
        "realising the system",
        "nix", "build", *await get_build_args(), "--out-link", GC_ROOT, toplevel,
        timeout=NIX_BUILD_TIMEOUT,
    )  # fmt: skip

    log.info("host_system_copying")
    # --no-check-sigs: this process is root, which the host daemon trusts,
    # and the path came through this node's own substituters.
    await _step(
        "copying the system to the host store",
        "nix", "copy", "--no-check-sigs", "--to", HOST_DAEMON, toplevel,
        timeout=NIX_BUILD_TIMEOUT,
    )  # fmt: skip


async def _activate(toplevel: str) -> None:
    log = logger.bind(toplevel=toplevel)
    # The profile first, as nixos-rebuild does: it is what the bootloader
    # entry and the next boot read, and the generation it adds is a GC root
    # on the host.
    log.info("host_system_profile")
    await _step(
        "setting the system profile",
        *in_host(
            f"{toplevel}/sw/bin/nix-env", "--profile", SYSTEM_PROFILE, "--set", toplevel
        ),
        timeout=60,
    )

    unit = switch_unit(toplevel)
    log.info("host_system_switching", unit=unit)
    await _step(
        f"switch-to-configuration (host journal: journalctl --unit {unit})",
        *in_host(
            f"{toplevel}/sw/bin/systemd-run",
            f"--unit={unit}",
            "--collect",
            "--wait",
            "--quiet",
            "--service-type=exec",
            "--property=KillMode=process",
            f"{toplevel}/bin/switch-to-configuration",
            "switch",
        ),
        timeout=SWITCH_TIMEOUT,
    )
    log.info("host_system_switched")


async def reconcile(node: kr8s.asyncio.objects.Node) -> None:
    current = await current_system()
    try:
        toplevel = wanted_toplevel(node.annotations, current)
        if toplevel is None:
            if (
                current is not None
                and node.annotations.get(CURRENT_ANNOTATION) != current
            ):
                await node.annotate({CURRENT_ANNOTATION: current})
            return
        await switch_host(toplevel)
    except HostSwitchError as e:
        logger.warning("host_switch_failed", error=str(e))
        await report_event(None, "HostSwitchFailed", note=str(e), event_type="Warning")
        return

    await node.annotate({CURRENT_ANNOTATION: toplevel})
    await report_event(None, "HostSwitched", note=f"{KUBE_NODE_NAME} runs {toplevel}")


async def nixos_host_loop() -> None:
    """Watch this node's Node object and act on each change.

    The watch replays the object as ADDED when it opens, so a restart
    reconciles once before it waits.
    """
    if not await anyio.Path(HOST / "etc/NIXOS").exists():
        logger.warning("nixos_host_not_nixos", node=KUBE_NODE_NAME)
        await anyio.sleep_forever()

    api = await kr8s.asyncio.api()
    while True:
        async for event, node in api.watch(
            "nodes", field_selector=f"metadata.name={KUBE_NODE_NAME}"
        ):
            if event in ("ADDED", "MODIFIED"):
                await reconcile(node)  # type: ignore[arg-type]
