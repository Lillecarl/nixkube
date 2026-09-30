"""The node switches its NixOS host to the system its Node names. Issue #24.

`hostNext` is this guest's own configuration plus one file, so the switch
changes that file and nothing the cluster runs on. The phase switches there
and back, and refuses a value that is not a store path.
"""

from nixkube_uml import READY_TIMEOUT
from vivarium_runner import Machine, MachineError, Machines
from vivarium_runner.cluster import get_json, kubectl, until

TOPLEVEL = "nixkube/toplevel"
CURRENT = "nixkube/current-system"
MARKER = "/etc/nixkube-host-generation"


async def running(cp: Machine) -> str:
    return (await cp.succeed("readlink /run/current-system")).strip()


async def switch_to(cp: Machine, toplevel: str) -> None:
    await kubectl(cp, f"annotate node cp --overwrite {TOPLEVEL}={toplevel}")

    async def switched() -> tuple[bool, str]:
        now = await running(cp)
        node = await get_json(cp, "get node cp")
        reported = node["metadata"].get("annotations", {}).get(CURRENT)
        ready = any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"]["conditions"]
        )
        done = now == toplevel and reported == toplevel and ready
        return done, f"running {now}, node says {reported}, Ready={ready}"

    await until(f"the host to run {toplevel}", switched, READY_TIMEOUT, cp)


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    first = await running(cp)
    after = settings["hostNext"]
    if first == after:
        raise MachineError(f"[cp] hostNext is the system the guest booted: {first}")

    await switch_to(cp, after)
    marker = (await cp.succeed(f"cat {MARKER}")).strip()
    if marker != "next":
        raise MachineError(f"[cp] {MARKER} says {marker!r} after the switch")
    profile = (
        await cp.succeed("readlink --canonicalize /nix/var/nix/profiles/system")
    ).strip()
    if profile != after:
        raise MachineError(f"[cp] the system profile is {profile}, not {after}")
    print(f"[nixkube] the host switched to {after}", flush=True)

    await switch_to(cp, first)
    rc, _ = await cp.execute(f"test -e {MARKER}")
    if rc == 0:
        raise MachineError(f"[cp] {MARKER} survived the switch back")
    print(f"[nixkube] the host switched back to {first}", flush=True)

    # A value that is not a store path is reported and not acted on.
    await kubectl(cp, f"annotate node cp --overwrite {TOPLEVEL}=/tmp/not-a-system")

    async def refused() -> tuple[bool, str]:
        events = await kubectl(
            cp,
            "get events --namespace nixkube --field-selector reason=NixHostSwitchFailed"
            " --no-headers",
        )
        return "not a store path" in events, events.strip() or "no event yet"

    await until("the bad annotation to be refused", refused, READY_TIMEOUT, cp)
    if await running(cp) != first:
        raise MachineError("[cp] a refused annotation changed the running system")
    await kubectl(cp, f"annotate node cp {TOPLEVEL}-")
    print("[nixkube] a toplevel that is not a store path was refused", flush=True)
