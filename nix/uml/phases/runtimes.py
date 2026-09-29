"""What each RuntimeClass the node offers gets from nixkube. Issue #74.

A runtime that runs the container as a host process gets both mounts, as
runc does. A VM runtime gets both read-only, through virtio-fs. A sandboxed
one gets CSI, and NRI refuses it by name.
"""

from nixkube_uml import check_sandboxed, check_vm, probe
from uml_runner import Machines

# The pid a sandboxed runtime reports is not the container's, so NRI cannot
# reach it. Measured for gVisor; see `check_sandboxed`.
SANDBOXED = {"runsc"}

# `nixkube.nri.vmRuntimeHandlers`' default.
VM = {"kata"}


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    for runtime in settings["runtimes"]:
        if runtime in SANDBOXED:
            await check_sandboxed(cp, settings, runtime)
        elif runtime in VM:
            await check_vm(cp, settings, runtime)
        else:
            await probe(cp, settings, "a clean start", runtime_class=runtime)
