#!/usr/bin/env python3
"""nixkube on a real node, in a Nix build sandbox.

The manifest applies, the node DaemonSet reaches Ready, and a workload gets
the store path it asked for through a CSI ephemeral volume.

What is being proved is narrow and load-bearing. The DaemonSet's init
container fills the node's Nix store, and a build sandbox has no network. It
works here because the node's store is already the right one: `hostMountPath
= "/"` points nixkube at the guest's own /nix, which is the sandbox's over
hostfs, and `boot.uml.nixDatabase` registers it so Nix agrees the paths are
real. Nothing is fetched and nothing is copied.
"""

from uml_runner import MachineError, run_test
from uml_runner.cluster import KUBE_PROXY, bring_up, get_json, kubectl, until

NAMESPACE = "nixkube"
DAEMONSET = "nix-node"

# The jobs ./workloads.nix adds, by name.
WORKLOADS = ("csi-path", "csi-shared")

# These are stuck-detectors, not patience.
#
# Nothing here is slow. The images are already on the node, the closure the
# init container copies is already in the guest's own store, and the copy is
# a local HTTP fetch of a few hundred megabytes. A minute is a lot; five is
# generous. Anything past that is not slow, it is stuck -- waiting longer
# turns a bug into a delay and hides it.
#
# So when one of these fires, it means something is wrong and the report has
# to say what. `until` dumps the pod list, the events, kubelet, containerd
# and the pod logs.
APPLY_TIMEOUT = 5 * 60
READY_TIMEOUT = 5 * 60


async def check_one_mount(cp):
    """The guest's /nix has to be one mount, before anything else runs.

    The node container mounts the host's / with `subPath = "nix"`. kubelet
    implements a subPath with a plain `mount --bind`, which is not
    recursive. So a guest that keeps /nix/store as a mount of its own hands
    that container an empty /nix/store, and every store path in it fails to
    open. The symptom names nothing: the entrypoint is on PATH under
    /nix/var/result/bin, so runc reports `exec: "tini": executable file not
    found in $PATH` and no log line anywhere says why.

    user-mode-nixos overlays all of /nix for this reason. Check it here,
    where the message can say what broke, rather than five minutes later in
    a crash loop.
    """
    rc, out = await cp.execute(
        "findmnt --noheadings --output TARGET,FSTYPE --target /nix/var",
        timeout=60,
    )
    if rc != 0 or out.split()[:2] != ["/nix", "overlay"]:
        raise MachineError(
            "[cp] the guest's /nix is not one overlay mount, so a pod that"
            f" mounts it with a subPath will see an empty store:\n{out}"
        )
    rc, out = await cp.execute("findmnt --noheadings /nix/store", timeout=60)
    if rc == 0:
        raise MachineError(
            "[cp] /nix/store is a mount inside /nix, so a subPath bind of"
            f" /nix drops it:\n{out}"
        )
    print("[nixkube] the guest's /nix is one mount", flush=True)


async def deploy(cp, settings):
    """Apply the manifest, the way the NixOS test does."""
    print("[nixkube] applying the manifest", flush=True)
    out = await kubectl(
        cp,
        f"apply --server-side --filename {settings['manifest']}",
        timeout=APPLY_TIMEOUT,
    )
    print(out, flush=True)


