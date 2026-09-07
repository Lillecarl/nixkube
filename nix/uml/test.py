"""nixkube on a real node, in a Nix build sandbox.

The manifest applies, the node DaemonSet reaches Ready, and a workload gets
the store path it asked for through a CSI ephemeral volume.

What is being proved is narrow and load-bearing. The DaemonSet's init
container fills the node's Nix store by substituting into it, and a build
sandbox has no network. It works here because the guest's own /nix/store is
the sandbox's, over hostfs, so `local?trusted=true` already holds every path
the manifest names -- and `boot.uml.nixDatabase` is what makes Nix agree they
are real rather than go looking for them.
"""

from uml_runner import MachineError, run_test
from uml_runner.cluster import KUBE_PROXY, bring_up, get_json, kubectl, until

NAMESPACE = "nixkube"
DAEMONSET = "nix-node"

# The jobs ./workloads.nix adds, by name.
WORKLOADS = ("csi-path", "csi-shared")

# The probes ./probe.nix renders, as (what it wants of /nix, settings key).
# Created after the driver is Ready, never applied with the manifest -- an
# NRI pod waits for nothing, so an applied one starts before the plugin
# exists. See ./probe.nix.
PROBES = (("ro", "probeRo"), ("rw", "probeRw"))


def pod_summary(out):
    """`kubectl get pods --no-headers` with the clocks taken out.

    `until` reports how long its evidence has been *unchanged*, and that
    figure is the one that separates slow from stuck. Both AGE and RESTARTS
    carry a time in them, so evidence built from the whole line changes
    every poll and the answer is always "0s unchanged" -- a stuck-detector
    that cannot detect anything.

    Name, ready count and status. Those change when something happens, and
    only then.
    """
    rows = [
        line.split()
        for line in out.splitlines()
        if line.split() and not line.startswith("No resources found")
    ]
    return " | ".join(f"{r[0]}={r[1]} {r[2]}" for r in rows if len(r) > 2) or "no pods"


def nix_mount(mountinfo):
    """The line for the container's own /nix, from /proc/self/mountinfo.

    Fields up to the `-` separator are fixed: id, parent, device, root,
    mount point, options. Everything after it is the filesystem, whose
    fields vary. Returns (options, fstype) or None.

    A mount point of exactly `/nix` is what says the NRI plugin ran. The
    base runtime spec binds `/nix/store` into every container here, and
    never `/nix`.
    """
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[4] != "/nix":
            continue
        sep = fields.index("-")
        return fields[5], fields[sep + 1]
    return None


# These are stuck-detectors, not patience.
#
# Nothing here waits on anything far away. The images are already on the
# node, and the closure the init container copies comes from the guest's own
# store on the same disk. Five minutes is generous. Anything past that is not
# slow, it is stuck -- waiting longer turns a bug into a delay and hides it.
#
# So when one of these fires, it means something is wrong and the report has
# to say what. `until` dumps the pod list, the events, kubelet, containerd
# and the pod logs.
APPLY_TIMEOUT = 5 * 60
READY_TIMEOUT = 5 * 60


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
        # What says whether this is progressing, pulling, or stuck in a
        # crash loop -- and nothing that ticks on its own. See `pod_summary`.
        return (
            bool(want) and got == want,
            f"{got}/{want} ready, pods {pod_summary(pods)}",
        )

    await until("the node DaemonSet", ready, READY_TIMEOUT, cp)
    print("[nixkube] the node DaemonSet is Ready", flush=True)

    drivers = await kubectl(cp, "get csidriver --no-headers")
    if "nixkube" not in drivers:
        raise MachineError(
            f"[cp] the API server serves no nixkube CSIDriver:\n{drivers}"
        )
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
        summary = ", ".join(
            f"{name}={'ok' if name in done else '...'}" for name in sorted(seen)
        )
        if len(done) != len(WORKLOADS):
            # Which pod, and in what state. "csi-path=..." for five minutes
            # says nothing about why.
            pods = await kubectl(
                cp,
                f"get pods --namespace {NAMESPACE}"
                " --selector app.kubernetes.io/component=uml-test --no-headers",
            )
            summary = f"{summary} -- {pod_summary(pods)}"
        return len(done) == len(WORKLOADS), summary or "no jobs yet"

    await until("the workload jobs", finished, READY_TIMEOUT, cp)

    for job in WORKLOADS:
        out = await kubectl(cp, f"logs --namespace {NAMESPACE} job/{job}")
        if "Hello, world!" not in out:
            raise MachineError(f"[cp] {job} completed but said nothing:\n{out}")
    print(f"[nixkube] {', '.join(WORKLOADS)} ran out of the volume", flush=True)


