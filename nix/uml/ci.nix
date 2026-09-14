# SPDX-License-Identifier: MIT

# What the kind jobs do, on a guest instead of on a container.
#
#     nix run --file . ciTest.run
#
# `nix/uml/default.nix` is the other cluster test and asks a different
# question: it renders the manifest, applies it, and then breaks the node
# nine ways to see whether the driver comes back.  It also runs on a node
# this repository's harness built specially -- every image nix-built and
# imported, the store mounted into every static pod.
#
# This one is the kind jobs, ported.  The node pulls its images from
# registry.k8s.io like any other node, nothing is patched, and the
# deployment goes through `kubenixDeploy` -- the same script CI runs, doing
# the same kluctl deploy -- rather than through a manifest this test
# rendered itself.  That last part is most of the point: the deployment
# script is a thing that can break, and nothing tested it before.
#
# **kluctl is deprecated.** It is still what CI uses, so this ports it
# faithfully rather than getting ahead of the replacement.
#
# kluctl runs on the *host*, against the guest's API server through a passt
# forward, which is where kind runs it too -- on the runner, not inside a
# node.  It also keeps `cachix push` in `kubenixCI1`'s pre-deploy step
# working, which needs a token the guest does not have.
#
# **Outside the build sandbox only.** The node pulls from registry.k8s.io
# and the images this deploys come from ghcr.io, so there is nothing to
# run here without a network.
{
  pkgs,
  lib,
  sources,
  instance,
  workloads,
  # Which jobs the deployment creates, and which of them are asserted.
  # Both lists come from ci/workflows/ci.nix so the two cannot drift.
  deployedJobs,
  assertedJobs,
  name,
}:
let
  uml = import (sources.user-mode-nixos + "/lib.nix") { inherit pkgs; };
in
uml.mkTest {
  inherit name;
  script = ./ci.py;
  backend = "qemu";

  nodes.cp =
    { config, ... }:
    {
      imports = [ (sources.user-mode-nixos + "/modules/k8s.nix") ];

      services.uml-k8s = {
        enable = true;
        role = "control-plane";

        /*
          A node like any other node.

          `images = "nix"`, which is the default and what the other
          cluster tests use, builds every image from nixpkgs and mounts
          `/nix/store` into the static pods to make them resolve. That is
          what lets a cluster run inside a build sandbox -- and it is
          exactly wrong here, because nixkube's whole job is to put a
          store into a pod. On such a node it cannot be told apart from
          the node doing it, and this test would pass with its subject
          switched off.
        */
        images = "pull";

        # The DaemonSet asks containerd for an NRI connection whether or
        # not containerd is listening, and gets no error when it is not.
        nri = true;
      };

      boot.uml = {
        memory = "14336M";
        cpus = 4;
        diskSize = 8192;
        /*
          The API server, reachable from the host, because kluctl runs
          there.

          On port 16443 and not 6443. The runner gives a guest a
          127.0.0.x of its own and needs every port in a rule free on it,
          and a developer machine that is itself running Kubernetes has
          `*:6443` bound already -- which takes every loopback address at
          once and fails the run before the guest boots, with "no free
          address in 127.0.0.2-254".

          Two ports in one rule rather than two rules: both would be given
          the same address anyway.
        */
        forward = [
          {
            ports = [
              config.boot.uml.sshPort
              {
                host = 16443;
                guest = 6443;
              }
            ];
          }
        ];
        lan = {
          network = "nixkube-ci";
          address = "10.105.0.1/24";
        };
      };

    };

  settings = {
    deploy = "${instance.deploymentScript}";
    deployWorkloads = "${workloads.deploymentScript}";
    inherit deployedJobs assertedJobs;
  };
}