async def wait_for_driver(cp):
    """The DaemonSet's pod reaches Ready, and kubelet knows the driver.

    Reported together because either alone is misleading: a Ready pod whose
    registrar never reached kubelet cannot serve a volume, and a CSIDriver
    object exists from the moment the manifest applies.
    """

    async def ready():
        data = await get_json(cp, f"get daemonset {DAEMONSET} --namespace {NAMESPACE}")
        status = data.get("status", {})
        want = status.get("desiredNumberScheduled", 0)
        got = status.get("numberReady", 0)
        pods = await kubectl(
            cp,
            f"get pods --namespace {NAMESPACE} --output wide --no-headers",
        )
        # `until` prints this evidence's first line about once a minute, so
        # put the summary there and the detail underneath. This is the long
        # wait -- the init container copies the node environment over HTTP
        # onto a UML block device -- and silence here reads as a hang.
        # NAME READY STATUS ... -- name and status is what says whether this
        # is progressing, pulling, or stuck in a crash loop.
        seen = [line.split() for line in pods.splitlines() if line.split()]
        summary = " | ".join(f"{c[0]}={c[2]}" for c in seen if len(c) > 2) or "no pods yet"
        return (
            bool(want) and got == want,
            f"{got}/{want} ready, pods {summary}\n{pods}",
        )

    await until("the node DaemonSet", ready, READY_TIMEOUT, cp)
    print("[nixkube] the node DaemonSet is Ready", flush=True)

    drivers = await kubectl(cp, "get csidriver --no-headers")
    if "nixkube" not in drivers:
        raise MachineError(f"[cp] the API server serves no nixkube CSIDriver:\n{drivers}")
    print(f"[nixkube] csidrivers:\n{drivers}", flush=True)


async def check_workloads(cp):
    """The driver mounts a store path where the pod asked for it.

    Both jobs run /mnt/csi/nix/store/...-hello/bin/hello -- through the
    volume, and not through /nix/store. That distinction is the whole
    assertion: user-mode-nixos bind-mounts the guest's store into every
    container, so a job that ran the same binary from /nix/store would
    complete with the driver switched off. Only the driver can put a file
    under /mnt/csi.

    They ask for the same store path at the same time, which is also the
    question a driver that shares one mount between volumes gets wrong.

    `backoffLimit = 0`, so a failure is final and this sees it at once
    rather than after six retries.
    """

    async def finished():
        data = await get_json(
            cp,
            f"get jobs --namespace {NAMESPACE}"
            " --selector app.kubernetes.io/component=uml-test",
        )
        seen = {
            item["metadata"]["name"]: item.get("status", {})
            for item in data.get("items", [])
        }
        failed = sorted(name for name, s in seen.items() if s.get("failed"))
        if failed:
            raise MachineError(
                f"[cp] the workload jobs failed: {', '.join(failed)}.\n"
                "The container's command is a path inside the CSI volume, so"
                " this says the driver did not mount it there."
            )
        done = sorted(name for name, s in seen.items() if s.get("succeeded"))
        summary = ", ".join(f"{name}={'ok' if name in done else '...'}" for name in sorted(seen))
        return len(done) == len(WORKLOADS), summary or "no jobs yet"

    await until("the workload jobs", finished, READY_TIMEOUT, cp)

    for job in WORKLOADS:
        out = await kubectl(cp, f"logs --namespace {NAMESPACE} job/{job}")
        if "Hello, world!" not in out:
            raise MachineError(f"[cp] {job} completed but said nothing:\n{out}")
    print(f"[nixkube] {', '.join(WORKLOADS)} ran out of the volume", flush=True)


async def check_unmount(cp):
    """The mounts go away when the pods do.

    A driver that mounts and never unmounts looks exactly like a working
    one, until a node has run for a week. The jobs are gone by the end of
    this, which is why it runs last: `report` can no longer read their
    logs, and `check_workloads` has already read them.
    """
    await kubectl(
        cp,
        f"delete job --namespace {NAMESPACE} {' '.join(WORKLOADS)} --wait=true",
        timeout=APPLY_TIMEOUT,
    )

    async def gone():
        rc, out = await cp.execute(
            "findmnt --noheadings --output TARGET | grep kubernetes.io~csi || true",
            timeout=60,
        )
        left = [line.strip() for line in out.splitlines() if line.strip()]
        return not left, f"{len(left)} csi mounts left\n" + "\n".join(left[:10])

    await until("the csi mounts to go away", gone, READY_TIMEOUT, cp)
    print("[nixkube] the csi mounts went away with the pods", flush=True)


