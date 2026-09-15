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
  # goes through the umbrella's own `nix/resolve.nix`, which UMBRELLA_GIT
  # already reaches. This one has to find the umbrella first, so it reads the
  # variable a second time.
  #
  # `github:` here is unlocked -- no revision, no narHash -- so Nix asks
  # api.github.com for the head of the default branch on every evaluation
  # that `tarball-ttl` does not answer from cache. That is one call per job
  # before any source is resolved at all. Anonymous api.github.com allows 60
  # an hour per IP and GitHub's runners share a NAT pool.
  #
  # The git reference resolves the same head over the git protocol, which
  # that limit does not count.
  # UMBRELLA_REV pins it, and that is what makes two jobs of one CI run agree.
  #
  # Without it the reference below is unlocked, so it resolves the head of the
  # default branch again in every job. `umbrella land` pushes the working
  # copies, which starts the run, and the umbrella lock commit follows
  # seconds later, so a run straddles the push and its jobs read two
  # revisions. Measured in nixkube run 35026926963: seven seconds between the
  # first job starting and the commit, two `cacheEnv` paths, and a job that
  # asked for one nothing had built. Issue Lillecarl/nanopynix#301.
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
    else if builtins.getEnv "UMBRELLA_GIT" != "" then
      "git+https://github.com/nixidae/nixidae?shallow=1"
    else
      "github:nixidae/nixidae";

  wire =
    if inUmbrella then
      ../../nix/wire.nix
    else
      (builtins.fetchTree (builtins.parseFlakeRef umbrellaRef)).outPath + "/nix/wire.nix";
in
import wire { overrides.nixkube = ../.; }
