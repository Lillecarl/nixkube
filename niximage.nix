# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  # The cache environment is a dinit tree, so it needs the same `dinix` the
  # deployment builds it with. `kubenix/options.nix` holds the option; this
  # takes the source directly, because nothing here evaluates that module.
  dinix ? (import ./nix/sources.nix).dinix,
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

      # The same `/etc` `appstarter init` installs for itself. One definition,
      # so the two containers of a pod resolve a user the same way.
      fakeNss = sysPkgs.appstarter-fake-nss;

      # The fallback the image carries, one per role. `appstarter init` takes
      # it when it cannot fetch what the deployment asks for, and it is useful
      # precisely because it lags: it is whatever was current at build time.
      #
      # Naming these in the pod spec instead would not work. A pod spec comes
      # out of the same evaluation as `APPSTARTER_WANTED`, so the fallback
      # would be the identical path.
      nodeEnv = sysPkgs.callPackage ./environments/node { };
      cacheEnv = sysPkgs.callPackage ./environments/cache { inherit dinix; };

      runtimeInputs = [
        sysPkgs.coreutils
        sysPkgs.gitMinimal
        sysPkgs.nix
        sysPkgs.rsync
        sysPkgs.openssh
        sysPkgs.kubectl
      ];

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
        Entrypoint = [
          (lib.getExe sysPkgs.appstarter)
          "init"
        ];
        Env = [
          "PATH=${
            lib.makeBinPath [
              sysPkgs.appstarter
              init-secrets
            ]
          }"
          "APPSTARTER_FALLBACK_NODE=${nodeEnv}"
          "APPSTARTER_FALLBACK_CACHE=${cacheEnv}"
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
          cachix push nixkube ${image}
        '';
    }
  ) images;

  # Manifest creation (used by dependent CI job after both arches are pushed)
  pushManifest = pkgs.writeShellApplication {
    name = "push-nix-manifest";
    runtimeInputs = [ pkgs.regctl ];
    text = # bash
      ''
        set -euo pipefail

        regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}

        # The two tags come from two jobs on two runners. Say which one is
        # missing, rather than letting `index create` fail against a name
        # that reads like a typo.
        for ref in ${imageRef "aarch64-linux"} ${imageRef "x86_64-linux"}; do
          if ! regctl manifest head "$ref" >/dev/null 2>&1; then
            echo "missing: $ref" >&2
            echo "Its build job did not publish. An index over it would be wrong." >&2
            exit 1
          fi
          echo "present: $ref"
        done

        regctl index create ${repo}/nix:${pkgs.nix.version}-${nixkubeVersion} \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
        regctl index create ${repo}/nix:latest \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
      '';
  };
}
