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

            # pynixd is the fallback here, not the source. nodeEnv is a plain
            # buildEnv over nixkube and generic tools, with nothing from a
            # consumer's configuration in it, so the substituters in /etc/nix
            # are what normally serve it. This block is for the case where
            # they do not have it yet.
            PYNIXD_STATE="disabled"
            EXTRA_SUBSTITUTERS="local?trusted=true"
            if [ "''${PYNIXD_ENABLED:-false}" = "true" ]; then
              if nix store ping --store ssh-ng://nix@pynixd; then
                EXTRA_SUBSTITUTERS="$EXTRA_SUBSTITUTERS ssh-ng://nix@pynixd?trusted=true"
                PYNIXD_STATE="answered"
              else
                PYNIXD_STATE="unreachable"
              fi
            fi

            # Carrying on without pynixd is right, and the silence is not.
            #
            # `nix build` fails with "no substituter that can build it"
            # whether pynixd was unreachable, or answered and did not have the
            # path, or was never enabled. Those are three different problems
            # with one sentence between them. Both of the first two happened on
            # nixlab2 in one day and could not be told apart from the error.
            #
            # The retry is only for `unreachable`. pynixd may be starting
            # elsewhere in the cluster, and this node may be what it is waiting
            # for -- see issue #27, where that is a cycle rather than a race.
            # Nothing else here gets better by being asked twice.
            attempt=1
            max_attempts=5
            until nix \
              build \
                --extra-substituters "$EXTRA_SUBSTITUTERS" \
                --max-jobs auto \
                --option sandbox false \
                --store /nix-volume \
                --out-link /nix-volume/nix/var/result \
                --fallback \
                "$STORE_PATH"; do
              if [ "$PYNIXD_STATE" = "unreachable" ] && [ "$attempt" -lt "$max_attempts" ]; then
                delay=$(( attempt * 10 ))
                echo "pynixd did not answer; retry $attempt of $max_attempts in ''${delay}s" >&2
                sleep "$delay"
                attempt=$(( attempt + 1 ))
                continue
              fi
              {
                echo "cannot get $STORE_PATH"
                echo "  pynixd:       $PYNIXD_STATE"
                echo "  substituters: $EXTRA_SUBSTITUTERS"
                echo "  and whatever /etc/nix/nix.conf adds:"
                sed -n 's/^substituters *= */    /p' /etc/nix/nix.conf || true
                case "$PYNIXD_STATE" in
                  unreachable)
                    echo "  pynixd was asked and did not answer, so it served nothing here."
                    echo "  This path should come from a binary cache. If it does not, the"
                    echo "  cache is behind: nothing published it. See nixkube issue #27."
                    ;;
                  answered)
                    echo "  pynixd answered and does not have this path. It is not a"
                    echo "  connectivity problem."
                    ;;
                  disabled)
                    echo "  pynixd is disabled, so a binary cache is the only source."
                    ;;
                esac
              } >&2
              exit 1
            done

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
            # `-e` alone is wrong, because a store path can itself be a symlink.
            #
            # `nix-<version>-man` is one: it points at an absolute
            # `/nix/store/...-nix-manual-...`. Read from /nix-volume that
            # target resolves against the container's own /nix/store, where it
            # is not, so `-e` calls a present path absent. At runtime the CSI
            # mounts this store at /nix and it resolves.
            #
            # Measured on a node: 2 of 312 entries are symlinks, both
            # `nix-*-man`, and both were rejected while every other entry
            # passed. `-L` accepts the link itself; nix already guarantees the
            # target is in the closure, which the loop checks separately.
            missing=0
            while read -r p; do
              if [ ! -e "/nix-volume$p" ] && [ ! -L "/nix-volume$p" ]; then
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
