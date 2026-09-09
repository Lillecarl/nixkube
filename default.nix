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
        nix-store -qR --include-outputs $(nix-store -qd ${kubenixPush.deploymentScript}) | grep -v '\.drv$' | cachix push nix-csi
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
        DRV=$(nix-store -qd $(nix build --no-link --print-out-paths --file ${builtins.toString ./.} kubenixCI2.deploymentScript))
        nix-store -qR --include-outputs "$DRV" | grep -v '\.drv$' | cachix push nix-csi
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
        nix-store -qR --include-outputs $(nix-store -qd ${kubenixPushBoth.deploymentScript}) | grep -v '\.drv$' | cachix push nix-csi
      '';

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
    pkgs.runCommand "builder-settings-override" { } "echo ok > $out";

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
}
