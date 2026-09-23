# SPDX-License-Identifier: MIT

# Does a CSI publish carry a volume root's own mounts into the pod, and are
# they read-only when it gets there?
#
# Issue #66. `mount_volume` published with `MS_BIND` and made the result
# read-only with `MS_BIND | MS_REMOUNT | MS_RDONLY`. Both are correct for a
# volume holding a hardlink tree and wrong for one holding a bind farm (#65):
# the first hides every path, the second leaves them writable, and a write
# through a bound path reaches the host store by way of the shared inode.
#
# This needs real root. mount(2) wants it, and the read-only check has to be
# a real write that the kernel refuses.
{
  pkgs,
  lib ? pkgs.lib,
}:

let
  venv = pkgs.pythonSet.mkVirtualEnv "csi-mount-rec-env" { nixkube = [ ]; };

  publish = pkgs.writeText "publish.py" ''
    import sys
    from pathlib import Path

    import anyio

    from src.volume import mount_volume

    anyio.run(mount_volume, Path(sys.argv[1]), Path(sys.argv[2]), True)
  '';
in
pkgs.testers.runNixOSTest {
  name = "nixkube-csi-mount-rec";

  nodes.machine = {
    virtualisation.memorySize = 1024;
    environment.systemPackages = [ venv ];
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")

    # A volume root shaped like a bind farm: three store paths, each its own
    # mount, the way #65 would build it.
    machine.succeed(
        """
        mkdir -p /var/vol/volume_root/nix/store
        for i in 0 1 2; do
            mkdir -p /var/vol/real$i
            echo "path $i" > /var/vol/real$i/marker
            mkdir /var/vol/volume_root/nix/store/p$i
            mount --bind /var/vol/real$i /var/vol/volume_root/nix/store/p$i
        done
        """
    )

    def readable(target):
        """How many of the three paths carry their content at `target`.

        `find`, not `cat` or `ls` over a glob: the driver runs each command
        under `pipefail`, and those two exit non-zero when the count is the
        zero this test needs to be able to measure.
        """
        out = machine.succeed(f"find {target}/nix/store -name marker | wc -l")
        return int(out.strip())

    # The negative control. A plain MS_BIND is what `mount_volume` used to
    # do, and it is the reason the fix is needed: the mountpoints arrive as
    # empty directories. If this ever reads 3, the kernel changed and the
    # test below stopped proving anything.
    machine.succeed("mkdir -p /var/control")
    machine.succeed("mount --bind /var/vol/volume_root /var/control")
    control = readable("/var/control")
    assert control == 0, f"a non-recursive bind carried {control}/3 paths"
    print("[control] a non-recursive bind carries 0/3 paths")

    # The second control, for the other half of the fix. A recursive bind
    # made read-only the classic way leaves its submounts writable, which is
    # why the publish needs mount_setattr(AT_RECURSIVE) instead.
    machine.succeed("mkdir -p /var/control-ro")
    machine.succeed("mount --rbind /var/vol/volume_root /var/control-ro")
    machine.succeed("mount -o remount,bind,ro /var/control-ro")
    machine.fail("touch /var/control-ro/nix/store/WRITTEN")
    machine.succeed("touch /var/control-ro/nix/store/p0/WRITTEN")
    print("[control] MS_REMOUNT|MS_RDONLY leaves a submount writable")

    machine.succeed(
        "${venv}/bin/python ${publish} /var/vol/volume_root /var/target"
    )

    got = readable("/var/target")
    assert got == 3, f"the publish carried {got}/3 paths into the pod"
    machine.succeed("grep -q 'path 0' /var/target/nix/store/p0/marker")
    print("[publish] mount_volume carries 3/3 paths")

    # Read-only has to reach the submounts, not just the top mount.
    machine.fail("touch /var/target/nix/store/WRITTEN")
    machine.fail("touch /var/target/nix/store/p0/WRITTEN")
    print("[publish] the top mount and the submounts both refuse writes")

    # And the refusal must not be the source being read-only: the host side
    # is writable, so only the mount flags can be saying no.
    machine.succeed("touch /var/vol/real0/HOST-CAN-WRITE")
  '';
}