async def probe(cp, settings, why):
    """Create one pod that wants both mount paths, and see that it gets them.

    This is the question every chaos scenario asks: after that, can a pod
    still start? A pod that was already running cannot answer it, so each
    scenario makes a new one.

    Two containers, two answers. `csi` runs a binary that only the driver
    can put where the pod asked. `nri` prints its own mount table, because
    running a binary proves nothing here -- /nix/store is bound into every
    container, so it would run with the plugin switched off. A mount point
    of exactly `/nix` is the plugin's work and nobody else's.

    Read-only and read-write are told apart by the mount options, not the
    filesystem type: the plugin builds the writable one as an overlay, and
    the guest's own /nix is an overlay too, so the type says nothing.
    """
    for want, key in PROBES:
        created = await kubectl(
            cp,
            f"create --namespace {NAMESPACE} --filename {settings[key]} --output name",
            timeout=APPLY_TIMEOUT,
        )
        name = created.strip().splitlines()[-1].split("/")[-1]

        async def done(name=name, want=want):
            data = await get_json(cp, f"get job {name} --namespace {NAMESPACE}")
            status = data.get("status", {})
            if status.get("failed"):
                pods = await kubectl(
                    cp,
                    f"get pods --namespace {NAMESPACE}"
                    f" --selector job-name={name} --output wide --no-headers",
                )
                raise MachineError(
                    f"[cp] a {want} probe failed after {why}. A pod that"
                    f" cannot start is the whole question here.\n{pods}"
                )
            pods = await kubectl(
                cp,
                f"get pods --namespace {NAMESPACE}"
                f" --selector job-name={name} --no-headers",
            )
            return bool(status.get("succeeded")), f"{name}: {pod_summary(pods)}"

        await until(f"a {want} probe after {why}", done, READY_TIMEOUT, cp)

        csi = await kubectl(
            cp, f"logs --namespace {NAMESPACE} job/{name} --container csi"
        )
        if "Hello, world!" not in csi:
            raise MachineError(
                f"[cp] the {want} probe's csi container said nothing after"
                f" {why}, so the volume is not what the pod asked for:\n{csi}"
            )

        nri = await kubectl(
            cp, f"logs --namespace {NAMESPACE} job/{name} --container nri"
        )
        mount = nix_mount(nri)
        if mount is None:
            raise MachineError(
                f"[cp] the {want} probe has no mount at /nix after {why}, so"
                f" the NRI plugin did not touch it. Its mount table was:\n{nri}"
            )
        options, fstype = mount
        if want not in options.split(","):
            raise MachineError(
                f"[cp] the {want} probe wanted a {want} /nix after {why} and"
                f" got `{options}` ({fstype})."
            )

        await kubectl(
            cp,
            f"delete job {name} --namespace {NAMESPACE} --wait=true",
            timeout=APPLY_TIMEOUT,
        )
        print(f"[nixkube] a {want} probe got both mounts after {why}", flush=True)


def state_dirs(settings):
    """Where the driver's own state lands on the node.

    The driver's constants are pod paths -- CSI_ROOT is `/nix/var/nix-csi`
    inside the node container -- and the container's /nix is the host's
    `<hostMountPath>/nix`. So on the node they sit under that.
    """
    root = settings["hostMountPath"].rstrip("/")
    return {
        "volumes": f"{root}/nix/var/nix-csi/volumes",
        "containers": f"{root}/nix/var/nix-csi/containers",
        "gcroots": f"{root}/nix/var/nix/gcroots/nix-csi",
    }


