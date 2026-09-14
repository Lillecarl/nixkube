# The NixOS integration test workflow, as a value. See ci/workflows/ci.nix
# for how these are rendered and checked.
#
# It runs on `cidev` only. `ci.yaml` excludes that branch, so a push there
# runs this and nothing else.
{ ghalib, ... }:
ghalib.evalWorkflow {
  on = {
    push.branches = [ "cidev" ];
    workflow_dispatch = null;
  };

  env = {
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
    steps = [
      {
        name = "Checkout";
        uses = "actions/checkout@main";
      }
      # cachix/install-nix-action, which installs a daemon. A single-user nix
      # builds under the runner's TMPDIR, so what a derivation can do depends
      # on where the checkout happens to be. See nixkube issue #23.
      #
      # Not the shared setup-nix action: this workflow needs `access-tokens`
      # and `sandbox = relaxed`, which that action does not take. It does need
      # `trusted-users`, because a daemon ignores substituters and public keys
      # an untrusted user asks for -- without it every path here would come
      # from cache.nixos.org and nix-csi would go unread.
      #
      # The keys and substituters here are written again in that action, and
      # nothing makes the two agree. See issue #33. They cannot be one value
      # yet: the action is YAML that nothing renders, and ghanix describes
      # workflows rather than actions.
      {
        name = "Install Nix";
        uses = "cachix/install-nix-action@master";
        "with".extra_nix_config = ''
          experimental-features = nix-command flakes
          trusted-users = root runner
          access-tokens = github.com=''${{ secrets.GITHUB_TOKEN }}
          trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= nix-csi.cachix.org-1:i4w33gR4efO67jpz8U7g/MdvRQ6mQ3LEF9fB8tES60g=
          substituters = https://cache.nixos.org?priority=1 https://nix-csi.cachix.org?priority=2
          sandbox = relaxed
        '';
      }
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
