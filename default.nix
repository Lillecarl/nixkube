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
              cachix push nixkube ${config.internal.manifestJSONFile}
            '';
          nixkube.pynixd.enable = false;
          # Keeping string context on the DaemonSet's store paths makes them
          # part of the manifest's closure. The NixOS test VM then has every
          # path in /nix/store, where nix-serve makes them available as a
          # substituter for nixkube's separate /var/lib/nix-csi store.
          nixkube.discardStringContext = false;
          nixkube.systems = {
            x86_64-linux = true;
            aarch64-linux = false;
          };
          # 10.113.37.1 is the PTP CNI gateway — the host-side veth IP
          # reachable from all pods. nix-serve runs there during the NixOS
          # test, and only there, which is why this is here rather than in
          # ./kubenix/ci beside the substituters every variant shares.
          nixkube.node.nixConfig.settings.substituters = [
            "https://nixkube.cachix.org"
            "https://cache.nixos.org"
            "http://10.113.37.1:5000?trusted=1"
          ];
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
              cachix push nixkube ${config.internal.manifestJSONFile}
            '';
          nixkube.pynixd.enable = true;
          nixkube.discardStringContext = false;
        }
      )
    ];
  };
  kubenixPush = kubenixInstance {
    module.config = {
      nixkube.discardStringContext = false;
      nixkube.systems = {
        ${builtins.currentSystem} = true;
      };
    };
  };
  kubenixPushBoth = kubenixInstance {
    module.config = {
      nixkube.discardStringContext = false;
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
        nix-store -qR ${kubenixPush.deploymentScript} | cachix push nixkube
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
        nix-store -qR "$OUT" | cachix push nixkube
      '';

  # Push the test workloads' store paths to cachix.
  #
  # The workloads are not all substitutable by accident. `env-ssl` runs a
  # `writeShellApplication` built on the runner, and the node discovers it
  # from the container's `command` because that volume carries no
  # `volumeAttributes`. Nothing published it, so the node asked for a path
  # that existed in one store on earth:
  #
  #   don't know how to build these paths:
  #     /nix/store/2imfy8cd7s3cxv4swmh3xx3izwchglbf-printer
  #
  # The manifest and not the deployment script, and it has to be the
  # manifest: `kubenixCITest` sets `nixkube.enable = false`, so the discard
  # transformer never runs and the manifest keeps the string context that
  # makes these paths part of its closure. 29 paths, `printer` among them.
  #
  # See issue #30.
  push-citest =
    pkgs.writeScriptBin "push-citest" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        set -euo pipefail
        # No 2>/dev/null, for the reason given on `push-ci2`.
        OUT=$(nix build --no-link --print-out-paths --file ${builtins.toString ./.} kubenixCITest.config.internal.manifestJSONFile)
        nix-store -qR "$OUT" | cachix push nixkube
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
        nix-store -qR ${kubenixPushBoth.deploymentScript} | cachix push nixkube
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

  # Does any private key reach the Nix store?
  #
  # /nix/store is world readable, and a manifest rendered here is a store
  # path. So a private key that reaches this render is readable by every user
  # on every machine that builds or substitutes it, and it stays readable --
  # a store path is immutable and lives until it is garbage collected.
  #
  # nixkube never generates a keypair at evaluation time. `kubenix/secret.nix`
  # runs `ssh-keygen` in the cluster, at deploy time, and hands the result to
  # `kubectl create secret`. What this repository renders is the script, the
  # secret's name, and a mount path. `keys/*.pub` are public halves and are
  # filtered by suffix.
  #
  # That is an invariant, not an observation, so it is checked rather than
  # documented. Adding a `Secret` with `data` to the module would be an
  # ordinary-looking change and this is what says no.
  noPrivateKeysInManifest =
    pkgs.runCommand "no-private-keys-in-manifest" { nativeBuildInputs = [ pkgs.jq ]; }
      ''
        manifest=${kubenixApply.manifestJSONFile}

        # A rendered Secret carrying material. `nixkube/discard` does not help
        # here: the key would be in the JSON itself, not in a store path it
        # names.
        carriers=$(jq -r '
          [ (.items // [.])[]
            | select(.kind == "Secret")
            | select((.data // .stringData) != null)
            | .metadata.name ] | join(", ")
        ' "$manifest")
        if [ -n "$carriers" ]; then
          echo "Secret objects rendered with inline material: $carriers" >&2
          echo "Generate them in the cluster instead. See kubenix/secret.nix." >&2
          exit 1
        fi

        # And the shape a key takes when it arrives some other way -- read from
        # a path with builtins.readFile, or pasted into an option.
        if grep -qE 'PRIVATE KEY|BEGIN OPENSSH' "$manifest"; then
          echo "The rendered manifest contains private key material." >&2
          echo "/nix/store is world readable, so this publishes it." >&2
          exit 1
        fi

        touch $out
      '';

  # Can the builder manager watch what it decides on?
  #
  # It watches CSINode to find nodes where the nixkube CSI driver is
  # registered, and patches the Node with the result. A missing grant does
  # not crash it: `_watch_nodes` catches, logs `node_watch_error` and sleeps
  # ten seconds, for ever. So the cluster looks healthy, no node is ever
  # probed, and the only evidence is one line in a log nobody reads.
  #
  # Against the rendered manifest, because the ClusterRole is what the
  # apiserver reads.
  probeWatchHasRbac = pkgs.runCommand "probe-watch-has-rbac" { nativeBuildInputs = [ pkgs.jq ]; } ''
    # `$rules` is bound before the map, because inside `map` the input is
    # the triple being tested and not the document.
    missing=$(jq -r '
      [ (.items // [.])[] | select(.kind == "ClusterRole") | .rules[] ] as $rules
      | def granted($group; $resource; $verb):
          [ $rules[]
            | select(.apiGroups | index($group))
            | select(.resources | index($resource))
            | select((.verbs | index($verb)) or (.verbs | index("*")))
          ] | length > 0;

        [ ["storage.k8s.io", "csinodes", "watch"],
          ["storage.k8s.io", "csinodes", "list"],
          ["", "nodes", "get"],
          ["", "nodes", "patch"]
        ]
        | map(select(granted(.[0]; .[1]; .[2]) | not) | join(" "))
        | join("; ")
    ' ${kubenixApply.manifestJSONFile})

    if [ -n "$missing" ]; then
      echo "The ClusterRole does not grant: $missing" >&2
      echo "Node probing fails silently without these. See kubenix/rbac.nix." >&2
      exit 1
    fi
    touch $out
  '';

  # Does a builder present the host key the controller pins?
  #
  # `ssh_known_hosts` names one ed25519 key for `*`, and asyncssh derives the
  # acceptable server-host-key algorithms from that entry. A builder with no
  # `PYNIXD_SSH_HOST_KEY` generates a fresh ssh-rsa key instead, the
  # intersection is empty, and the exchange dies before authentication:
  #
  #   asyncssh.misc.KeyExchangeFailed: Unable to find compatible server host key
  #
  # Measured on nixlab2: 82 builders registered, 82 ssh_connect_failed, none
  # ever reachable. The builders were healthy. Nothing else reports this --
  # the Job runs, the pod is Ready, and only the controller's own traceback
  # says why -- so it is asserted here.
  #
  # Against the rendered template and the rendered StatefulSet, and against
  # the volume rather than the string. A path that matches no mount would
  # pass a string comparison and fail on a cluster.
  builderPresentsPinnedHostKey =
    let
      res = kubenixApply.config.kubernetes.resources.nixkube;
      hostKeyOf =
        spec:
        let
          container = lib.head (lib.filter (c: c.name == "pynixd") spec.containers);
          env = lib.filter (e: e.name == "PYNIXD_SSH_HOST_KEY") (container.env or [ ]);
        in
        assert lib.assertMsg (env != [ ]) "pynixd container has no PYNIXD_SSH_HOST_KEY";
        rec {
          path = (lib.head env).value;
          # `mountPath + "/"`, because these are path prefixes and not string
          # prefixes. The controller mounts ssh-config at /etc/ssh, which is a
          # string prefix of /etc/ssh-key/id_ed25519 and not a path one, and
          # sorts first -- so a plain hasPrefix picks the ConfigMap and asks
          # it for a secretName it does not have.
          matches = lib.filter (m: lib.hasPrefix (m.mountPath + "/") path) (container.volumeMounts or [ ]);
          mount =
            assert lib.assertMsg (
              lib.length matches == 1
            ) "PYNIXD_SSH_HOST_KEY ${path} is under ${toString (lib.length matches)} mounts";
            lib.head matches;
          secret = (lib.head (lib.filter (v: v.name == mount.name) spec.volumes)).secret.secretName;
        };
      builder = hostKeyOf res.PodTemplate.nixkube-builder.template.spec;
      controller = hostKeyOf res.StatefulSet.pynixd.spec.template.spec;
    in
    # Both sides of the connection have to name one keypair, or the pin the
    # controller carries cannot match what the builder offers.
    assert builder.secret == controller.secret;
    # ed25519, because the known_hosts entry is. A key of another type is the
    # defect this check exists for.
    assert lib.hasInfix "ed25519" builder.path;
    pkgs.runCommand "builder-presents-pinned-host-key" { } "echo ok > $out";

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

  /*
    The kind jobs, on a guest instead of on a container.

    `umlTest` breaks a node nine ways to see whether the driver comes back.
    This one asks what the kind jobs ask: that `kubenixDeploy` works, that
    the DaemonSet rolls out, that the test workloads complete, and that
    deleting them cleans up -- on a node that pulls its own images and has
    had nothing done to it.

    Not a check and not in CI's sandbox: the node pulls from
    registry.k8s.io and what it deploys comes from ghcr.io.

        nix run --file . ciTest.run          # no pynixd, kubenixCI2
  */
  ciTest = pkgs.callPackage ./nix/uml/ci.nix {
    inherit sources;
    name = "nixkube-ci";
    instance = kubenixCI2;
    workloads = kubenixCITest;
    deployedJobs = (import ./ci/test-jobs.nix).deployed;
    assertedJobs = (import ./ci/test-jobs.nix).asserted;
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

  # Every gate CI builds, as one attribute.
  #
  # `nix build --file ./checks.nix all` is the answer to "does this pass
  # CI", and before this it was "read ci.yaml and copy six commands". A gate
  # that is not in here is a gate nobody runs by hand, so adding one to the
  # workflow and not to this set is the mistake to avoid -- which is why the
  # `check` job builds this attribute rather than listing them again.
  #
  # It holds exactly what the workflow held, so this is a refactor and not a
  # widening. Two things stay outside it, both because they need something a
  # derivation does not have: pyright wants the dev shell, and treefmt is
  # checked with `git diff` against a working tree.
  #
  # `umlImagesMatch` is the one member that builds rather than evaluates: it
  # writes two image tarballs, so on a cold store it also fetches the nix
  # image closure. That is the whole cost of this set.
  #
  #   this step, without it   69s    check job  3m25s
  #   this step, with it     168s    check job  5m13s
  #
  # Measured on runs 34856944613 and 34857619341, cold runners both. So it
  # costs about 100s against a 30 minute bound, and it earns that: the
  # failure it catches -- a tarball that does not carry the tag the DaemonSet
  # asks for -- otherwise appears as ErrImagePull, on a cluster, twenty
  # minutes after the change that caused it.
  #
  # If it ever stops being worth 100s, move it to `build-amd64`, which builds
  # the same image and so already holds the closure. Dropping it is the wrong
  # answer; nothing else asks this question.
  checks = {
    inherit
      assertionsNullShape
      assertionsNeighbourScope
      builderSettingsOverride
      nodeDriverReadiness
      sourcesAreLocked
      ciWorkflowCheck
      builderPresentsPinnedHostKey
      noPrivateKeysInManifest
      probeWatchHasRbac
      umlImagesMatch
      ;
    # The Python suite, which runs in this derivation's checkPhase.
    #
    # It was missing, and that cost a red CI: a test asserting the probe Job
    # carries `nodeName` failed after the probe moved to a nodeAffinity, and
    # `checks.all` passed locally the whole time because it never ran pytest.
    # The suite only ran inside `build-amd64`, so the feedback came after a
    # full environment build rather than in seconds.
    pynixd-nixkube-tests = pkgs.pynixd-nixkube;
    all = pkgs.runCommand "nixkube-checks" {
      checks = [
        assertionsNullShape
        assertionsNeighbourScope
        builderSettingsOverride
        nodeDriverReadiness
        sourcesAreLocked
        ciWorkflowCheck
        builderPresentsPinnedHostKey
        noPrivateKeysInManifest
        probeWatchHasRbac
        umlImagesMatch
        pkgs.pynixd-nixkube
      ];
    } "printf '%s\\n' $checks > $out";
  };

  # Every GitHub Actions workflow as a value, beside the file it renders to.
  # ci/workflows/*.nix hold them and say why.
  ciWorkflows =
    let
      ghalib = import sources.ghanix { inherit lib; };
      workflow = module: committed: {
        inherit committed;
        value = import module { inherit lib ghalib; };
      };
    in
    {
      ci = workflow ./ci/workflows/ci.nix ./.github/workflows/ci.yaml;
      test-nixos = workflow ./ci/workflows/test-nixos.nix ./.github/workflows/test-nixos.yaml;
    };

  # Those values as the files GitHub reads.
  #
  # `pkgs.formats.yaml` emits a `%YAML 1.1` directive and a `---` marker that
  # no workflow carries, so the first two lines go. It quotes `'on'` for the
  # same YAML 1.1 reason that makes the directive appear, which is what a
  # workflow needs: unquoted, `on` is the boolean `true` to a 1.1 parser.
  #
  # Then yamlfmt, because treefmt runs yamlfmt over this file and the `check`
  # job ends in `git diff --exit-code`. A render in any other style is a
  # failing job on every run. Running the same formatter here makes the two
  # agree by construction rather than by taste.
  #
  # Key order is alphabetical and nothing depends on it: GitHub does not, and
  # ciWorkflowCheck compares parsed documents rather than text.
  ciWorkflowFiles = lib.mapAttrs (
    name: wf:
    let
      header = ''
        # SPDX-License-Identifier: MIT
        # GENERATED FILE -- do not edit by hand.
        # Edit ci/workflows/${name}.nix, then run: nix run --file . ci-workflow-update
      '';
    in
    pkgs.runCommand "${name}.yaml" { nativeBuildInputs = [ pkgs.yamlfmt ]; } ''
      {
        printf '%s\n' ${lib.escapeShellArg header}
        tail --lines=+3 ${(pkgs.formats.yaml { }).generate "${name}.yaml" wf.value}
      } > out.yaml
      HOME=$PWD yamlfmt out.yaml
      mv out.yaml $out
    ''
  ) ciWorkflows;

  # Does each committed workflow still say what its ci/workflows/*.nix says?
  #
  # Parsed, not compared as text, so key order and quoting cannot fail this.
  # yq reads YAML 1.2, where a bare `on:` key is the string "on". A YAML 1.1
  # parser answers the boolean `true` for it, which would compare a boolean
  # key against a string one and fail every run.
  #
  # It renders with the ghanix the umbrella locks, not a working copy. So a
  # ghanix change reaches this check only after the umbrella lock moves, and
  # UMBRELLA_DEV=ghanix can pass here while a runner fails. That is the same
  # asymmetry as issue #28 and not a separate bug.
  ciWorkflowCheck =
    pkgs.runCommand "ci-workflow-check"
      {
        nativeBuildInputs = [
          pkgs.yq-go
          pkgs.jq
        ];
      }
      (
        lib.concatStrings (
          lib.mapAttrsToList (name: wf: ''
            yq --output-format=json '.' ${wf.committed} | jq --sort-keys . > committed.json
            jq --sort-keys . < ${pkgs.writeText "${name}-rendered.json" (builtins.toJSON wf.value)} > rendered.json
            if ! diff --unified committed.json rendered.json; then
              echo >&2
              echo ".github/workflows/${name}.yaml and ci/workflows/${name}.nix disagree." >&2
              echo "Run: nix run --file . ci-workflow-update" >&2
              exit 1
            fi
          '') ciWorkflows
        )
        + "touch $out\n"
      );

  ci-workflow-update = pkgs.writeScriptBin "ci-workflow-update" ''
    #! ${pkgs.runtimeShell}
    set -euo pipefail
    ${lib.concatStrings (
      lib.mapAttrsToList (
        name: file: "cp --no-preserve=mode ${file} .github/workflows/${name}.yaml\n"
      ) ciWorkflowFiles
    )}
  '';
}
