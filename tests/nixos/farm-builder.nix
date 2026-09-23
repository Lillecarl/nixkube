# SPDX-License-Identifier: MIT

# nixkube's own `build_farm`, run as root against a real closure.
#
# The unit tests cannot ask any of this: mount(2) needs real privileges, and
# every property that matters here is a property of the kernel's mount table
# rather than of the Python. Issue #65.
#
# What it has to get right, and what goes wrong if it does not:
#
#   - every closure path readable through the farm  (an empty store)
#   - each bind read-only on its own                (writes reach /nix/store
#                                                    on the node, through the
#                                                    shared inode)
#   - the gaps between the mounts still writable    (no `nix build` in a
#                                                    read-write pod)
#   - the mounts confined to the worker's namespace (fs.mount-max is 100,000
#                                                    per namespace, so the
#                                                    daemon would hold about
#                                                    40 containers)
{
  pkgs,
  lib ? pkgs.lib,
}:

let
  seed = pkgs.bash;
  venv = pkgs.pythonSet.mkVirtualEnv "farm-builder-env" { nixkube = [ ]; };

  # Runs in a namespace of its own, the way the mount worker does.
  builder = pkgs.writeText "build_farm_probe.py" ''
    import json
    import subprocess
    import sys
    from pathlib import Path

    from src.nri.farm import build_farm, detach_namespace

    root = Path(sys.argv[1])
    seed = sys.argv[2]

    closure = [
        Path(line)
        for line in subprocess.run(
            ["nix-store", "--query", "--requisites", seed],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    ]

    detach_namespace()
    bound = build_farm(root / "nix" / "store", closure)

    mounts = sum(
        1
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if line.split()[4].startswith(str(root) + "/")
    )

    # Checked in here, inside the namespace that holds the farm. The shell
    # outside cannot see these mounts at all, which is the point.
    checks = {
        "closure": len(closure),
        "bound": bound,
        "mounts": mounts,
        "readable": sum(1 for p in closure if (root / "nix/store" / p.name).exists()),
    }

    probe = root / "nix" / "store" / Path(seed).name / "REACHED-THE-NODE-STORE"
    try:
        probe.touch()
        checks["bind_is_writable"] = True
    except OSError:
        checks["bind_is_writable"] = False

    gap = root / "nix" / "store" / "0000000000000000000000000000000-gap"
    try:
        gap.mkdir()
        (gap / "built").write_text("a build landed here")
        checks["gap_is_writable"] = True
    except OSError:
        checks["gap_is_writable"] = False

    print("PROBE " + json.dumps(checks))
  '';
in
pkgs.testers.runNixOSTest {
  name = "nixkube-farm-builder";

  nodes.machine = {
    virtualisation.memorySize = 2048;
    virtualisation.diskSize = 4096;
    virtualisation.additionalPaths = [ seed ];
    environment.systemPackages = [ venv ];
  };

  testScript = ''
    import json

    seed = "${seed}"
    root = "/var/farm"

    machine.wait_for_unit("multi-user.target")
    machine.succeed(f"mkdir -p {root}/nix/store")

    out = machine.succeed(f"${venv}/bin/python ${builder} {root} {seed}")
    line = [ln for ln in out.splitlines() if ln.startswith("PROBE ")][0]
    checks = json.loads(line.removeprefix("PROBE "))
    print(f"[farm] {checks}")

    assert checks["closure"] > 1, f"seed closure too small to test: {checks}"
    assert checks["bound"] == checks["closure"], (
        f"bound {checks['bound']} of {checks['closure']} paths"
    )
    assert checks["readable"] == checks["closure"], (
        f"only {checks['readable']} of {checks['closure']} paths are readable"
    )
    assert checks["mounts"] == checks["bound"], (
        f"{checks['mounts']} mounts for {checks['bound']} binds"
    )

    # The one that protects the node's store.
    assert checks["bind_is_writable"] is False, (
        "a bound store path accepted a write, which reaches /nix/store on the"
        " node through the shared inode"
    )
    # And the one that makes a read-write pod possible at all.
    assert checks["gap_is_writable"] is True, (
        "nothing can be created between the mounts, so `nix build` cannot"
        " land its output"
    )
    print("[farm] read-only binds, writable gaps")

    # The farm must not have escaped into this namespace: `/` is shared under
    # systemd, so a worker that forgets MS_PRIVATE propagates every mount back
    # out and spends the node's mount budget.
    leaked = int(
        machine.succeed(
            f"awk '$5 ~ \"^{root}/\" {{n++}} END {{print n+0}}' /proc/self/mountinfo"
        )
    )
    assert leaked == 0, f"{leaked} of the farm's mounts leaked out of the worker"
    print("[farm] the mounts stayed in the worker's namespace")

    # The worker is gone, and with it the namespace that held every bind. What
    # is left on disk is the whole point of a farm: empty mountpoints, and
    # whatever the container itself wrote between them.
    GAP = "0000000000000000000000000000000-gap"
    on_disk = machine.succeed(f"ls -1 {root}/nix/store").split()
    assert len(on_disk) == checks["closure"] + 1, (
        f"{len(on_disk)} entries for {checks['closure']} paths plus the gap:"
        f" {sorted(on_disk)}"
    )

    seed_name = seed.removeprefix("/nix/store/")
    left = machine.succeed(f"ls -A {root}/nix/store/{seed_name} | wc -l").strip()
    assert left == "0", (
        f"a bound store path left {left} entries on disk, so the closure was"
        " copied after all"
    )
    print("[farm] the binds left nothing behind: empty mountpoints only")

    # And the writable side is the side that persists, which is what makes a
    # read-write pod's `nix build` output outlive the mount that produced it.
    machine.succeed(f"grep -q 'a build landed here' {root}/nix/store/{GAP}/built")
    print("[farm] what the container wrote between the mounts survived")
  '';
}
