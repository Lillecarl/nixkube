# SPDX-License-Identifier: Apache-2.0

{ pkgs }:
let
  version = "1.0.1";

  mkEmptyImage =
    GOARCH:
    pkgs.runCommand "scratch-${GOARCH}-${version}"
      {
        nativeBuildInputs = [ pkgs.umoci ];
      }
      ''
        umoci init --layout $out
        umoci new --image $out:${GOARCH}
        umoci config --image $out:${GOARCH} \
          --config.env PATH=/nix/var/result/bin \
          --architecture ${GOARCH} \
          --os linux
      '';

  x86Image = mkEmptyImage "amd64";
  armImage = mkEmptyImage "arm64";

  push =
    let
      registry = "ghcr.io/lillecarl/nix-csi/scratch";
      x86Tag = "${registry}:${builtins.baseNameOf x86Image}";
      armTag = "${registry}:${builtins.baseNameOf armImage}";
    in
    pkgs.writeShellApplication {
      name = "push";
      runtimeInputs = [ pkgs.crane ];
      text = ''
        set -euo pipefail

        crane auth login --username "$REPO_USERNAME" --password "$REPO_TOKEN" ghcr.io
              
        crane push ${x86Image} ${x86Tag}
        crane push ${armImage} ${armTag}

        crane index append \
          --manifest ${x86Tag} \
          --manifest ${armTag} \
          --tag ${registry}:${version}

        crane index append \
          --manifest ${x86Tag} \
          --manifest ${armTag} \
          --tag ${registry}:latest
      '';
    };

in
{
  inherit
    push
    x86Image
    armImage
    ;
}
