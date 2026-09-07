# SPDX-License-Identifier: MIT

# What has to be true of a nixkube deployment for it to come up inside a Nix
# build sandbox, on User-Mode Linux.
#
# An easykubenix module rather than a patch to `kubenix/`: none of this is how
# nixkube should behave on a real cluster, and a default that only suits a
# test is a default that misleads everybody else.
#
# Two things are different in a sandbox, and both come from the same fact --
# there is no network:
#
#   images   Nothing can be pulled. Every image is imported into containerd
#            before kubelet starts (see ./images.nix and
#            `services.uml-k8s.extraImages`), so every container has to say
#            `Never` -- `Always`, which two of them do say, fails on an image
#            that is already there.
#
#   store    The node's Nix store is normally a directory of its own that
#            the init container fills by *substituting* into it. Here there
#            is nothing to substitute from and nothing to fill: the guest's
#            /nix/store is the sandbox's, so the node environment is already
#            in it, and `boot.uml.nixDatabase` has already told Nix so.
#            `hostMountPath = "/"` points the node at that store instead of
#            at a copy of it.
{ lib, ... }:
let
  # Every container in an object, whether it runs first or not.
  pullNever =
    object:
    let
      never = container: container // { imagePullPolicy = "Never"; };
      fix =
        spec:
        spec
        // lib.optionalAttrs (spec ? containers) { containers = map never spec.containers; }
        // lib.optionalAttrs (spec ? initContainers) {
          initContainers = map never spec.initContainers;
        };
    in
    if object ? spec && object.spec ? template && object.spec.template ? spec then
      lib.recursiveUpdate object { spec.template.spec = fix object.spec.template.spec; }
    else
      object;
in
{
  kubernetes.transformers = [ pullNever ];

  nixkube = {
    # One architecture. Nothing here cross-compiles, and the sandbox has no
    # builder to send the other one to.
    systems = {
      x86_64-linux = true;
      aarch64-linux = false;
    };

    # No cache StatefulSet. It exists to coordinate distributed builds, and
    # nothing is built here -- every path a workload asks for is already in
    # the sandbox because the test derivation named it.
    pynixd.enable = false;

    # Keeps the Nix string context on the DaemonSet's store paths, so the node
    # environment is part of the manifest's closure. That is what carries it
    # into the sandbox at all, and `boot.uml.nixDatabase.extraRoots` in
    # ./default.nix registers that same closure inside the guest.
    push = true;

    /*
      The node's store is the guest's store, not a copy of it.

      `/` is what the option's own description calls untested, and this is
      the test. The layout works out exactly: kubenix/daemonset.nix mounts
      this path at `/nix-volume` in the init container, where Nix makes a
      chroot store and therefore looks for `/nix-volume/nix/store`; and at
      `/nix` in the node container, with `subPath = "nix"`. With `/` both
      resolve to the guest's own `/nix/store` -- which is the build
      sandbox's, over hostfs.

      So `nix build --store /nix-volume` finds the node environment already
      present and already valid, and copies nothing. The default
      `/var/lib/nix-csi` would have it fetch, over HTTP, a closure sitting
      on the same filesystem.
    */
    hostMountPath = "/";

    /*
      No substituters at all.

      Nothing should need one -- see above -- and an unreachable one is
      worse than none. With cachix and cache.nixos.org in the list (which
      is what kubenix/ci sets) a miss costs four HTTP retries and a DNS
      timeout each before failing, which reads as a hang. Empty means a
      genuine miss fails at once and says so:

          error: path '...-nodeEnv' is required, but there is no
          substituter that can build it
    */
    node.nixConfig.settings.substituters = lib.mkForce [ ];
  };
}
