# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config = {
    nixkube.loggingConfig = {
      renderer = "json";
      loggers = {
        nixkube.level = "DEBUG";
        httpx.level = "WARNING";
      };
      root.level = "INFO";
    };
    # Substituters every CI variant can actually reach.
    #
    # The NixOS test VM's nix-serve, at the PTP CNI gateway 10.113.37.1:5000,
    # used to be in this list with the note "Nix handles dead/unreachable
    # substituters gracefully so this is safe on Kind". Measured on run
    # 34835732224, it is not safe: there is no such Service in a kind cluster,
    # and every miss pays 5 x 15s of retries.
    #
    #   80.4s   the env-ssl build
    #   80.09s  NixVolumeMount on path-hello
    #   88.48s  pynixd-0
    #
    # That is enough to push nri-hello-ro and nri-hello-rw past the
    # `--timeout=300s` in ci.yaml, and it masks the cache gate as well:
    # assert_cached.py raises Indeterminate on an unreachable substituter, so
    # a FAIL becomes UNKNOWN.
    #
    # It now lives on kubenixCI2, which is the instance the NixOS test uses.
    # See issue #30.
    nixkube.node.nixConfig.settings.substituters = [
      "https://nixkube.cachix.org"
      "https://cache.nixos.org"
    ];
  };
}
