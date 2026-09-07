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

  stdNix = pkgs.nix;
  nix = pkgs.nix.overrideAttrs (oldAttrs: {
    doCheck = false;
    doInstallCheck = false;
  });

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
