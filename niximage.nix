# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  nixkubeVersion ?
    (builtins.fromTOML (builtins.readFile ./pkgs/nixkube/pyproject.toml)).project.version,
}:
rec {
  server = "ghcr.io";
  repo = "${server}/lillecarl/nix-csi";

  images = lib.genAttrs [ "aarch64-linux" "x86_64-linux" ] (
    system:
    let
      sysPkgs = import pkgs.path {
        inherit system;
        overlays = [
          (import ./pkgs)
        ];
      };
      inherit (sysPkgs) lib;

      fakeNss = sysPkgs.dockerTools.fakeNss.override {
        extraGroupLines = [
          "nixbld:x:30000:"
        ];
      };
      runtimeInputs = [
        sysPkgs.coreutils
        sysPkgs.gitMinimal
        sysPkgs.nix
        sysPkgs.rsync
        sysPkgs.openssh
        sysPkgs.kubectl
      ];

      # TODO: consolidate init-copy and init-secrets into the same file (not same script)
      # ConfigMap or OCI? I think ConfigMap?
      init-copy = sysPkgs.writeShellApplication {
        name = "init-copy";
        inherit runtimeInputs;
        text = # bash
          ''
            set -euo pipefail
            set -x
            mkdir /tmp
            rsync --archive ${fakeNss}/ /

            # Resolve the correct store path for this architecture from the JSON NODE_ENV
            STORE_PATH=$(nix eval --store dummy:// --raw --impure --expr \
              '(builtins.fromJSON (builtins.getEnv "NODE_ENV")).${system}')

            # Check if we can SSH to pynixd
            EXTRA_SUBSTITUTERS="local?trusted=true"
            if nix store ping --store ssh-ng://nix@pynixd; then
              EXTRA_SUBSTITUTERS="$EXTRA_SUBSTITUTERS ssh-ng://nix@pynixd?trusted=true"
            fi

            nix \
              build \
                --extra-substituters "$EXTRA_SUBSTITUTERS" \
                --max-jobs auto \
                --option sandbox false \
                --store /nix-volume \
                --out-link /nix-volume/nix/var/result \
                --fallback \
                "$STORE_PATH"

            # Is every reference actually here?
            #
            # `nix build` can report success over a store that already holds
            # the top path and not all of its closure, and the node then starts
            # a container that cannot exec out of /nix/var/result/bin. The error
            # names $PATH, not the missing store path, so it reads as a broken
            # image rather than an incomplete store. That is issue #8: a
            # `containerWrapper` absent from the cache, found only after
            # comparing symlink targets by hand on a live node.
            #
            # `path-info --recursive` alone is not enough, and I measured that
            # rather than assuming it: it answers from the database, so a path
            # that is registered and absent from disk passes. That is exactly
            # the shape of #8. The loop stats each path, which is what catches
            # it, and nothing is hashed -- `nix store verify` would, and would
            # cost minutes on a 954 MiB closure at every node start.
            missing=0
            while read -r p; do
              if [ ! -e "/nix-volume$p" ]; then
                echo "incomplete store: $p is registered and absent" >&2
                missing=1
              fi
            done < <(nix path-info --store /nix-volume --recursive "$STORE_PATH")
            [ "$missing" -eq 0 ]
          '';
      };

      init-secrets = sysPkgs.writeShellApplication {
        name = "init-secrets";
        inherit runtimeInputs;
        text = # bash
          ''
            set -euo pipefail
            set -x
            mkdir /tmp
            rsync --archive ${fakeNss}/ /
            # shellcheck source=/dev/null
            source /opt/bin/init-secrets
          '';
      };

    in
    pkgs.dockerTools.streamLayeredImage {
      name = "${repo}/nix";
      tag = "${sysPkgs.nix.version}-${nixkubeVersion}-${sysPkgs.stdenv.hostPlatform.system}";
      architecture = sysPkgs.go.GOARCH;

      maxLayers = 125;
      includeNixDB = true;
      contents = [
        sysPkgs.dockerTools.binSh
        sysPkgs.dockerTools.caCertificates
        sysPkgs.dockerTools.usrBinEnv
      ];
      config = {
        Entrypoint = [ (lib.getExe init-copy) ];
        Env = [
          "PATH=${
            lib.makeBinPath [
              init-copy
              init-secrets
            ]
          }"
        ];
      };
    }
  );

  imageRef = system: "${images.${system}.imageName}:${images.${system}.imageTag}";

  # Per-arch push scripts (one per system, used by per-arch CI jobs)
  pushArch = lib.mapAttrs (
    system: image:
    pkgs.writeShellApplication {
      name = "push-nix-${system}";
      runtimeInputs = [
        pkgs.skopeo
        pkgs.gzip
        pkgs.cachix
      ];
      text = # bash
        ''
          skopeo login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          ${image} | gzip --fast | skopeo copy docker-archive:/dev/stdin docker://${imageRef system}
          cachix push nix-csi ${image}
        '';
    }
  ) images;

  # Manifest creation (used by dependent CI job after both arches are pushed)
  pushManifest = pkgs.writeShellApplication {
    name = "push-nix-manifest";
    runtimeInputs = [ pkgs.regctl ];
    text = # bash
      ''
        regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
        regctl index create ${repo}/nix:${pkgs.nix.version}-${nixkubeVersion} \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
        regctl index create ${repo}/nix:latest \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
      '';
  };
}
