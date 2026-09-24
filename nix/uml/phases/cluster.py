"""A one-node kubeadm cluster on the guest `cp`."""

from uml_runner import Machines
from uml_runner.cluster import KUBE_PROXY, bring_up


async def test(vms: Machines) -> None:
    print(
        f"[nixkube] kubernetes {vms.settings['kubernetesVersion']}, the node's store is the sandbox's",
        flush=True,
    )
    # kube-proxy but not CoreDNS, matching `services.uml-k8s.skipAddons`.
    # Nothing resolves a name here, but everything in a pod reaches the API
    # server through the `kubernetes.default` ClusterIP, and kube-proxy is
    # what makes that address go anywhere.
    await bring_up(vms, addons=(KUBE_PROXY,))
