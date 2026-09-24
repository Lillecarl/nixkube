"""On a clean start: the workloads get their store paths, and unmount is clean."""

from nixkube_uml import check_resident, check_unmount, check_workloads, probe
from uml_runner import Machines


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    await check_workloads(cp)
    await probe(cp, settings, "a clean start")
    await check_resident(cp, settings, "a clean start")
    await check_unmount(cp, settings)
