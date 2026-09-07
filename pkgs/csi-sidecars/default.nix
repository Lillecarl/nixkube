# SPDX-License-Identifier: MIT

# The two kubernetes-csi sidecars the node DaemonSet runs beside the driver,
# built from source rather than pulled from registry.k8s.io.
#
# The DaemonSet names them by image, and a registry is a network. That is fine
# on a real cluster and impossible in a Nix build sandbox, which is where the
# User-Mode Linux test runs -- so the test cannot pull them and there is no
# other way to put them on a node. Everything else the manifest names is
# already buildable here: `nixImage` and `scratchImage` in the repository root.
#
# The versions match `kubenix/daemonset.nix`. A bump there needs one here, and
# the image tags below are what ties the two together.
{
  lib,
  buildGoModule,
  fetchFromGitHub,
  dockerTools,
}:
let
  registry = "registry.k8s.io/sig-storage";

  # `ldflags` follows each project's own release build: the version string is
  # linked in rather than read from a file, and a sidecar that reports
  # "unknown" is one nobody can tell apart from the released one in a log.
  sidecar =
    {
      pname,
      version,
      owner ? "kubernetes-csi",
      repo,
      hash,
      vendorHash,
      versionPackage,
      description,
    }:
    buildGoModule {
      inherit pname version vendorHash;

      # Both repositories vendor `release-tools`, a helper module with a `main`
      # of its own. Without this it lands in `bin/` beside the sidecar and the
      # image carries a second entry point nobody asked for.
      subPackages = [ "cmd/${pname}" ];

      src = fetchFromGitHub {
        inherit owner repo hash;
        rev = "v${version}";
      };

      ldflags = [
        "-s"
        "-w"
        "-X ${versionPackage}.version=v${version}"
      ];

      # The upstream suites want a running kubelet plugin socket.
      doCheck = false;

      meta = {
        inherit description;
        homepage = "https://github.com/${owner}/${repo}";
        license = lib.licenses.asl20;
        platforms = lib.platforms.linux;
        mainProgram = pname;
      };
    };

  # An image is a binary and nothing else. The node these run on has the whole
  # host store visible, so a layer carrying a copy of glibc would be asking
  # containerd to unpack something the node can already see.
  image =
    {
      name,
      tag,
      package,
    }:
    dockerTools.buildImage {
      inherit tag;
      name = "${registry}/${name}";
      copyToRoot = [ package ];
      config.Entrypoint = [ (lib.getExe package) ];
    };
in
rec {
  csi-node-driver-registrar = sidecar {
    pname = "csi-node-driver-registrar";
    version = "2.16.0";
    repo = "node-driver-registrar";
    hash = "sha256-zpWu8w7kJVrKPHnGgd0L9XN4btL9I3oTWbK+b3S1bNs=";
    # Both projects commit their `vendor/` tree, so there is no module fetch
    # here at all -- the source archive is the whole input.
    vendorHash = null;
    versionPackage = "main";
    description = "Registers a CSI driver with the kubelet";
  };

  livenessprobe = sidecar {
    pname = "livenessprobe";
    version = "2.18.0";
    repo = "livenessprobe";
    hash = "sha256-87nnIQRTeT7goHVK77FR2nnSp7wsggNq3Q7A1zQkr8Y=";
    vendorHash = null;
    versionPackage = "main";
    description = "Monitors the health of a CSI driver and reports it to kubelet";
  };

  images = {
    csi-node-driver-registrar = image {
      name = "csi-node-driver-registrar";
      tag = "v${csi-node-driver-registrar.version}";
      package = csi-node-driver-registrar;
    };
    livenessprobe = image {
      name = "livenessprobe";
      tag = "v${livenessprobe.version}";
      package = livenessprobe;
    };
  };
}
