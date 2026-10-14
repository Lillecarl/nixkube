# Where every dependency of nixkube lives: nixidae
# (https://github.com/nixidae/nixidae), the repository this one is exported
# from. Work on nixkube happens there.
#
# Inside nixidae this directory is `<nixidae>/nixkube`, and nixidae's own
# nix/wire.nix answers. On its own, `flake.lock` names the nixidae commit to
# build against, and this tree takes the place of its copy there: what git
# tracks of it, as a store path named `source`, the way nixidae copies each
# source.
let
  root = toString ../.;
  # `../..` from a bare store path leaves the store root, and Nix refuses to
  # evaluate it. `builtins.tryEval` does not catch that.
  escapesStore = builtins.match "/nix/store/[^/]+" root != null;
  inNixidae = !escapesStore && builtins.pathExists ../../nix/wire.nix;

  lock = builtins.fromJSON (builtins.readFile ../flake.lock);
  nixidae = builtins.fetchTree lock.nodes.${lock.nodes.${lock.root}.inputs.nixidae}.locked;

  tree = if builtins.pathExists ../.git then (builtins.fetchGit { url = root; }).outPath else ../.;
  self = builtins.path {
    path = tree;
    name = "source";
  };
in
if inNixidae then
  import ../../nix/wire.nix { }
else
  import "${nixidae}/nix/wire.nix" { overrides.nixkube = self; }