# Everything the driver holds that the node no longer has. Empty is the pass.
#
# Not "the directories are empty". With NRI on, every container whose command
# names a store path gets an entry, and the control plane's own static pods
# are exactly that -- etcd, the API server, kube-proxy all run out of the
# store. The invariant is instead the one the driver's own garbage collection
# claims to keep: an entry for a container the CRI no longer lists, or a
# volume kubelet no longer records, is a leak.
def reconciled(settings):
    """Everything the driver holds that the node no longer has.

    Not "the directories are empty". With NRI on, every container whose
    command names a store path gets an entry, and the control plane's own
    static pods are exactly that -- etcd, the API server and kube-proxy all
    run out of the store. The invariant is instead the one the driver's own
    garbage collection claims to keep: an entry for a container the CRI no
    longer lists, or a volume kubelet no longer records, is a leak.

    The CRI is asked twice, before and after reading the directory, and an
    entry counts as stale only if it is in neither answer. A container
    created or removed while this runs would otherwise look like a leak,
    and does: measured, with a different container id each poll.

    `findmnt --list`, because the default output draws a tree and the
    box-drawing characters end up in the path.
    """
    d = state_dirs(settings)
    return f"""
    live=$(mktemp); have_c=$(mktemp); have_v=$(mktemp)
    crictl ps --all --quiet 2>/dev/null > "$live"
    ls -1 {d["containers"]} 2>/dev/null | sort > "$have_c"
    ls -1 {d["volumes"]} 2>/dev/null | sort > "$have_v"
    ls -1 {d["gcroots"]} 2>/dev/null | sort > "$have_v.g"
    crictl ps --all --quiet 2>/dev/null >> "$live"
    sort -u -o "$live" "$live"

    cat /var/lib/kubelet/pods/*/volumes/kubernetes.io~csi/*/vol_data.json 2>/dev/null \
      | jq --raw-output .volumeHandle 2>/dev/null | sort -u > "$live.v"

    comm -23 "$have_c" "$live"   | sed 's|^|stale container |'
    comm -23 "$have_v" "$live.v" | sed 's|^|stale volume |'
    comm -23 "$have_v.g" "$live.v" | sed 's|^|stale gcroot |'
    findmnt --list --noheadings --output TARGET | grep kubernetes.io~csi | while read -r t; do
      [ -f "$(dirname "$t")/vol_data.json" ] || echo "stale mount $t"
    done
    rm -f "$live" "$live.v" "$have_c" "$have_v" "$have_v.g"
"""


async def check_reconciled(cp, settings, what):
    """Nothing the driver kept outlives what the node still has."""

    async def clean():
        _, out = await cp.execute(reconciled(settings), timeout=120)
        left = [line.strip() for line in out.splitlines() if line.strip()]
        return not left, f"{len(left)} stale -- " + "; ".join(left[:6])

    await until(f"the node's state to settle after {what}", clean, READY_TIMEOUT, cp)
    print(f"[nixkube] nothing stale on the node after {what}", flush=True)


async def on_node(cp, command, timeout=120):
    """Do something to the node, and say what came of it.

    `execute` rather than `succeed`: several of these are expected to return
    non-zero, and the interesting failures are the ones that come afterwards.
    """
    rc, out = await cp.execute(command, timeout=timeout)
    print(f"[nixkube] $ {command}\n  rc={rc} {out.strip()[:300]}", flush=True)
    return rc, out


NODE_SELECTOR = "--selector app.kubernetes.io/component=node"

# The container id and the sandbox id of the running driver, for the tools
# that work below Kubernetes.
NODE_CONTAINER = "$(crictl ps --quiet --name '^nix-node$')"
NODE_SANDBOX = "$(crictl pods --quiet --name '^nix-node-')"


async def kubernetes_deletes_the_pod(cp, settings):
    await kubectl(
        cp, f"delete pod --namespace {NAMESPACE} {NODE_SELECTOR} --wait=false"
    )


async def crictl_stops_the_container(cp, settings):
    await on_node(cp, f"crictl stop {NODE_CONTAINER}")


async def crictl_removes_the_container(cp, settings):
    await on_node(cp, f"crictl rm --force {NODE_CONTAINER}")


