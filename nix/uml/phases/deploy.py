"""nixkube's manifest applied, and its driver and pynixd up."""

from nixkube_uml import deploy, wait_for_driver, wait_for_pynixd
from uml_runner import Machines


async def test(vms: Machines) -> None:
    await deploy(vms.cp, vms.settings)
    await wait_for_driver(vms.cp)
    await wait_for_pynixd(vms.cp)
