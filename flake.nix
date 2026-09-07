# SPDX-License-Identifier: MIT

# The public surface of this repository, for a consumer who uses flakes.
#
# **This is not how the repository builds.** `default.nix` is, and the nixidae
# umbrella hands it every source. This file exists because flakes have the
# market share: it lets somebody write an input for this repository and get a
# curated set of outputs, rather than nothing.
#
# So it holds no logic. It names what is public and calls `default.nix`, and
# a change to how anything is built happens there.
#
# Two inputs, and neither duplicates the umbrella's pins.
#
#   nixpkgs   The consumer's, and the point of the exercise. It is handed to
#             the umbrella in place of the revision nix/sources.lock names,
#             so `inputs.<this>.inputs.nixpkgs.follows = "nixpkgs"` does what
#             a flake user expects it to. Measured on pynixd: with the
#             umbrella's own revision the flake and `--file .` give the same
#             derivation, d3w8gnxlihcrdvz56fwqlwm4k4r3p6ba.
#
#   nixidae   Which umbrella, and nothing else. `nix/sources.nix` finds one
#             by an impure fetch, which a flake evaluation cannot do, so the
#             lock beside this file pins it instead. Every other source comes
#             from that revision's own nix/sources.lock.
#
# `flake.lock` here therefore has two nodes and pins nothing twice.
{
  description = "A CSI driver for Nixxing Kubernetes";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    nixidae = {
      url = "github:nixidae/nixidae";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixidae,
    }:
    let
      inherit (nixpkgs) lib;
      # Linux only. This is a CSI driver and a container runtime plugin.
      forEachSystem = lib.genAttrs [
        "aarch64-linux"
        "x86_64-linux"
      ];

      # The umbrella's own set, with the consumer's nixpkgs in place of the
      # one it names.
      sources = import "${nixidae}/nix/wire.nix" {
        overrides.nixpkgs = nixpkgs.outPath;
      };

      # No `pkgs` argument: this repository builds its own package set,
      # because it applies an overlay of its own.
      each = forEachSystem (system: import ./. { inherit sources system; });
    in
    {
      # `kubenixApply` and its siblings are deliberately absent. Each one is
      # an easykubenix evaluation rather than a derivation, so `nix build`
      # cannot realise it and `nix flake show` throws on it. A consumer who
      # wants one takes the module below and evaluates it themselves.
      packages = forEachSystem (system: {
        inherit (each.${system}) nixkube-docs;
        default = each.${system}.nixkube-docs;
      });

      # The easykubenix module that installs the driver, which is what a
      # consumer actually wants from this repository.
      easykubenixModules.default = ./kubenix;

      formatter = forEachSystem (system: each.${system}.treefmt);
    };
}
