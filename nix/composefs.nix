# SPDX-License-Identifier: MIT

/**
  A CSI volume as a composefs image: `$out/image.cfs`, and `$out/db`, the
  closure's Nix database, which the image presents as /nix/var/nix/db. Issues
  #68 and #25; `pkgs/nixkube/src/composefs.py` is the builder and says what
  is in the image.

  A plain `derivation` with its tools passed in, and no nixpkgs: a node
  evaluates it with nothing but the store paths it already has, so a node and
  CI that pass the same tools get the same derivation, and a node substitutes
  what CI or pynixd built. The image names every closure path's hash, so its
  references are the closure: a GC root on the image keeps the closure.

  # Inputs

  `roots`
  : Store paths whose closure the volume holds.

  `primary`
  : The store path whose tree goes at the volume's root, and that
    /nix/var/result points to; or null.

  `tools`
  : `{ nixkube, composefs, nix }`, the packages the builder runs.

  `system`
  : The system to build for.

  `opaque`
  : True names every input as a store path, the way a node can: a node has
    the outputs and none of their `.drv` files. An input named by its
    derivation is another derivation with another output path, measured, so
    only the opaque form is the one nodes share. It builds only where the
    inputs already exist; false builds anywhere and is for checking content.
*/
{
  roots,
  primary ? null,
  tools,
  system,
  opaque ? true,
}:
let
  input =
    p:
    let
      bare = builtins.unsafeDiscardStringContext (toString p);
    in
    if opaque then builtins.appendContext bare { ${bare}.path = true; } else toString p;
in
derivation {
  name = "nixkube-composefs";
  inherit system;
  builder = "${input tools.nixkube}/bin/nixkube-composefs-build";
  __structuredAttrs = true;
  # Sorted, so the same roots in another order are the same derivation:
  # a node writes this derivation itself, from a set.
  exportReferencesGraph.closure = builtins.sort builtins.lessThan (map input roots);
  primary = if primary == null then "" else input primary;
  nixStore = "${input tools.nix}/bin/nix-store";
  mkcomposefs = "${input tools.composefs}/bin/mkcomposefs";
}