async def crictl_removes_the_sandbox(cp, settings):
    await on_node(cp, f"crictl rmp --force {NODE_SANDBOX}")


async def the_process_is_killed(cp, settings):
    # By pid from the CRI, not by name. The guest is the node, so a pattern
    # match would find the test's own tooling as readily as the driver.
    await on_node(
        cp,
        f"kill -9 $(crictl inspect {NODE_CONTAINER} | jq --raw-output .info.pid)",
    )


async def kubelet_restarts(cp, settings):
    await on_node(cp, "systemctl restart kubelet")


async def containerd_restarts(cp, settings):
    # The big one. Every container on the node dies, including the control
    # plane, and the NRI connection the plugin holds goes with it. A
    # containerd upgrade does exactly this.
    await on_node(cp, "systemctl restart containerd")


async def the_csi_socket_is_deleted(cp, settings):
    await on_node(cp, "rm -f /var/lib/kubelet/plugins/nixkube/csi.sock")


async def the_state_is_wiped(cp, settings):
    d = state_dirs(settings)
    await on_node(cp, f"rm -rf {d['volumes']}/* {d['containers']}/*")


# What the pod that was already running is entitled to afterwards.
#
# KEEPS is the normal answer, and it is the strong one: the scenario breaks
# the driver and leaves the node's files alone, so a pod that was already
# served must still hold its mounts.
#
# REPLACED is for the one scenario that deletes those files. The resident's
# CSI volume root is among them, and a bind mount whose source is unlinked
# goes on serving the unlinked directory -- the kernel marks it "//deleted"
# and nothing outside the pod's mount namespace can repair it. Asking that
# pod to still work would be asking for the impossible, so the question
# becomes the honest one: does the Deployment's replacement come up whole?
KEEPS = "keeps its mounts"
REPLACED = "is replaced"

SCENARIOS = (
    ("kubernetes deletes the pod", kubernetes_deletes_the_pod, KEEPS),
    ("crictl stops the container", crictl_stops_the_container, KEEPS),
    ("crictl removes the container", crictl_removes_the_container, KEEPS),
    ("crictl removes the sandbox", crictl_removes_the_sandbox, KEEPS),
    ("the process is killed outright", the_process_is_killed, KEEPS),
    ("kubelet restarts", kubelet_restarts, KEEPS),
    ("containerd restarts", containerd_restarts, KEEPS),
    ("its csi socket is deleted", the_csi_socket_is_deleted, KEEPS),
    ("its state directories are wiped", the_state_is_wiped, REPLACED),
)


async def wait_for_apiserver(cp):
    """The API server answers again.

    `kubectl` through `succeed` raises, and half of these scenarios take the
    control plane down with them, so the recovery wait cannot use it.
    """

    async def up():
        rc, out = await cp.execute("kubectl get --raw /healthz", timeout=60)
        return rc == 0, out.strip()[:80] or f"rc={rc}"

    await until("the api server to answer", up, READY_TIMEOUT, cp)


async def chaos(cp, settings):
    """Break the driver every way there is, and ask if a pod can still start.

    nixkube is not a component a node can do without: a pod that wants a
    store path does not start until the driver serves it. So the question
    after each of these is never "did it come back" alone -- it is whether a
    *new* pod gets both mounts, and whether the driver left anything behind.

    `NIXKUBE_UML_SCENARIOS` selects a subset by substring, for iterating on
    one of them without paying for the other eight.
    """
    only = [s for s in settings.get("scenarios", "").split(",") if s.strip()]

    for name, action, resident in SCENARIOS:
        if only and not any(s.strip() in name for s in only):
            continue
        print(f"\n[nixkube] ======== {name} ========", flush=True)
        await action(cp, settings)
        await wait_for_apiserver(cp)
        await wait_for_driver(cp)
        await probe(cp, settings, name)
        if resident is REPLACED:
            # `--wait` by default, so the old pod is gone before
            # `check_resident` counts pods and finds the new one.
            await kubectl(cp, f"delete pod --namespace {NAMESPACE} {RESIDENT}")
        await check_resident(cp, settings, name)
        await check_reconciled(cp, settings, name)


