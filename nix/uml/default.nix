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
# store, over hostfs. So a store path this file names is a path the guest
# already has, with nothing fetched -- `boot.uml.nixDatabase` registers it so
# Nix in the guest agrees, and `nixkube-seed-store` below copies it one
# directory across into the node's own store, which is where nixkube looks.
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

  probes = pkgs.callPackage ./probe.nix { };

  # `nixkube.hostMountPath` in ./manifest.nix. The driver's own paths are
  # relative to it, and `settings.hostMountPath` below tells the test where
  # to look for them.
  hostMountPath = "/nixkube";

  # Everything the node has to have without a network: the manifest, whose
  # closure carries the node environment; the probes, whose closures carry
  # what they run; and what a workload asks the driver to mount.
  #
  # The same list twice on purpose. Registered, so Nix in the guest agrees
  # these paths are real; copied into the node's own store, so nixkube finds
  # them there. Naming them here is also what puts them in the build sandbox.
  seedRoots = [
    manifestFile
    probes.ro
    probes.rw
    pkgs.hello
  ];

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

      # nixkube's other mount path. The node DaemonSet asks containerd for
      # an NRI connection whether or not containerd is listening, and gets
      # no error when it is not -- so without this the plugin waits, the
      # pods that need it start without a /nix, and nothing says why.
      nri = true;
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
        extraRoots = map toString seedRoots;
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

    # The chaos scenarios compare what the driver left on the node against
    # what the node says is still alive, and both answers are JSON.
    environment.systemPackages = [ pkgs.jq ];

    /*
      Fill the node's store before kubelet can want it.

      The DaemonSet's init container fills `nixkube.hostMountPath` by
      substituting into it, and a build sandbox has no binary cache to
      substitute from. There does not have to be one: this guest's own
      /nix/store *is* the sandbox's, over hostfs, and holds every path the
      manifest names. It only has to be copied one directory across.

      A store-to-store copy on the same disk, so no HTTP, no signatures and
      no resolver. Serving it over nix-serve was tried first and is what a
      real node does; here it answered HTTP 500 to every narinfo, and
      debugging a cache server is not what this test is for.

      The other rejected option was to hand containers the node's whole
      /nix through containerd's base runtime spec. That works and is
      wrong: user-mode-nixos already mounts /nix/store into every
      container, and mounting /nix as well would leave this test unable to
      tell nixkube's own /nix from the harness's -- it would pass with the
      driver switched off.

      Before kubelet, so the init container finds the paths already valid
      and copies nothing. It overlaps `kubeadm init`, which takes longer.
    */
    systemd.services.nixkube-seed-store = {
      description = "Copy what nixkube needs into the node's own store";
      wantedBy = [ "multi-user.target" ];
      before = [ "kubelet.service" ];
      after = [ "uml-nix-db.service" ];
      requires = [ "uml-nix-db.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        mkdir -p ${hostMountPath}
        ${lib.getExe' pkgs.nix "nix"} copy \
          --no-check-sigs --to ${hostMountPath} ${lib.escapeShellArgs seedRoots}
      '';
    };
  };

  settings = {
    manifest = "${manifestFile}";
    # One pod using both mount paths, with a generated name, created after
    # the driver is up and again after each thing that breaks it. See
    # ./probe.nix.
    probeRo = "${probes.ro}";
    probeRw = "${probes.rw}";
    /*
      Which chaos scenarios to run, by substring, comma separated.

      Empty runs all of them, which is what CI wants and what takes twenty
      minutes. Iterating on one costs two minutes of cluster and one
      scenario:

          NIXKUBE_UML_SCENARIOS=containerd nix build --file . umlTest

      `builtins.getEnv` because this file is not a flake, so the evaluation
      is impure already, and a knob on the test's own entry point beats a
      loop somebody builds in a shell.
    */
    scenarios = builtins.getEnv "NIXKUBE_UML_SCENARIOS";

    # What to run inside the resident pod to ask whether it still has what
    # it was given. The first only resolves through the CSI volume; the
    # second prints the mount table the NRI plugin wrote. `/mnt/csi` is the
    # same literal ./workloads.nix and ./probe.nix use.
    residentHello = "/mnt/csi${lib.getExe pkgs.hello}";
    # From the resident's *own* closure, not the node's. The NRI mount is a
    # narrowed /nix holding what that container needs and nothing else --
    # which is the point of nixkube -- so a binary from anywhere else is not
    # in there. Measured: busybox was not, and the exec failed with "no such
    # file or directory" on a path the node certainly has.
    residentCat = "${lib.getExe' pkgs.coreutils "cat"}";

    # Where the driver's own state lands on the node -- see CSI_ROOT in
    # pkgs/nixkube/src/constants.py, which is a path inside the node
    # container, and this is what its /nix comes from.
    inherit hostMountPath;
    kubernetesVersion = pkgs.kubernetes.version;
    # What a workload will ask the CSI driver to mount. A store path this
    # file names is a path the sandbox has.
    workloadStorePath = "${pkgs.hello}";
    workloadImage = "uml.test/busybox:1";
  };
}
