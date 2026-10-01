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
*/
{
  roots,
  primary ? null,
  tools,
  system,
}:
derivation {
  name = "nixkube-composefs";
  inherit system;
  builder = "${tools.nixkube}/bin/nixkube-composefs-build";
  __structuredAttrs = true;
  exportReferencesGraph.closure = roots;
  primary = if primary == null then "" else "${primary}";
  nixStore = "${tools.nix}/bin/nix-store";
  mkcomposefs = "${tools.composefs}/bin/mkcomposefs";
}
