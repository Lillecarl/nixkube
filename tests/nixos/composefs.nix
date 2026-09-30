# SPDX-License-Identifier: MIT

# Can composefs present a CSI volume? A measurement, not a feature.
#
# A composefs mount is one overlay mount: an EROFS image holds the tree,
# and each regular file's content is a file under a base directory. With
# /nix/store as that base directory, a closure is one mount that reads
# straight from the store. That would answer what the farm (#68) and the
# hardlink tree cannot both answer at once:
#
#   - one mount per volume, not one per store path (fs.mount-max)
#   - real files, so kubelet's subPath, a non-recursive bind, carries them
#   - no link(2) out of the store, so no EXDEV on a host-store node (#25)
#   - the primary package's tree at the volume root without a copy
#
# The image is written from a composefs-dump(5) text file this test
# generates from the closure, so nothing is copied or walked twice.
{
  pkgs,
  lib ? pkgs.lib,
}:

let
  primary = pkgs.hello;

  # Writes the dump for a closure, laid out as a CSI volume root: /nix/store
  # with every path, /nix/var/result, and the primary package's tree at /.
  dump = pkgs.writeText "composefs_dump.py" ''
    import json
    import os
    import stat
    import subprocess
    import sys
    import time
    from pathlib import Path

    STORE = Path("/nix/store")
    primary = Path(sys.argv[1])
    roots = sys.argv[2:]
    out = sys.stdout


    def esc(s: str) -> str:
        b = s.encode()
        return "".join(
            chr(c) if 0x21 <= c <= 0x7E and c not in (0x5C, 0x3D) else f"\\x{c:02x}"
            for c in b
        )


    def line(path, size, mode, nlink, payload="-"):
        # PATH SIZE MODE NLINK UID GID RDEV MTIME PAYLOAD CONTENT DIGEST
        out.write(f"{esc(path)} {size} {mode:o} {nlink} 0 0 0 1.0 {payload} - -\n")


    def directory(path):
        line(path, 0, stat.S_IFDIR | 0o555, 2)


    def entry(image_path: str, real: Path, st: os.stat_result) -> int:
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(real)
            line(image_path, len(target.encode()), stat.S_IFLNK | 0o777, 1, esc(target))
        elif stat.S_ISDIR(st.st_mode):
            line(image_path, 0, st.st_mode, 2)
        else:
            rel = str(real.relative_to(STORE))
            line(image_path, st.st_size, st.st_mode, 1, esc(rel))
        return 1


    def tree(image_path: str, real: Path) -> int:
        n = entry(image_path, real, real.lstat())
        if real.is_dir() and not real.is_symlink():
            for child in sorted(os.listdir(real)):
                n += tree(f"{image_path}/{child}", real / child)
        return n


    def deref(image_path: str, real: Path) -> int:
        """The primary package's tree, symlinks followed, as a subPath
        needs it. A file's payload is the store file the link resolves to."""
        resolved = real.resolve()
        if resolved.is_dir():
            if image_path:
                directory(image_path)
            n = 1
            for child in sorted(os.listdir(resolved)):
                n += deref(f"{image_path}/{child}", resolved / child)
            return n
        return entry(image_path, resolved, resolved.stat())


    started = time.monotonic()
    closure = sorted(
        subprocess.run(
            ["nix-store", "--query", "--requisites", *roots],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    directory("/")
    entries = deref("", primary)
    directory("/nix")
    directory("/nix/store")
    directory("/nix/var")
    target = str(primary)
    line("/nix/var/result", len(target), stat.S_IFLNK | 0o777, 1, esc(target))
    for p in closure:
        entries += tree(p, Path(p))
    print(
        "DUMPED " + json.dumps({
            "paths": len(closure),
            "entries": entries,
            "seconds": round(time.monotonic() - started, 2),
        }),
        file=sys.stderr,
    )
  '';
in
{
  name = "nixkube-composefs";

  nodes.machine =
    { ... }:
    {
      virtualisation.memorySize = 2048;
      virtualisation.additionalPaths = [ primary ];
      environment.systemPackages = [
        pkgs.composefs
        pkgs.python3
        pkgs.util-linux
      ];
    };

  testScript = ''
    import json
    import re

    machine.wait_for_unit("multi-user.target")
    print("[kernel] " + machine.succeed("uname -r").strip())

    def mounts():
        return int(machine.succeed("wc -l < /proc/self/mountinfo").strip())

    def build(name, roots):
        """Dump, image, and the measurements of both."""
        err = machine.succeed(
            f"python3 ${dump} ${primary} {roots} > /var/{name}.dump 2> /var/{name}.err"
            f"; cat /var/{name}.err"
        )
        dumped = json.loads(err.split("DUMPED ", 1)[1].splitlines()[0])
        t = machine.succeed(
            f"s=$(date +%s.%N); mkcomposefs --from-file /var/{name}.dump /var/{name}.cfs"
            f"; e=$(date +%s.%N); echo \"$e - $s\" | ${pkgs.bc}/bin/bc"
        ).strip()
        size = int(machine.succeed(f"stat -c %s /var/{name}.cfs").strip())
        dumped |= {"mkcomposefs_seconds": float(t), "image_bytes": size}
        print(f"[{name}] {dumped}")
        return dumped

    small = build("hello", "${primary}")

    before = mounts()
    loops_before = machine.succeed("losetup -a | wc -l").strip()
    machine.succeed("mkdir -p /var/ro")
    machine.succeed("mount -t composefs -o basedir=/nix/store /var/hello.cfs /var/ro")
    added = mounts() - before
    loops = int(machine.succeed("losetup -a | wc -l").strip()) - int(loops_before)
    print(f"[mount] one volume added {added} mounts and {loops} loop devices")
    print(machine.succeed("grep ' /var/ro' /proc/self/mountinfo"))

    # 1. Content, straight from the store.
    machine.succeed("/var/ro/nix/store/$(basename ${primary})/bin/hello | grep -q 'Hello, world'")
    machine.succeed("cmp /var/ro/nix/store/$(basename ${primary})/bin/hello ${primary}/bin/hello")
    print("[content] a store file reads the same through the image")

    # 6. The primary package at the root: a real file, no copy on disk.
    machine.succeed("test -f /var/ro/bin/hello && test ! -L /var/ro/bin/hello")
    machine.succeed("/var/ro/bin/hello | grep -q 'Hello, world'")
    machine.succeed("test \"$(readlink /var/ro/nix/var/result)\" = ${primary}")
    print("[deref] /bin/hello is a regular file backed by the store; result link set")

    # 3. What kubelet's subPath does: a bind that is not recursive.
    machine.succeed("mkdir -p /var/subpath && mount --bind /var/ro/nix /var/subpath")
    machine.succeed("/var/subpath/store/$(basename ${primary})/bin/hello | grep -q 'Hello, world'")
    print("[subPath] a non-recursive bind of <volume>/nix carries the store")
    machine.succeed("umount /var/subpath")

    # 4. Read-only.
    machine.fail("touch /var/ro/nix/store/$(basename ${primary})/WRITTEN")
    machine.fail("touch /var/ro/WRITTEN")
    print("[ro] writes refused")

    # 7. Teardown: one umount, back to where it was.
    machine.succeed("umount /var/ro")
    assert mounts() == before, f"{mounts()} mounts after, {before} before"
    print("[teardown] one umount; the namespace is back to its mount count")

    # 4. Read-write, with an upper layer on the volume's own disk.
    machine.succeed("mkdir -p /var/rw /var/rw-upper /var/rw-work")
    machine.succeed(
        "mount -t composefs -o basedir=/nix/store,rw,upperdir=/var/rw-upper,workdir=/var/rw-work"
        " /var/hello.cfs /var/rw"
    )
    machine.succeed("mkdir /var/rw/nix/store/0000000000000000000000000000000-built")
    machine.succeed("echo built > /var/rw/nix/store/0000000000000000000000000000000-built/out")
    machine.succeed("test -f /var/rw-upper/nix/store/0000000000000000000000000000000-built/out")
    machine.fail("test -e /nix/store/0000000000000000000000000000000-built")
    print("[rw] a write lands in the upper directory, not in /nix/store")
    machine.succeed("umount /var/rw")

    # 5. Scale: the whole system closure, as a node's closure is thousands.
    big = build("system", "/run/current-system")
    machine.succeed("mkdir -p /var/big")
    t = machine.succeed(
        "s=$(date +%s.%N); mount -t composefs -o basedir=/nix/store /var/system.cfs /var/big"
        "; e=$(date +%s.%N); echo \"$e - $s\" | ${pkgs.bc}/bin/bc"
    ).strip()
    n = machine.succeed("ls /var/big/nix/store | wc -l").strip()
    print(f"[scale] {big}; mount took {t}s; {n} store paths visible")
    assert int(n) == big["paths"], f"{n} visible of {big['paths']}"
    machine.succeed("umount /var/big")
  '';
}