async def report(cp):
    """What the driver's own containers said, pass or fail.

    This runs for an hour on a builder. A log that says only "passed" makes
    the next failure start from nothing.
    """
    for what in (
        f"get all --namespace {NAMESPACE}",
        f"get events --namespace {NAMESPACE} --sort-by=.lastTimestamp",
    ):
        rc, out = await cp.execute(f"kubectl {what}", timeout=120)
        print(f"[nixkube] kubectl {what} (rc={rc}):\n{out}", flush=True)

    # What the node container's /nix is made of, from the node's side.
    #
    # The init container writes an out-link at /nix/var/result and the node
    # container's PATH is /nix/var/result/bin. When that entrypoint does not
    # resolve, runc says only that it is "not found", so these three answer
    # the question runc will not: is the link there, does it point at
    # something, and did kubelet's subPath bind carry the store.
    rc, out = await cp.execute(
        "ls -l /nix/var/result;"
        " ls /nix/var/result/bin 2>&1 | head -n 5;"
        " findmnt --output TARGET,SOURCE,FSTYPE | grep -i nix;"
        " ls -d /var/lib/kubelet/pods/*/volume-subpaths/*/*/* 2>&1 | head -n 10",
        timeout=120,
    )
    print(f"[nixkube] the node's /nix (rc={rc}):\n{out}", flush=True)

    # Every container of the DaemonSet's pod, by name, from the API server.
    #
    # `diagnose` tails the log files off the node, which works but competes
    # for a line budget with everything else running. Asking kubelet for one
    # container's output is exact, and `--previous` is the only way to see a
    # container that has already exited -- which a crash-looping init
    # container always has by the time anybody looks.
    pods = await cp.execute(
        f"kubectl get pods --namespace {NAMESPACE}"
        " --selector app.kubernetes.io/component=node"
        " --output jsonpath={.items[*].metadata.name}",
        timeout=120,
    )
    for pod in pods[1].split():
        for container in ("initcopy", "nix-node"):
            for flags in ("", " --previous"):
                rc, out = await cp.execute(
                    f"kubectl logs --namespace {NAMESPACE} {pod}"
                    f" --container {container}{flags} --tail 60",
                    timeout=120,
                )
                if rc == 0 and out.strip():
                    print(
                        f"[nixkube] logs {pod} {container}{flags}:\n{out}",
                        flush=True,
                    )

    # What the workloads said. A job that fails here fails for one of two
    # reasons -- the driver did not mount, or the thing it mounted is not
    # what the pod asked for -- and the container's own output separates
    # them.
    for job in WORKLOADS:
        rc, out = await cp.execute(
            f"kubectl logs --namespace {NAMESPACE} job/{job} --tail 40",
            timeout=120,
        )
        print(f"[nixkube] logs job/{job} (rc={rc}):\n{out}", flush=True)


async def test(vms):
    settings = vms.settings
    print(
        f"[nixkube] kubernetes {settings['kubernetesVersion']},"
        " the node's store is the sandbox's",
        flush=True,
    )

    # kube-proxy but not CoreDNS, matching `services.uml-k8s.skipAddons`.
    # Nothing resolves a name here, but everything in a pod reaches the API
    # server through the `kubernetes.default` ClusterIP, and kube-proxy is
    # what makes that address go anywhere.
    cp = await bring_up(vms, addons=(KUBE_PROXY,))

    try:
        await check_one_mount(cp)
        await deploy(cp, settings)
        await wait_for_driver(cp)
        await check_workloads(cp)
        await check_unmount(cp)
    except Exception:
        # `until` already attaches `diagnose` -- kubelet, containerd, crictl
        # and the pod logs -- to the message it raises. What it cannot know
        # about is this namespace, so add that and nothing else.
        await report(cp)
        raise
    await report(cp)


run_test(test)
