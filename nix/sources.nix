# Where every dependency of this repository lives.
#
# nixidae is the umbrella that holds it, and the umbrella owns every source.
# Inside the umbrella that is the checkout two directories up. Outside it,
# the umbrella is fetched, and this working copy is put in place of the copy
# that came down with it. Either way the answer is the same one, so a build
# here and a build from the umbrella agree.
#
# A plain tarball is enough. The umbrella records every revision in
# nix/sources.lock, a file in its own tree, and the working copies beside it
# are ignored rather than committed, so a fetch that brings down none of them
# still resolves all of them.
#
# `..` from a store path leaves the store root, and Nix refuses that rather
# than answering false: "'nix' is too short to be a valid store path". So ask
# only when this checkout is not itself in the store.
#
# This repository is not a flake. Every entry point here takes the set this
# file returns and imports what it wants.
let
  # Where this checkout sits. Inside the umbrella, whether that umbrella is a
  # working copy or a pinned store path, this is `<umbrella>/nixkube`.
  # Fetched on its own, it is a store path with nothing above it.
  root = toString ../.;

  # `../..` from a bare store path leaves the store root, and Nix refuses to
  # evaluate that at all: "'nix' is too short to be a valid store path".
  # `builtins.tryEval` does not catch it -- measured, not assumed -- so the
  # question has to be avoided rather than caught.
  #
  # A bare store path is exactly `/nix/store/` plus one component. Anything
  # deeper has a directory between this checkout and the store root, which is
  # precisely the case where the umbrella is the thing in the store and this
  # checkout is inside it.
  escapesStore = builtins.match "/nix/store/[^/]+" root != null;

  # Being in the store does not mean the umbrella is absent. An earlier
  # version tested "not in the store", which is only ever true in a working
  # copy -- so every downstream project that pinned the umbrella silently
  # took the fetch below instead of the umbrella it shipped inside.
  inUmbrella = !escapesStore && builtins.pathExists ../../nix/wire.nix;

  # The umbrella itself, when this checkout is on its own.
  #
  # **This fetch is the one the umbrella cannot cover.** Every other source
  # goes through the umbrella's own `nix/resolve.nix`. This one has to find
  # the umbrella first.
  #
  # UMBRELLA_REV names it, and that is what makes two jobs of one CI run
  # agree. `ci/walkback.sh` computes it: the umbrella that locks the nearest
  # landed ancestor of HEAD, read from the umbrella remote refs named
  # `refs/umbrella/nixkube/*`. `umbrella mark` publishes them.
  #
  # Before umbrella 0.1.0 this fell back to an unlocked
  # `github:nixidae/nixidae`, which resolves the head of the default branch at
  # evaluation time, every time `tarball-ttl` does not answer from cache. So
  # the same commit gave a different answer an hour later, with nothing here
  # changed and no lock moved. Measured 2026-09-16 by a consumer whose nixidae
  # pin had not moved since 2026-09-14 and whose render hash moved anyway,
  # twice. Issue Lillecarl/nanopynix#301.
  umbrellaRev = builtins.getEnv "UMBRELLA_REV";

  # The umbrella revision this checkout was written against, when it records
  # one.
  #
  # **A file, and not a git ref, because only a file survives every way this
  # expression is read.** A tree that Nix fetched carries no `.git` and no
  # revision marker, so an expression inside it cannot learn its own revision
  # or its own url. Measured: `builtins.readDir` of a fetched tree lists the
  # source files and nothing else. A consumer that took this repository as
  # `github:Lillecarl/nixkube`, a release tarball, or a pull request from a
  # fork could not resolve a pin held in a ref. GitHub copies `refs/heads`
  # and tags to a fork, and not a custom ref namespace. A file is in the
  # tree, so every one of those reads it.
  #
  # It is also the only mechanism that works under `--pure-eval`, where
  # `builtins.getEnv` answers "". That is what lets a flake consumer resolve
  # a pinned umbrella rather than the moving head of a branch.
  #
  # **It names the umbrella this commit was written against, and not the one
  # that locks this commit.** Those cannot be the same. The umbrella lock
  # holds this commit's hash, so a commit that held the lock's hash would
  # need a hash that contains itself. The earlier revision is the useful one
  # anyway: a build of this checkout overrides this repository with the
  # checkout, so the umbrella supplies every *other* source, and the revision
  # the work was done against is the one that supplied them.
  #
  # `builtins.match` rather than a trim helper, because it also rejects a
  # file that holds anything but a hash.
  pinMatch =
    if builtins.pathExists ./umbrella.rev then
      builtins.match "[ \n\t]*([0-9a-f]{40})[ \n\t]*" (builtins.readFile ./umbrella.rev)
    else
      null;
  pinned = if pinMatch == null then "" else builtins.head pinMatch;

  umbrellaRef =
    if umbrellaRev != "" then
      "git+https://github.com/nixidae/nixidae?rev=${umbrellaRev}&shallow=1"
    else if pinned != "" then
      "git+https://github.com/nixidae/nixidae?rev=${pinned}&shallow=1"
    else
      # **No third source.** `builtins.tryEval` does not catch a failed
      # `fetchGit` -- measured, not assumed -- so an arm that guessed at a
      # mapping ref could not fall back when that ref is absent. The lookup
      # belongs outside Nix, in `ci/walkback.sh`.
      throw ''
        nix/sources.nix: no umbrella to build nixkube against.

        Set UMBRELLA_REV, or write an umbrella revision into
        nix/umbrella.rev. This prints the one that locks this checkout:

          ci/walkback.sh https://github.com/nixidae/nixidae nixkube

        umbrella 0.1.0 removed the arm that fell back to the head of the
        umbrella default branch. That arm was unlocked, so one commit of
        this repository rendered differently from one hour to the next.
        See umbrella/docs/releases.md.
      '';

  wire =
    if inUmbrella then
      ../../nix/wire.nix
    else
      (builtins.fetchTree (builtins.parseFlakeRef umbrellaRef)).outPath + "/nix/wire.nix";
in
import wire { overrides.nixkube = ../.; }
