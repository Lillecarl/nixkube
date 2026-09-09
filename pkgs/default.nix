# SPDX-License-Identifier: MIT

self: pkgs: {
  # Overlay lib
  lib = pkgs.lib.extend (import ../lib);

  # First argument is NIX_STATE_DIR which is where we init the dumped database
  nix_init_db =
    pkgs.writeScriptBin "nix_init_db" # bash
      ''
        #! ${pkgs.runtimeShell}
        NSD="$1"
        shift
        export USER nobody
        nix-store --option store local --dump-db "$@" | NIX_STATE_DIR="$NSD" nix-store --load-db --option store local
      '';

  nixkube = pkgs.python3Packages.callPackage ./nixkube {
    inherit (self)
      csi-proto-python
      cri-proto-python
      nri-proto-python
      grpclib-nri
      kr8s
      nri-wait
      ;
    coreutils = pkgs.pkgsStatic.coreutils;
  };

  # kluctl = pkgs.kluctl.override {
  #   python310 = pkgs.python3;
  # };

  # No override on `nix`.
  #
  # This used to be `pkgs.nix.overrideAttrs { doCheck = false;
  # doInstallCheck = false; }`, to skip Nix's own test suite. It changed the
  # derivation hash, so cache.nixos.org could never answer for it:
  #
  #   overridden  bq05h82hrbjar92g6fm64g56vj3msgy6-nix-2.34.8   404
  #   stock       j02vvifyzjw52xqrvyj1bhd8s45yn1fl-nix-2.34.8   200
  #
  # Every CI runner then built Nix from source, on both architectures, and
  # `build-amd64` and `build-arm64` both hit GitHub's six-hour job limit. That
  # skipped `build-manifests`, which is what publishes the image manifest and
  # pushes to cachix -- so nothing has published since, and a node cannot find
  # `nodeEnv` to boot with. See issue #9.
  #
  # Skipping the test suite saves minutes on a build nobody should be doing.
  # Paying for it with a source build of Nix, twice per run, is the wrong
  # trade. Anything that wants to skip those checks locally has to do it
  # without changing the hash of what CI publishes.

  grpclib-ttrpc = pkgs.python3Packages.callPackage ./grpclib-ttrpc {
    inherit (self) ttrpc-proto-python;
  };
  grpclib-nri = pkgs.python3Packages.callPackage ./grpclib-nri {
    inherit (self) grpclib-ttrpc nri-proto-python;
  };
  csi-proto-python = pkgs.python3Packages.callPackage ./csi-proto-python { };
  cri-proto-python = pkgs.python3Packages.callPackage ./cri-proto-python { };
  nri-proto-python = pkgs.python3Packages.callPackage ./nri-proto-python { };
  ttrpc-proto-python = pkgs.python3Packages.callPackage ./ttrpc-proto-python { };
  python-jsonpath = pkgs.python3Packages.callPackage ./python-jsonpath.nix { };
  kr8s = pkgs.python3Packages.callPackage ./kr8s.nix { inherit (self) python-jsonpath; };
  shellous = pkgs.python3Packages.callPackage ./shellous.nix { };

  # NRI wait Python application for OCI hooks
  # Runs inside chroot(/var/lib/nix-csi), uses pyzmq for communication
  nri-wait = pkgs.python3Packages.callPackage ./nri-wait { };

  ci-debug = pkgs.callPackage ./ci-debug { inherit pkgs; };

  # The two kubernetes-csi sidecars the node DaemonSet runs, and images for
  # them. Built here so a node with no route to registry.k8s.io can still run
  # the driver -- which is every node inside a Nix build sandbox. See
  # ./csi-sidecars.
  csi-sidecars = pkgs.callPackage ./csi-sidecars { };

  pynixd =
    let
      path =
        if builtins.pathExists ../../pynixd then
          ../../pynixd
        else
          fetchTree {
            type = "github";
            owner = "lillecarl";
            repo = "pynixd";
            ref = "develop";
          }; # this must be updated to a "flake" locked input
    in
    (import path {
      inherit pkgs;
    }).library;
  pynixd-nixkube = pkgs.python3Packages.callPackage ./pynixd-nixkube {
    inherit (self) pynixd kr8s;
    inherit (pkgs) dockerTools;
  };
}