RESIDENT = "--selector app.kubernetes.io/component=uml-resident"


async def check_resident(cp, settings, why):
    """The pod that was already running still has what it was given.

    A driver that comes back but leaves the pods it was serving without
    their mounts has not recovered, it has restarted. A fresh probe cannot
    see that -- it gets its mounts from the driver that is up now.

    So this asks the pod itself. `kubectl exec` runs in the container's own
    mount namespace, so the mount table it prints is that container's, and
    the binary it runs only resolves through the CSI volume.
    """

    async def up():
        out = await kubectl(
            cp, f"get pods --namespace {NAMESPACE} {RESIDENT} --no-headers"
        )
        rows = [line.split() for line in out.splitlines() if line.split()]
        running = [
            r for r in rows if len(r) > 2 and r[2] == "Running" and r[1] == "1/1"
        ]
        return len(running) == 1, pod_summary(out)

    await until(f"the resident pod after {why}", up, READY_TIMEOUT, cp)

    name = (
        await kubectl(
            cp,
            f"get pods --namespace {NAMESPACE} {RESIDENT}"
            " --output jsonpath={.items[0].metadata.name}",
        )
    ).strip()

    hello = await kubectl(
        cp, f"exec --namespace {NAMESPACE} {name} -- {settings['residentHello']}"
    )
    if "Hello, world!" not in hello:
        raise MachineError(
            f"[cp] the resident pod lost its volume after {why}: its binary"
            f" under /mnt/csi said\n{hello}"
        )

    mounts = await kubectl(
        cp,
        f"exec --namespace {NAMESPACE} {name} --"
        f" {settings['residentCat']} /proc/self/mountinfo",
    )
    if nix_mount(mounts) is None:
        raise MachineError(
            f"[cp] the resident pod lost its /nix after {why}. Its mount"
            f" table was:\n{mounts}"
        )
    print(f"[nixkube] {name} still has both mounts after {why}", flush=True)


async def check_unmount(cp, settings):
    """The mounts go away when the pods do.

    A driver that mounts and never unmounts looks exactly like a working
    one, until a node has run for a week. `check_reconciled` is the same
    question the chaos scenarios ask, so it is the same answer here: once
    the jobs are gone, nothing of theirs may be left.

    The jobs are gone by the end of this, which is why it runs last among
    the clean-start checks -- `report` can no longer read their logs, and
    `check_workloads` has already read them. The resident Deployment stays
    up, and its volume is not stale, so it does not confuse this.
    """
    await kubectl(
        cp,
        f"delete job --namespace {NAMESPACE} {' '.join(WORKLOADS)} --wait=true",
        timeout=APPLY_TIMEOUT,
    )
    await check_reconciled(cp, settings, "the workload jobs are deleted")


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

    # What the nri-wait hook said before it gave up.
    #
    # The hook runs at createRuntime, so its stderr goes into runc's error,
    # into containerd's, and from there into a Kubernetes event. kubelet
    # cuts that event's message at about 1KB, which lands mid-sentence and
    # drops the one line that says which deadline the hook hit. containerd's
    # own journal keeps the whole thing. Measured: the event ended at
    # "[nri-wait] OCI state: id=hello pid=2081 bundle=..." and the verdict
    # was 900 characters further on.
    rc, out = await cp.execute(
        "journalctl --unit containerd --no-pager --lines 4000"
        " | grep --text nri-wait | tail -n 20",
        timeout=120,
    )
    print(f"[nixkube] what nri-wait said (rc={rc}):\n{out}", flush=True)

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
        await deploy(cp, settings)
        await wait_for_driver(cp)
        await check_workloads(cp)
        await probe(cp, settings, "a clean start")
        await check_resident(cp, settings, "a clean start")
        await check_unmount(cp, settings)
        await chaos(cp, settings)
    except Exception:
        # `until` already attaches `diagnose` -- kubelet, containerd, crictl
        # and the pod logs -- to the message it raises. What it cannot know
        # about is this namespace, so add that and nothing else.
        await report(cp)
        raise
    await report(cp)


run_test(test)
