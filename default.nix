# SPDX-License-Identifier: MIT

{
  # Where every dependency lives, as directories. nix/sources.nix says how
  # this repository finds the umbrella that owns them.
  sources ? import ./nix/sources.nix,
  system ? builtins.currentSystem,
}:
rec {
  pkgs = import sources.nixpkgs {
    inherit system;
    config = {
      allowUnfree = true;
    };
    overlays = [ (import ./pkgs) ];
  };
  inherit sources;
  lib = pkgs.lib;

  easykubenix = import sources.easykubenix;

  kubenixApply = kubenixInstance { };
  kubenixCI1 = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      {
        nixkube.systems = {
          x86_64-linux = true;
          aarch64-linux = false;
        };
      }
    ];
  };
  # kubenixCI2 is used by tests/nixos/integration.nix for the containerd nixos test.
  # Disables aarch64-linux to avoid needing cross-compilation support.
  kubenixCI2 = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      (
        { config, pkgs, ... }:
        {
          kluctl.preDeployScript = # bash
            ''
              export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
              cachix push nix-csi ${config.internal.manifestJSONFile}
            '';
          nixkube.pynixd.enable = false;
          # push = true retains Nix string context on DaemonSet store paths so
          # they become part of the manifest's closure.  The NixOS test VM then
          # has every path in /nix/store, where nix-serve makes them available
          # as a substituter for nixkube's separate /var/lib/nix-csi store.
          nixkube.push = true;
          nixkube.systems = {
            x86_64-linux = true;
            aarch64-linux = false;
          };
          # 10.113.37.1 is the PTP CNI gateway — the host-side veth IP reachable
          # from all pods.  nix-serve runs there during the NixOS test.
        }
      )
    ];
  };
  # Separate instance for test workload Jobs, deployed after infrastructure
  # is fully rolled out so CSI and NRI are ready before pods start.
  kubenixCITest = easykubenix {
    inherit pkgs;
    modules = [
      ./kubenix
      ./kubenix/ci/test-workloads.nix
      {
        _module.args.sources = sources;
      }
      {
        ekn.environment = "nixkube-test";
        nixkube.enable = false;
      }
    ];
  };

  kubenixLocal = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      (
        { config, ... }:
        {
          kluctl.preDeployScript = # bash
            ''
              expected_context="kind"
              current_context=$(kubectl config current-context)

              if [[ "$current_context" != *"$expected_context" ]]; then
                  echo "Warning: Current context is $current_context, not *$expected_context"* >&2
                  read -rp "Continue anyway? [y/N] " confirm
                  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                      echo "Aborted." >&2
                      exit 1
                  fi
              fi
              cachix push nix-csi ${config.internal.manifestJSONFile}
            '';
          nixkube.pynixd.enable = true;
          nixkube.push = true;
        }
      )
    ];
  };
  kubenixPush = kubenixInstance {
    module.config = {
      nixkube.push = true;
      nixkube.systems = {
        ${builtins.currentSystem} = true;
      };
    };
  };
  kubenixPushBoth = kubenixInstance {
    module.config = {
      nixkube.push = true;
      nixkube.systems = {
        x86_64-linux = true;
        aarch64-linux = true;
      };
    };
  };
  kubenixInstance =
    {
      module ? { },
    }:
    easykubenix {
      inherit pkgs;
      modules = [
        module
        ./kubenix
        {
          _module.args.sources = sources;
        }
        {
          config = {
            # Disabled by default so you can include the module in an easykubenix project
            nixkube.enable = true;
            # Allow easily adding your pubkeys to the cache
            nixkube.pynixd.authorizedKeys = lib.pipe (lib.filesystem.listFilesRecursive ./keys) [
              (lib.filter (name: lib.hasSuffix ".pub" name))
              (lib.map (name: builtins.readFile name))
              (lib.map (key: lib.trim key))
            ];
          };
        }
      ];
    };

  push =
    pkgs.writeScriptBin "push" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        # ${lib.concatStrings (lib.attrValues sources)}
        #
        # The runtime closure, and not the derivation closure with its outputs.
        #
        # `-qR --include-outputs` on the .drv uploads every build-time
        # dependency's output as well: compilers, sources, intermediate
        # results. A node needs none of them. That was there to speed up
        # rebuilds while nixkube carried a patched Nix, and nixkube no longer
        # overrides Nix at all, so the reason has gone.
        #
        # Measured on the aarch64 side, which is where the cost showed:
        #
        #   derivation closure   1270 paths   1197 absent from cachix   4.91 GiB
        #   runtime closure       328 paths    298 absent               1.37 GiB
        #
        # cachix compresses each path with zstd on the runner. Five gigabytes
        # of that on a 4-core arm runner is what "the hosted runner lost
        # communication with the server ... starves it for CPU/Memory" looks
        # like, and build-arm64 died that way twice. amd64 never did, because
        # its build-time outputs went to cachix over months of runs and were
        # skipped. See issue #21.
        nix-store -qR ${kubenixPush.deploymentScript} | cachix push nix-csi
      '';

  # Push kubenixCI2 (nocache variant) store paths to cachix.
  # Run separately since CI2 is x86_64-only (cannot be in the main push script
  # which also runs on ARM runners).
  push-ci2 =
    pkgs.writeScriptBin "push-ci2" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        set -euo pipefail
        # No 2>/dev/null on either command. Both wrote the reason a push failed
        # to stderr, and both threw it away. See issue #12.
        # The runtime closure, for the reason given on `push` above.
        OUT=$(nix build --no-link --print-out-paths --file ${builtins.toString ./.} kubenixCI2.deploymentScript)
        nix-store -qR "$OUT" | cachix push nix-csi
      '';

  # Push environments for both x86_64-linux and aarch64-linux to cachix.
  #
  # Needs a machine that can build both, so it is a hand-run script and not a
  # CI step: CI builds each architecture on a runner of that architecture
  # instead (`build-amd64`, `build-arm64`), which is what `push` above is for.
  # Point it at your own ssh builder if you need it.
  push-env =
    pkgs.writeScriptBin "push-env" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        # ${lib.concatStrings (lib.attrValues sources)}
        # The runtime closure, for the reason given on `push` above.
        nix-store -qR ${kubenixPushBoth.deploymentScript} | cachix push nix-csi
      '';

  # Publish both `nix:` images and the multi-arch manifest, by hand.
  #
  # `build-arm64` cannot do this. It stalls part-way through substituting from
  # cachix, on a warm cache and a clean runner, and issue #21 has the
  # elimination list. Until that is understood, this is how a `nix:` tag gets
  # published, and it takes about two minutes.
  #
  # Needs a machine that can build both architectures, exactly like `push-env`.
  # Point Nix at a builder for the one it cannot do itself:
  #
  #   nix run --file . push-images --builders "@$HOME/tmp/builders"
  #
  # REPO_USERNAME and REPO_TOKEN must be set, and the token needs
  # `write:packages`. A `gh` token does not have that by default:
  #
  #   gh auth refresh -s write:packages
  #   REPO_USERNAME=<user> REPO_TOKEN="$(gh auth token)" nix run --file . push-images
  #
  # The manifest step needs both per-architecture tags to exist already, which
  # is why they are pushed first and in order.
  push-images = pkgs.writeShellApplication {
    name = "push-images";
    text = ''
      : "''${REPO_USERNAME:?set REPO_USERNAME}"
      : "''${REPO_TOKEN:?set REPO_TOKEN, with write:packages}"
      ${lib.getExe' nixImage.pushArch.x86_64-linux "push-nix-x86_64-linux"}
      ${lib.getExe' nixImage.pushArch.aarch64-linux "push-nix-aarch64-linux"}
      ${lib.getExe' nixImage.pushManifest "push-nix-manifest"}
    '';
  };

  uploadScratch =
    let
      scratchVersion = "1.0.1";
      scratchUrl = system: "ghcr.io/lillecarl/nix-csi/scratch:${scratchVersion}-${system}";
      scratchManifest = "ghcr.io/lillecarl/nix-csi/scratch:${scratchVersion}";
    in
    pkgs.writeScriptBin "uploadScratch" # bash
      ''
        #! ${pkgs.runtimeShell}
        set -euo pipefail
        set -x
        export PATH=${lib.makeBinPath [ pkgs.buildah ]}:$PATH
        # Build and publish scratch image(s)
        buildah login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ghcr.io
        container=$(buildah from --platform linux/amd64 scratch)
        buildah config --env "PATH=/nix/var/result/bin" $container
        buildah commit $container ${scratchUrl "x86_64-linux"}
        buildah push ${scratchUrl "x86_64-linux"}
        container=$(buildah from --platform linux/arm64 scratch)
        buildah config --env "PATH=/nix/var/result/bin" $container
        buildah commit $container ${scratchUrl "aarch64-linux"}
        buildah push ${scratchUrl "aarch64-linux"}
        buildah manifest rm ${scratchManifest} &>/dev/null || true
        buildah manifest create ${scratchManifest}
        buildah manifest add ${scratchManifest} ${scratchUrl "x86_64-linux"}
        buildah manifest add ${scratchManifest} ${scratchUrl "aarch64-linux"}
        buildah manifest push ${scratchManifest}
      '';
  genModDoc =
    let
      optionsDocs = pkgs.nixosOptionsDoc {
        inherit (kubenixCI1.eval) options;
        warningsAreErrors = false;
        transformOptions =
          opt:
          opt
          // {
            # Remove internal options, modify declarations, etc.
            visible =
              opt.visible or true && (lib.hasPrefix "nix-csi" opt.name || lib.hasPrefix "nixkube" opt.name);
            declarations = map (
              decl:
              let
                prefix = builtins.toString ./.;
              in
              if lib.hasPrefix prefix (toString decl) then lib.removePrefix prefix (toString decl) else decl
            ) opt.declarations;
          };
      };
    in
    pkgs.writeScriptBin "genModDoc" # bash
      ''
        #! ${pkgs.runtimeShell}
        cp --no-preserve=mode ${optionsDocs.optionsCommonMark} $GIT_ROOT/doc/options.md
      '';

  # Evaluate the hostPath+subPath assertion against a container that has no
  # volumeMounts at all.
  #
  # easykubenix renders through typed submodules whose optional fields default
  # to null, so such a container has `volumeMounts = null`, not a missing
  # attribute. `x or [ ]` does not catch that, and the assertion crashed on a
  # consumer tree with "expected a list but found null".
  #
  # Every container nixkube declares has volumeMounts, so none of the six
  # instances above can produce the shape. This attribute does, on purpose.
  assertionsNullShape =
    let
      instance = kubenixInstance {
        module.imports = [
          (
            { lib, ... }:
            {
              kubernetes.resources.nixkube.Deployment.nullprobe = {
                # Labelled as ours, so this exercises the assertion rather
                # than the warning for a neighbour's object.
                metadata.labels."app.kubernetes.io/part-of" = "nixkube";
                spec.template.spec.containers = lib.mkNamedList {
                  backend = {
                    image = "example/backend";
                    # Present and null, which is the shape that broke the
                    # assertion. Leaving it out gives a *missing* attribute
                    # instead, and `x or [ ]` handles that one -- so a probe
                    # that omits it passes whether the code is fixed or not.
                    #
                    # This attribute made exactly that mistake, and the test
                    # that convinced me otherwise was `c.volumeMounts or null
                    # == null`, which answers null for a missing attribute too.
                    volumeMounts = null;
                  };
                };
              };
            }
          )
        ];
      };
      # Evaluate the assertions, and do not build the manifest.
      #
      # This read `test -s ${"$"}{instance.manifestYAMLFile}`, which realises the
      # whole closure. That failed the check job on a runner with a cold store,
      # for a test whose entire subject is an evaluation. Reading
      # config.assertions forces every condition, including the walk over
      # kubernetes.resources that this attribute exists to exercise, and
      # nothing is built.
      #
      # The output closure of this derivation was empty either way, which is
      # what I measured and why I got it wrong. The cost was in the inputs.
      failed = lib.filter (entry: !entry.assertion) instance.config.assertions;
    in
    assert failed == [ ];
    pkgs.runCommand "assertions-null-shape" { } "echo ok > $out";

  # A neighbour's object is reported and not refused.
  #
  # config.kubernetes.resources holds every object of the whole instance, not
  # nixkube's, so an assertion that walks the lot lets a nixkube module veto a
  # consumer's unrelated chart. That happened: the empty-string check refused
  # to render a 448-object tree over two env vars in Rook's ceph-csi
  # controller, where `WATCH_NAMESPACE: ""` is the ordinary idiom for "all
  # namespaces". Nothing of nixkube's was wrong.
  #
  # This renders exactly that shape -- an empty env var on an object without
  # nixkube's part-of label -- and must evaluate. The warning still names it.
  assertionsNeighbourScope =
    let
      instance = kubenixInstance {
        module.imports = [
          (
            { lib, ... }:
            {
              kubernetes.resources.nixkube.Deployment.neighbour = {
                spec.template.spec.containers = lib.mkNamedList {
                  manager = {
                    image = "example/rook";
                    env = lib.mkNamedList {
                      WATCH_NAMESPACE.value = "";
                    };
                  };
                };
              };
            }
          )
        ];
      };
      failed = lib.filter (entry: !entry.assertion) instance.config.assertions;
    in
    assert failed == [ ];
    pkgs.runCommand "assertions-neighbour-scope" { } "echo ok > $out";

  # An operator can cap or disable builders through nixkube.pynixd.settings.
  #
  # Both the built-in defaults and the shared settings were wrapped in
  # mkDefault, so two definitions of builder-max met at the same priority and
  # the evaluation failed with a conflict rather than one winning. mkForce did
  # not help, because mapAttrsRecursive re-wrapped it. The option that looks
  # like the way to cap builders could not set the three keys that have
  # defaults, which is every key anyone would want to change.
  builderSettingsOverride =
    let
      capped = kubenixInstance {
        module.config.nixkube.pynixd.settings = {
          builder-min = 0;
          builder-max = 0;
          builder-startup-timeout = 120;
          builder-backoff-cap = 60;
        };
      };
      stock = kubenixInstance { };
      got = capped.config.nixkube.pynixd.controller.settings;
      base = stock.config.nixkube.pynixd.controller.settings;

      # The value the program actually reads.
      #
      # Asserting the option alone is not enough, and this check made that
      # mistake first: the option merged correctly while the StatefulSet still
      # carried a hardcoded PYNIXD_BUILDER_MAX of "3". NixkubeCentralSettings
      # reads these from the environment only, so the literal won and capping
      # builders did nothing.
      envOf =
        instance: name:
        let
          pod = instance.config.kubernetes.resources.nixkube.StatefulSet.pynixd.spec.template.spec;
          container = lib.head (lib.filter (c: c.name == "pynixd") pod.containers);
        in
        (lib.head (lib.filter (e: e.name == name) container.env)).value;
    in
    assert got.builder-min == 0 && got.builder-max == 0;
    # The defaults still apply where the operator said nothing.
    assert got.idle-timeout == 300;
    assert base.builder-min == 1 && base.builder-max == 3;
    assert envOf capped "PYNIXD_BUILDER_MAX" == "0";
    assert envOf capped "PYNIXD_BUILDER_MIN" == "0";
    assert envOf stock "PYNIXD_BUILDER_MAX" == "3";
    assert envOf stock "PYNIXD_BUILDER_MIN" == "1";
    # The startup watchdog and the failure backoff read the environment the
    # same way, so they need the same assertion.
    assert envOf capped "PYNIXD_BUILDER_STARTUP_TIMEOUT" == "120";
    assert envOf capped "PYNIXD_BUILDER_BACKOFF_CAP" == "60";
    assert envOf stock "PYNIXD_BUILDER_STARTUP_TIMEOUT" == "600";
    assert envOf stock "PYNIXD_BUILDER_BACKOFF_CAP" == "600";
    # activeDeadlineSeconds is deliberately absent from the builder Job. It
    # measures a Job's whole life, and a builder Job runs for hours, so any
    # value that bounds a Pod hung before startup kills working builders
    # mid-build. The bound is `builder-startup-timeout`, in the manager.
    assert
      !(builtins.hasAttr "activeDeadlineSeconds" (
        stock.config.kubernetes.resources.nixkube.PodTemplate.nixkube-builder.template.spec
      ));
    pkgs.runCommand "builder-settings-override" { } "echo ok > $out";

  # Every source is a fetched tree, not a directory on somebody's disk.
  #
  # A source read as a directory is a different input from the tree CI
  # fetches, so the same commit builds different packages in the two places.
  # Nothing says so at the time: the build succeeds, and a node later asks its
  # substituters for a store path that was never built anywhere.
  #
  # Three separate places had this. The umbrella read working copies as
  # directories, this overlay resolved pynixd from a sibling directory or an
  # unpinned branch, and Python's caches reached the package sources. All
  # three produced one symptom, and the first two are invisible to any check
  # that only looks at one checkout.
  #
  # `nixkube` is the exception and has to be. It is this repository, passed to
  # the umbrella as `overrides.nixkube`, so it is the tree being built.
  #
  # UMBRELLA_DEV makes a source a directory on purpose, and this check fails
  # then. That is the point rather than a limitation: a tree built that way is
  # not the tree CI builds, and this is the thing that says so out loud.
  sourcesAreLocked =
    let
      loose = lib.filterAttrs (
        name: value: name != "nixkube" && !(lib.hasPrefix builtins.storeDir (toString value))
      ) (import ./nix/sources.nix);
      names = lib.attrNames loose;
      asked = builtins.getEnv "UMBRELLA_DEV" != "";
    in
    assert lib.assertMsg (names == [ ]) ''
      These sources are directories rather than fetched trees:

      ${lib.concatMapStringsSep "\n" (n: "  ${n} -> ${toString loose.${n}}") names}

      A build here and a build in CI disagree when that happens, and the
      difference shows up on a cluster rather than here: a node asks its
      substituters for a store path that was never built anywhere.

      ${
        if asked then
          "UMBRELLA_DEV is set, so you asked for this. Unset it to build what CI builds."
        else
          "Nothing asked for this, so it is a bug. A source resolved to a path instead of a revision."
      }
    '';
    pkgs.runCommand "sources-are-locked" { } "echo ok > $out";

  # nix-node reports whether its driver answers.
  #
  # The container had a livenessProbe and no readinessProbe. The liveness
  # path works -- restartCount climbed 3 -> 6 on nixlab2 while the driver was
  # dead -- but a container with no readinessProbe is Ready as soon as it
  # runs. So `kubectl get pods` said ready=true for six minutes of a node
  # that could not mount anything, and builders kept being sent to it.
  #
  # Asserted against the rendered container, not the option, and against the
  # sidecar that serves the port. A probe pointing at a port nothing serves
  # would pass an option-level check and fail on a cluster.
  nodeDriverReadiness =
    let
      instance = kubenixInstance { };
      podSpec = instance.config.kubernetes.resources.nixkube.DaemonSet.nix-node.spec.template.spec;
      containerNamed = name: lib.head (lib.filter (c: c.name == name) podSpec.containers);
      node = containerNamed "nix-node";
      sidecar = containerNamed "livenessprobe-nixkube";
      port = node.readinessProbe.httpGet.port;
    in
    assert node.readinessProbe.httpGet.path == "/healthz";
    assert node.livenessProbe.httpGet.port == port;
    # Something has to answer on that port.
    assert lib.elem "--health-port=${toString port}" sidecar.args;
    # NotReady has to be reported before the restart, or it says nothing new.
    assert
      node.readinessProbe.failureThreshold * node.readinessProbe.periodSeconds
      < node.livenessProbe.failureThreshold * node.livenessProbe.periodSeconds;
    pkgs.runCommand "node-driver-readiness" { } "echo ok > $out";

  # NixOS integration tests — spin up real kubeadm clusters in VMs
  nixosTests = {
    containerd = import ./tests/nixos/integration.nix {
      inherit pkgs lib;
      manifests = kubenixCI2.manifestYAMLFile;
    };
  };

  treefmt = (import sources.treefmt-nix).mkWrapper pkgs {
    projectRootFile = "default.nix";
    programs.fish_indent.enable = true;
    programs.isort.enable = true;
    programs.nixfmt.enable = true;
    programs.ruff-check.enable = true;
    programs.ruff-format.enable = true;
    programs.shellcheck.enable = true;
    programs.typos.enable = true;
    programs.yamlfmt.enable = true;
  };

  nixkube-docs = pkgs.python3Packages.callPackage ./nix/docs.nix { };

  nixImage = pkgs.callPackage ./niximage.nix { };
  scratchImage = pkgs.callPackage ./scratchimage.nix { };

  # Every image the node DaemonSet names, as a tarball a UML guest can import.
  # There is no registry in a Nix build sandbox. See nix/uml/images.nix.
  umlImages = pkgs.callPackage ./nix/uml/images.nix { inherit nixImage scratchImage; };

  # Do those tarballs carry the tags the DaemonSet asks for? Seconds, against
  # twenty minutes of cluster before an ErrImagePull says the same thing.
  umlImagesMatch = umlImages.check umlManifest.manifestJSONFile;

  # nixkube on a real node, under User-Mode Linux, in a Nix build sandbox --
  # so no KVM, no root, and a derivation that passes or fails. See nix/uml.
  umlManifest = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      ./nix/uml/manifest.nix
      ./nix/uml/workloads.nix
    ];
  };

  umlTest = pkgs.callPackage ./nix/uml {
    inherit sources umlImages;
    manifest = umlManifest;
  };
  ci-debug = pkgs.callPackage ./pkgs/ci-debug { };

  # Is every store path this manifest names actually fetchable?
  #
  # A nixkube CSI volume names an output path, so a node substitutes it and
  # can never build it. A path on no substituter is a mount that fails, and
  # the failure surfaces as `MountVolume.SetUp failed ... Failed to build
  # store path`, on a node, well away from whatever forgot to push it.
  #
  # Ask before deploying, not after. The substituter list comes from the
  # module, so this asks about the caches a node really carries.
  assert-cached = pkgs.callPackage ./pkgs/assert-cached {
    substituters = kubenixApply.config.nixkube.nixConfig.settings.substituters;
  };

  # Does this attribute build on the machine that is asked to build it?
  arch-audit = pkgs.callPackage ./pkgs/arch-audit { };
}
