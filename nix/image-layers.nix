# SPDX-License-Identifier: MIT
#
# The layer policy for the node image, ordered base first.
#
# **A list of names, not a size rule.** Nix cannot read a store path's size
# while it evaluates, so "every path over 10 MiB gets a layer" is not
# expressible. What is expressible is naming the packages that measurement
# says are big, and letting everything else fall into the remainder.
#
# Measured on the image before the environments went into it (118 paths,
# 0.47 GiB):
#
#   >= 10 MiB      7 paths    358 MiB
#   1 - 10 MiB    30 paths     80 MiB
#   100K - 1 MiB  56 paths     20 MiB
#   < 100 KiB     25 paths      3 MiB
#
# So 81 paths under a megabyte hold 23 MiB, 5% of the image, and
# `dockerTools.streamLayeredImage` spends 81 of its 125 layers on them. The
# layer budget goes to the paths that cost nothing to send again, and none is
# left for the ones that do. `nix2container`'s `buildLayer` takes the paths of
# a layer by name instead, which is why the image is built with it.
#
# Every layer dedupes against the ones before it, so a package named here
# carries only what no earlier layer already holds. A change to a layer
# invalidates every layer after it and none before, which is why the
# applications go at the end.
{
  pkgs,
  nodeEnv,
  cacheEnv,
}:
rec {
  /**
    The ordered groups. Each becomes one layer, and each lists every group
    before it as already held.

    Grouped by how often the thing changes, and only then by size. `nix` and
    its store move together on a Nix bump; `glibc` and `gcc-lib` move together
    on a stdenv bump; `icu4c`, `gettext` and `boost` move with neither and are
    only here because `git-minimal` and `kubectl` drag them in.
  */
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
    # **What the applications run as subprocesses, rather than import.** These
    # are on a wrapper's PATH, so without a layer of their own they land in an
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
    # **Everything the applications import, without the applications.** This
    # is what makes a release cheap. `nixkube`'s own output is 0.68 MiB,
    # `pynixd`'s is 0.27 MiB and `appstarter`'s is 1.3 KiB; the 192 MiB under
    # them is kr8s, pyzmq, cryptography, grpc and the rest, and none of it
    # moves when this repository releases. It moves when nixpkgs does, and
    # that is the bump where re-sending everything is expected anyway.
    {
      name = "python-deps";
      deps = dependenciesOf pkgs.nixkube ++ dependenciesOf pkgs.pynixd-nixkube;
    }
    {
      name = "pynixd";
      deps = [ pkgs.pynixd-nixkube ];
    }
    {
      name = "nixkube";
      deps = [ pkgs.nixkube ];
    }
    # **What the fallback environments hold, without the environments.**
    # `buildEnv` exposes its inputs as `passthru.paths`, so this is the
    # environments' own package lists rather than a copy of them. Measured:
    # 44 paths and 78 MiB that no layer above holds -- fish, curl, doggo,
    # iputils, dinit and the rest. They move when nixpkgs does.
    {
      name = "environment-tools";
      deps = nodeEnv.paths ++ cacheEnv.paths;
    }
    # The two symlink trees themselves, a few kilobytes each. Their own hashes
    # move on every release, and a layer is sent whole, so they cannot share
    # the 78 MiB above.
    {
      name = "environments";
      deps = [
        nodeEnv
        cacheEnv
      ];
    }
    {
      name = "app";
      deps = [ pkgs.appstarter ];
    }
  ];

  /**
    The direct runtime dependencies of a pyproject.nix application.

    **`passthru.dependencies`, not `propagatedBuildInputs`.** These
    applications are pyproject.nix venvs, and the builders propagate nothing,
    so `propagatedBuildInputs` is the empty list. Reading it here would make
    the layer above empty with no error, and put all 169 MiB back into the
    layer that moves on every release.

    The attribute names are distribution names, resolved in the set the
    applications are built from. `buildLayer` takes each path's closure, so
    the direct dependencies are enough.
  */
  dependenciesOf =
    app: map (name: pkgs.pythonSet.${name}) (builtins.attrNames (app.passthru.dependencies or { }));

  /**
    How many layers the remainder gets. Everything not named above lands
    here: the small paths that hold 23 MiB together and cost 81 layers under
    a popularity heuristic.
  */
  restLayers = 2;
}
