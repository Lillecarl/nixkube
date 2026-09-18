# SPDX-License-Identifier: MIT
#
# Spike B: the same layering policy through pkgs.dockerTools.
#
# **`streamLayeredImage` cannot express it.** Its knobs are `maxLayers` and
# nothing else: it sorts the closure by how many other paths refer to each
# one, gives the most-referred its own layers, and puts the whole remainder in
# the last layer. There is no argument that says which path belongs to which
# layer. That is the finding, and `baseline` below is what the heuristic
# actually does with our closure.
#
# `buildImage` can express it, by a different route. Each call adds exactly
# one layer over `fromImage`, so a chain of them is a chain of layers with
# content we choose. The cost is in the build, not the result: every link
# unpacks its parent's tarball and writes a new one, so the work is quadratic
# in the number of layers and every byte moves once per link.
{ pkgs }:
let
  policy = import ./policy.nix { inherit pkgs; };

  # One `buildImage` for each group, each on top of the last.
  chained = builtins.foldl' (
    parent: group:
    pkgs.dockerTools.buildImage {
      name = "nix-spike-dt";
      tag = group.name;
      copyToRoot = pkgs.buildEnv {
        name = "layer-${group.name}";
        paths = group.deps;
      };
      fromImage = parent;
    }
  ) null policy.groups;
in
{
  # The policy, expressed. Slow to build; see the header.
  chain = chained;

  # What dockerTools does when asked for the same content with no policy.
  # This is today's shape, and the one the layer budget breaks.
  baseline = pkgs.dockerTools.streamLayeredImage {
    name = "nix-spike-dt-baseline";
    tag = "spike";
    maxLayers = 125;
    includeNixDB = true;
    contents = policy.contents ++ [ policy.app ];
    config = {
      Env = [ "PATH=${pkgs.lib.makeBinPath policy.runtimeInputs}" ];
      Entrypoint = [ "${pkgs.bashInteractive}/bin/bash" ];
    };
  };

  inherit (policy) groups;
}
