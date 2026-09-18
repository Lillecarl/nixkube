# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  sources ? import ./nix/sources.nix,
  # The cache environment is a dinit tree, so it needs the same `dinix` the
  # deployment builds it with. `kubenix/options.nix` holds the option; this
  # takes the source directly, because nothing here evaluates that module.
  dinix ? sources.dinix,
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

      n2c = (import sources.nix2container { pkgs = sysPkgs; }).nix2container;

      policy = import ./nix/image-layers.nix {
        pkgs = sysPkgs;
        inherit nodeEnv cacheEnv;
      };

      /*
        Base first, each link holding every link before it. `layers` is the
        dedupe: a path an earlier layer already carries is not repeated, and
        the Go side needs every parent's `layers.json` to know that, so the
        whole prefix has to go in.

        `nestedLayers = built` is not cosmetic. nix2container computes it as
        `flatten (map (l: l.nestedLayers) layers) ++ layers`, which counts
        every layer once per path through the chain: 2^(k-1) - 1 entries for
        link k. `buildImage` then puts the lot in one shell argument, and 13
        links come to 8191 entries and about 450 KB -- past Linux's 128 KB
        limit for a single argument:

          jq: Argument list too long

        `built` is that same set of layers with the duplicates gone, so the
        chain stays linear. Measured: 91 entries for the 13 links here.
      */
      chain = builtins.foldl' (
        built: group:
        built
        ++ [
          (
            n2c.buildLayer {
              inherit (group) deps;
              layers = built;
            }
            // {
              nestedLayers = built;
            }
          )
        ]
      ) [ ] policy.groups;
    in
    n2c.buildImage {
      name = "${repo}/nix";
      tag = "${nixkubeVersion}-${sysPkgs.nix.version}-${sysPkgs.stdenv.hostPlatform.system}";
      arch = sysPkgs.go.GOARCH;

      layers = chain;
      # The remainder only. `maxLayers` does not apply to the named layers.
      maxLayers = policy.restLayers;

      # `nix` runs against the image's own store in the initContainer, and a
      # store with no database is one Nix declines to read.
      initializeNixDatabase = true;

      copyToRoot = [
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

  # skopeo, with nix2container's `nix:` transport. It reads the image's JSON
  # manifest and sends the store paths it names straight out of the store,
  # instead of piping a tarball through gzip.
  skopeo = (import sources.nix2container { inherit pkgs; }).skopeo-nix2container;

  # Per-arch push scripts (one per system, used by per-arch CI jobs)
  pushArch = lib.mapAttrs (
    system: image:
    pkgs.writeShellApplication {
      name = "push-nix-${system}";
      runtimeInputs = [
        skopeo
        pkgs.cachix
      ];
      text = # bash
        ''
          skopeo login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          skopeo --insecure-policy copy nix:${image} docker://${imageRef system}
          # The manifest, not a tarball. Its closure is every layer, so a
          # node can fetch what the image holds without the registry.
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

        regctl index create ${repo}/nix:${nixkubeVersion}-${pkgs.nix.version} \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
        regctl index create ${repo}/nix:latest \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
      '';
  };
}
