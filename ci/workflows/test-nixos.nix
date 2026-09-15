# The NixOS integration test workflow, as a value. See ci/workflows/ci.nix
# for how these are rendered and checked.
#
# It runs on `cidev` only. `ci.yaml` excludes that branch, so a push there
# runs this and nothing else.
{ lib, ghalib, ... }:
let
  inherit (import ./bootstrap.nix { inherit lib; }) bootstrap;
in
ghalib.evalWorkflow {
  on = {
    push.branches = [ "cidev" ];
    workflow_dispatch = null;
  };

  # UMBRELLA_GIT makes the umbrella fetch each source over the git
  # protocol, and not through api.github.com. Anonymous api.github.com
  # allows 60 calls an hour per IP, GitHub's runners share a NAT pool, and
  # every source a job resolves is one call. nanopynix issue #301.
  env = {
    UMBRELLA_GIT = "1";
    REPO_USERNAME = "\${{ github.actor }}";
    REPO_TOKEN = "\${{ secrets.GITHUB_TOKEN }}";
  };

  permissions.contents = "read";

  jobs.test-nixos = {
    runs-on = "ubuntu-latest";
    continue-on-error = true;
    # `continue-on-error` does not bound anything. Without this the job still
    # holds a runner for GitHub's default 360 minutes before it gives up, and
    # a nixos test that hangs in a VM is exactly the shape that does.
    timeout-minutes = 60;

    # The shared bootstrap, and the one setting only this workflow needs.
    # They merge rather than replace, so the substituters and the public
    # keys are the ones ci.nix uses and cannot drift from them -- which is
    # what issue #33 was about, when this step wrote them out again.
    #
    # No `access-tokens` here: ghanix's installer sets one by default now,
    # and this workflow has no reason to hold a different value.
    ghanix = lib.mkMerge [
      bootstrap
      {
        # A nixos test starts VMs, which the strict sandbox will not let a
        # build do.
        nix.install.settings.sandbox = "relaxed";
      }
    ];

    steps = [
      {
        name = "Build nixos test driver";
        run = "nix build --file . nixosTests.containerd.driverInteractive";
      }
      {
        name = "Start nixos test (background)";
        run = ''
          # The nixos-test-driver uses XDG_RUNTIME_DIR as its temp dir and
          # propagates it to VM subprocesses automatically.  GHA's default
          # XDG_RUNTIME_DIR is /run/user/1001 (1.6 GB tmpfs) which fills up
          # quickly — redirect to the root filesystem (87 GB free) instead.
          mkdir -p /tmp/nixos-test-tmp
          XDG_RUNTIME_DIR=/tmp/nixos-test-tmp ./result/bin/nixos-test-driver --no-interactive > /tmp/test.log 2>&1 &
          echo $! > /tmp/test.pid
          echo "Test running in background (PID: $(cat /tmp/test.pid))"
          sleep 5
        '';
      }
      {
        name = "Print test logs";
        run = ''
          cat /tmp/test.log || true
        '';
      }
      {
        name = "Print test results";
        run = ''
          echo "=== Test log output ==="
          cat /tmp/test.log 2>/dev/null || echo "(no test log)"
        '';
      }
    ];
  };
}
