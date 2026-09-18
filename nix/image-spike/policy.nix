# SPDX-License-Identifier: MIT
#
# One layering policy, read by both spikes.
#
# **The policy is a list of names, not a size rule.** Nix cannot read a store
# path's size while it evaluates, so "every path over 10 MiB gets a layer"
# is not expressible here. What is expressible is naming the packages that
# measurement says are big, and letting everything else fall into the
# remainder.
#
# Measured on the current image (`nixImage.images.x86_64-linux`, 118 paths,
# 0.47 GiB):
#
#   >= 10 MiB      7 paths    358 MiB
#   1 - 10 MiB    30 paths     80 MiB
#   100K - 1 MiB  56 paths     20 MiB
#   < 100 KiB     25 paths      3 MiB
#
# So 81 paths under a megabyte hold 23 MiB, 5% of the image, and
# `streamLayeredImage` spends 81 of its 125 layers on them. That is the whole
# problem: the layer budget goes to the paths that cost nothing to send again,
# and there is none left for the ones that do.
#
# The closure work lives in the packages, not here, so this policy overrides
# nothing. Measured on `pkgs.nixkube` as each step landed:
#
#   218 paths  844 MiB   stock
#   184 paths  634 MiB   `lib.getBin` on nix, openssh, util-linuxMinimal
#   176 paths  589 MiB   the codegen tools out of csi-proto-python
#   167 paths  534 MiB   googleapis-common-protos out, taking the protoc
#                        plugins of the C++ grpc with it
#
# The order is base first, application last. Every layer dedupes against the
# ones before it, so a package named here carries only what no earlier layer
# already holds. A change to a layer invalidates every layer after it and
# none before, which is why the application goes at the end.
{
  pkgs,
  # What stands in for "the application changed". A rebuild with a different
  # value here is the stability measurement: how many layers move when only
  # the top one should.
  # `null` means "whatever the policy picks", which is `pkgs.nixkube`. A
  # default argument cannot name a binding of the body.
  app ? null,
  # The second application. It shares python3 and glibc with the first, so it
  # costs only what is unique to it.
  pynixd ? pkgs.pynixd-nixkube,
}:
rec {
  fakeNss = pkgs.dockerTools.fakeNss.override {
    extraGroupLines = [ "nixbld:x:30000:" ];
  };

  runtimeInputs = [
    pkgs.coreutils
    pkgs.gitMinimal
    pkgs.nix
    pkgs.rsync
    pkgs.openssh
    pkgs.kubectl
  ];

  contents = [
    pkgs.dockerTools.binSh
    pkgs.dockerTools.caCertificates
    pkgs.dockerTools.usrBinEnv
    fakeNss
  ];

  # Ordered, base first. Each entry becomes one layer.
  #
  # Grouped by how often the thing changes, and only then by size. `nix` and
  # its store move together on a Nix bump; `glibc` and `gcc-lib` move together
  # on a stdenv bump; `icu4c`, `gettext` and `boost` move with neither and are
  # only here because `git-minimal` and `kubectl` drag them in.
  groups = [
    {
      name = "libc";
      deps = [
        pkgs.glibc
        pkgs.gcc-unwrapped.lib
      ];
    }
    {
      name = "heavy-libs";
      deps = [
        pkgs.icu
        pkgs.gettext
        pkgs.boost
        pkgs.openssl
        pkgs.sqlite
      ];
    }
    {
      name = "python";
      deps = [ pkgs.python3 ];
    }
    {
      name = "kubectl";
      deps = [ pkgs.kubectl ];
    }
    {
      name = "git";
      deps = [ pkgs.gitMinimal ];
    }
    {
      name = "nix";
      deps = [ pkgs.nix ];
    }
    {
      name = "shell";
      deps = [
        pkgs.bashInteractive
        pkgs.coreutils
        pkgs.openssh
        pkgs.rsync
      ];
    }
    # **What `nixkube` runs as subprocesses, rather than imports.** These are
    # on the wrapper's PATH, so without a layer of their own they land in the
    # application's layer and 27 MiB goes out on every release. They belong
    # with the base: `nri-wait` moves when this repository does, and the other
    # three only when nixpkgs does.
    {
      name = "tools";
      deps = [
        pkgs.pkgsStatic.coreutils
        pkgs.util-linuxMinimal
        pkgs.nix_init_db
        pkgs.nri-wait
      ];
    }
    # **Everything the two applications import, without the applications.**
    # This is what makes a release cheap. `nixkube`'s own output is 0.68 MiB
    # and `pynixd`'s is 0.27 MiB; the 192 MiB under them is kr8s, pyzmq,
    # cryptography, grpc and the rest, and none of it moves when this
    # repository releases. It moves when nixpkgs does, and that is the bump
    # where re-sending everything is expected anyway.
    {
      name = "python-deps";
      deps = dependenciesOf effectiveApp ++ dependenciesOf pynixd;
    }
    {
      name = "pynixd";
      deps = [ pynixd ];
    }
    {
      name = "app";
      deps = [ effectiveApp ];
    }
  ];

  effectiveApp = if app != null then app else pkgs.nixkube;

  # **`passthru.dependencies`, not `propagatedBuildInputs`.** These
  # applications are pyproject.nix venvs, and the builders propagate nothing,
  # so `propagatedBuildInputs` is the empty list. Reading it here would make
  # the layer above empty with no error, and put all 169 MiB back into the
  # layer that moves on every release.
  #
  # The attribute names are distribution names, resolved in the set the
  # applications are built from. `buildLayer` takes each path's closure, so
  # the direct dependencies are enough.
  dependenciesOf =
    app: map (name: pkgs.pythonSet.${name}) (builtins.attrNames (app.passthru.dependencies or { }));

  # How many layers the remainder gets. Everything not named above lands
  # here: the small paths that hold 23 MiB together and cost 81 layers today.
  restLayers = 2;
}
