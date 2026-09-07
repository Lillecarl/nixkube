# Where every dependency of this repository lives.
#
# nixidae is the umbrella that holds it, and the umbrella owns every source.
# Inside the umbrella that is the checkout two directories up. Outside it,
# the umbrella is fetched, and this working copy is put in place of the copy
# that came down with it. Either way the answer is the same one, so a build
# here and a build from the umbrella agree.
#
# A plain tarball is enough. The umbrella records every revision in
# nix/sources.lock, a file in its own tree, so a fetch that leaves the
# submodule directories empty still resolves all of them.
#
# `..` from a store path leaves the store root, and Nix refuses that rather
# than answering false: "'nix' is too short to be a valid store path". So ask
# only when this checkout is not itself in the store.
#
# This repository is not a flake. Every entry point here takes the set this
# file returns and imports what it wants.
let
  inUmbrella =
    builtins.substring 0 11 (toString ../.) != "/nix/store/" && builtins.pathExists ../../nix/wire.nix;

  wire =
    if inUmbrella then
      ../../nix/wire.nix
    else
      (builtins.fetchTree (builtins.parseFlakeRef "github:nixidae/nixidae")).outPath + "/nix/wire.nix";
in
import wire { overrides.nixkube = ../.; }
