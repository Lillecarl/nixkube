# SPDX-License-Identifier: MIT

# nixkube on a real Kubernetes node, inside a Nix build sandbox.
#
#     nix build --file . umlTest
#
# tests/nixos/integration.nix asks the same questions of a QEMU VM, and needs
# KVM and a network to do it. This runs the node as an ordinary process under
# User-Mode Linux -- no KVM, no root, no tap device -- so the whole thing is a
# derivation that passes or fails, and CI needs nothing but a builder.
#
# What makes that possible is that a guest's /nix/store is the *sandbox's*
# store, over hostfs, with a writable overlay on top; and user-mode-nixos
# bind-mounts it into every container. So a store path this file names is a
# path the node has, and the pods on it have, with nothing copied and nothing
# fetched. `boot.uml.nixDatabase` then registers that store so Nix inside the
# guest will use it rather than declaring every path invalid.
{
  pkgs,
  lib,
  sources,
  umlImages,
  manifest,
}:
let
  uml = import (sources.user-mode-nixos + "/lib.nix") { inherit pkgs; };

  # The rendered manifest, and the root of everything the node needs.
  #
  # `nixkube.push = true` (see ./manifest.nix) keeps the string context on the
  # DaemonSet's store paths, so the node environment is in this file's
  # closure. Naming it in `settings` is what pulls that closure into the
  # sandbox; naming it in `nixDatabase.extraRoots` is what makes Nix inside
  # the guest agree the paths are real.
  manifestFile = manifest.manifestYAMLFile;

in
uml.mkTest {
  name = "nixkube";
  script = ./test.py;

  nodes.cp = {
    imports = [ (sources.user-mode-nixos + "/modules/k8s.nix") ];

    services.uml-k8s = {
      enable = true;
      role = "control-plane";

      # There is no registry. Every image the DaemonSet names is imported
      # into containerd before kubelet starts; ./images.nix checks that the
      # tags match what the manifest asks for.
      extraImages = umlImages.tarballs;

      /*
        No CoreDNS. kube-proxy stays.

        Nothing here resolves a name: an in-cluster client reads
        KUBERNETES_SERVICE_HOST, which is an address. So CoreDNS is two pods
        on a one-CPU guest doing nothing but timing out against an upstream
        resolver a build sandbox cannot reach, several lines a second.

        kube-proxy looked equally unnecessary, because the manifest declares
        no Service. It is not: the *cluster* declares one.
        `kubernetes.default` is how anything in a pod reaches the API
        server, and DNAT'ing its ClusterIP is exactly what kube-proxy does.
        Measured by taking it away -- nixkube's init Job runs
        `kubectl get secret` and exited non-zero.

        `test.py` waits for `KUBE_PROXY` alone, to match.
      */
      skipAddons = [ "coredns" ];
    };

    boot.uml = {
      # A control plane, a CSI driver, an NRI plugin and whatever the test
      # schedules, all in one guest.
      memory = "4096M";
      diskSize = 4096;
      lan = {
        network = "nixkube";
        address = "10.103.0.1/24";
      };
      nixDatabase = {
        enable = true;
        /*
          The manifest, and what a workload will ask the driver to mount.

          The manifest's own closure carries the node environment, because
          `nixkube.push = true` keeps the string context on it. Registering
          that closure is what lets the init container's
          `nix build --store /nix-volume` see the environment as a valid path
          rather than something it has to go and fetch.
        */
        extraRoots = [
          "${manifestFile}"
          "${pkgs.hello}"
        ];
      };
    };

    # What the sidecar images point into the store, named where Nix can see
    # it. Their layers are gzipped, so nothing else says these paths are
    # needed, and a container whose entrypoint is missing fails in runc
    # rather than anywhere informative. See ./images.nix.
    system.extraDependencies = umlImages.runtimeInputs;

    # For looking around by hand when something fails. The pods get their own
    # configuration from the ConfigMap the manifest carries, not from this.
    nix.settings.experimental-features = [
      "nix-command"
      "flakes"
    ];
  };

  settings = {
    manifest = "${manifestFile}";
    kubernetesVersion = pkgs.kubernetes.version;
    # What a workload will ask the CSI driver to mount. A store path this
    # file names is a path the sandbox has.
    workloadStorePath = "${pkgs.hello}";
    workloadImage = "uml.test/busybox:1";
  };
}
