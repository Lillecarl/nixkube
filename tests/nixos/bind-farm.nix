# SPDX-License-Identifier: MIT

# Does `nix build` work inside a store built from bind mounts?
#
# The question issue #65 rests on. A bind farm presents a closure as one
# read-only bind mount per store path, inside a writable directory. That is
# cheap -- 14us per path against 22us per *file* for a hardlink tree -- and
# it leaves nothing on disk to garbage collect. But it only replaces the
# hardlink farm if nix itself is happy building into such a store: `nix
# build` ends by renaming a temporary directory into /nix/store, beside
# entries that are mountpoints rather than ordinary directories.
#
# This needs real root. A user namespace is not enough: bind mounts want one,
# and inside one nix's store initialisation cannot chown the store directory,
# while a full subuid map costs nix access to the host paths it reads.
{
  pkgs,
  lib ? pkgs.lib,
}:

let
  seed = pkgs.bash;
in
pkgs.testers.runNixOSTest {
  name = "nixkube-bind-farm";

  nodes.machine = {
    virtualisation.memorySize = 2048;
    virtualisation.diskSize = 4096;
    # The seed closure has to be in the guest's own store: the farm binds
    # from it, and the guest has no network.
    virtualisation.additionalPaths = [ seed ];
    nix.settings.experimental-features = [
      "nix-command"
      "flakes"
    ];
  };

  testScript = ''
    seed = "${seed}"
    root = "/var/farm"

    machine.wait_for_unit("multi-user.target")

    machine.succeed(f"mkdir -p {root}/nix/store {root}/nix/var/nix")
    machine.succeed(f"findmnt -no FSTYPE -T {root}")

    paths = machine.succeed(
        f"nix-store --query --requisites {seed}"
    ).split()
    assert len(paths) > 1, f"seed closure is too small to be a test: {paths}"

    # One read-only bind per store path, into a writable directory.
    machine.succeed(
        f"""
        for p in $(nix-store --query --requisites {seed}); do
            dst="{root}/nix/store/$(basename $p)"
            if [ -L "$p" ]; then cp -P "$p" "$dst"
            elif [ -d "$p" ]; then mkdir "$dst" && mount --bind -o ro "$p" "$dst"
            else : > "$dst" && mount --bind -o ro "$p" "$dst"
            fi
        done
        """
    )
    # Not `findmnt -R {root}`: that wants {root} itself to be a mountpoint,
    # and the farm's root is a plain directory holding mounts.
    bound = int(machine.succeed(f"awk '$5 ~ \"^{root}/\" {{n++}} END {{print n+0}}' /proc/self/mountinfo"))
    print(f"[farm] {len(paths)} closure paths, {bound} mount entries under the root")

    # Register the metadata only. Nothing is copied -- that is the point.
    machine.succeed(
        f"nix-store --dump-db $(nix-store --query --requisites {seed}) > /tmp/db.dump"
    )
    machine.succeed(f"NIX_STATE_DIR={root}/nix/var/nix nix-store --store {root} --load-db < /tmp/db.dump")

    known = machine.succeed(
        f"nix-store --store {root} --query --requisites {seed} | wc -l"
    ).strip()
    print(f"[farm] the farm's db knows {known} paths")

    # The question: does a build land in the gaps between the mounts?
    #
    # `builtins.storePath`, not a bare string. A diverted store forces the
    # sandbox on -- `store.storeDir != realStoreDir` sets `useSandbox = true`
    # and `--option sandbox false` cannot turn it off -- and the sandbox binds
    # only declared inputs. A bare string declares nothing, so the builder is
    # absent in the chroot and exec fails with ENOENT. Declaring it is also
    # the stronger question: nix must bind a farm entry into its own sandbox.
    out = machine.succeed(
        f"""
        NIX_STATE_DIR={root}/nix/var/nix nix build --store {root} --impure \
          --no-link --print-out-paths \
          --option substituters "" \
          --expr 'derivation {{
            name = "built-in-farm";
            system = builtins.currentSystem;
            builder = "''${{builtins.storePath "{seed}"}}/bin/bash";
            args = [ "-c" "echo made-inside-the-farm > $out" ];
          }}'
        """
    ).strip()
    print(f"[farm] nix build produced {out}")

    content = machine.succeed(f"cat {root}{out}").strip()
    assert content == "made-inside-the-farm", f"wrong content: {content!r}"

    # It must be a real file on the writable side, not a mount.
    machine.fail(f"mountpoint -q {root}{out}")
    print("[farm] the output is an ordinary file beside the mounts")

    # And the bound paths must still refuse writes.
    a_path = [p for p in paths if p != seed][0]
    machine.fail(f"touch {root}/nix/store/$(basename {a_path})/SHOULD-NOT-WRITE")
    print("[farm] bound store paths are still read-only")
  '';
}
