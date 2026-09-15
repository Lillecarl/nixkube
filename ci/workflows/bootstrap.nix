# What every job of every workflow here needs before it does anything of
# its own, as the `ghanix` options a job asks for by name.
#
# One file because there are two workflows, and issue #33 is what it cost
# when there were two copies: `ci.yaml` held the substituters in a composite
# action and `test-nixos.yaml` wrote them again inline, and nothing made the
# two agree. The issue said they could not be one value yet, because the
# action was YAML that nothing rendered and ghanix described workflows
# rather than actions. `ghanix.nix.install.settings` is where they go now,
# and the action is gone.
{ lib }:
rec {
  /*
    The Nix every job runs with.

    cachix/install-nix-action, and not nixbuild/nix-quick-install-action,
    because this one installs a daemon. A daemon builds in its own sandbox
    under /build; a single-user nix builds under the runner's TMPDIR, and
    everything a derivation touches then depends on where the checkout
    happened to be. That is not theoretical: it hung both builders for a
    day, because the nri-wait tests bind a unix socket whose path holds 107
    bytes, and the same socket came to 77 bytes in the sandbox and 125 on
    the runner. See #23.

    No GitHub store cache. cachix is the cache -- see #21.
  */
  settings = {
    # A daemon ignores substituters and public keys that a user it does not
    # trust asks for. Without this every path would come from
    # cache.nixos.org and everything of ours would build from source.
    trusted-users = [
      "root"
      "runner"
    ];
    # nixkube is the cache this repository owns. lillecarl is the one the
    # other umbrella projects push to, and it is here because this
    # repository's closure contains theirs: without it every job rebuilt
    # nanopynix from source. A read-only substituter costs nothing when it
    # misses.
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "nixkube.cachix.org-1:H8UE0jlI9pxHexK/NhDmEoLDarJXp1WTymQrsajlh7M="
      "lillecarl.cachix.org-1:NN/LLMg7mbyvZCu32Qlo8LpSHqNw7Rr3VBCEYQvRpT0="
    ];
    substituters = [
      "https://cache.nixos.org?priority=1"
      "https://nixkube.cachix.org?priority=2"
      "https://lillecarl.cachix.org?priority=3"
    ];
    # No `extra-platforms`. Each architecture is built on a runner of that
    # architecture -- `build-arm64` is `ubuntu-24.04-arm` -- so nothing here
    # needs emulation, and permitting it only means an accidental
    # cross-architecture build runs for hours under qemu instead of failing
    # at once.
  };

  # A checkout and that Nix. `job.ghanix` is ghanix's own and is stripped
  # before the YAML is written; each option enabled here puts a step at the
  # front of the job's `steps`, ahead of everything the job wrote itself.
  bootstrap = {
    checkout.enable = true;
    nix.install = {
      enable = true;
      inherit settings;
    };
  };

  /*
    What a job that boots a guest needs on top.

    passt unshares a user namespace before it serves the guest's uplink and
    exits if it cannot, and QEMU opens /dev/kvm. Issue #35 is what the
    first of those cost when the step was missing: every guest booted with
    no network and reported it, minutes later, as a name that would not
    resolve.

    `mkMerge` and not `//`, which is shallow: an addition under `nix` would
    otherwise replace the whole install block and take the substituters
    with it, silently.
  */
  guest = lib.mkMerge [
    bootstrap
    {
      userNamespaces.enable = true;
      openKvm.enable = true;
    }
  ];
}
