# SPDX-License-Identifier: MIT

# Assert that every store path a manifest names can be fetched.
#
# A nixkube CSI volume names an output path, so a node substitutes it and can
# never build it. A path no substituter serves is a mount that fails on the
# node, well away from whatever forgot to push it.
#
# **This is a wrapper now; the check lives in `ekn`.** It was 214 lines of
# Python here, reimplementing what `ekn` needs anyway: `ekn kubeapply` asks
# the same question before it applies, through `ekn.assertCached`. Two
# implementations of one check drift, and the one that drifts is the one
# fewer people run.
#
# `ekn`'s `storecheck` keeps all three properties this had, each of which was
# measured against a case that breaks without it:
#
# - Walks the closure. A present top path with an absent member is issue #8.
# - Unions the substituters. `cacheEnv` is on nixkube.cachix.org; its member
#   `python3.14-httpx` is only on cache.nixos.org. Either alone reports a
#   false failure.
# - Asks the caches and never the local store, so a path this machine happens
#   to have built does not pass. `session.store(uri=...)` per substituter.
#
# The command line is unchanged, so CI's two call sites did not move.
{
  lib,
  pkgs,
  # From the module, so the list checked is the list a node carries. Not the
  # substituters of whatever machine runs this: CI names more caches than a
  # node does, and asking a superset passes a path no node can fetch.
  substituters,
  # easykubenix's own build of the CLI.
  ekn,
}:
pkgs.runCommand "assert-cached"
  {
    nativeBuildInputs = [ pkgs.makeWrapper ];
    meta.mainProgram = "assert-cached";
  }
  ''
    makeWrapper ${lib.getExe ekn} $out/bin/assert-cached \
      --add-flags assert-cached \
      ${lib.concatMapStringsSep " " (
        uri: "--add-flags --substituter --add-flags ${lib.escapeShellArg uri}"
      ) substituters}
  ''
