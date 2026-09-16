# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  config,
  ...
}:
{
  options.gc = {
    retainSeconds = lib.mkOption {
      type = lib.types.ints.positive;
    };
    intervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
    };
  };
  config = {
    logger.files = [
      "gc.log"
    ];
    services.gc = {
      command = pkgs.writeShellApplication {
        name = "gc";
        runtimeInputs = [
          pkgs.nix
          pkgs.jq
          # `mktemp`, `timeout`, `shuf` and `grep`. They used to come off the
          # ambient PATH of the service.
          pkgs.coreutils
          pkgs.gnugrep
        ];
        text =
          let
            rs = toString config.gc.retainSeconds;
            lis = toString (config.gc.intervalSeconds / 2);
            uis = toString config.gc.intervalSeconds;
          in
          # bash
          ''
            while :; do
              # Copy everything to cache
              PYNIXD_ENABLED="''${PYNIXD_ENABLED:-"false"}"
              GC_KEEP_SECONDS="''${GC_KEEP_SECONDS:-"3600"}"
              IS_CACHE="''${IS_CACHE:-"false"}"
              if test "$PYNIXD_ENABLED" = "true"; then
                # `timeout`, because a copy to an unreachable cache does not
                # return and this loop never reaches the delete below. Issue #38.
                timeout 1800 nix copy --all --store local --to ssh-ng://nix@pynixd || true
              fi
              # Garbage collect anything older than an hour
              echo "Collecting garbage"
              WORK=$(mktemp -d)
              # `nix path-info --json` answers an object keyed by store path,
              # and the value holds no `path` of its own. `map(... | .path)`
              # walked the values and printed `null` for every one, so this
              # deleted nothing on any node, ever.
              nix path-info --store local --all --json --json-format 1 > "$WORK/path-info.json"
              jq -r --argjson age "$GC_KEEP_SECONDS" \
                'to_entries | map(select(.value.registrationTime < (now - $age)) | .key) | .[]' \
                < "$WORK/path-info.json" > "$WORK/old-paths.txt"
              # `--skip-live` is not a flag on nix 2.34.8, so this step failed
              # on the flag alone. Refusing a live path is already the default.
              #
              # Nix then exits 1 whenever it refused one, which is the normal
              # case here: the list holds every path older than GC_KEEP_SECONDS
              # and most of them are in use. `set -e` would end the whole loop,
              # so tolerate that and report anything else.
              if ! nix store delete --store local --stdin \
                   < "$WORK/old-paths.txt" > "$WORK/delete.log" 2>&1; then
                if grep '^error:' "$WORK/delete.log" | grep -qv 'since it is still alive'; then
                  echo "gc: nix store delete failed" >&2
                fi
              fi
              cat "$WORK/delete.log"
              rm -rf "$WORK"
              if test "$IS_CACHE" = "true"; then
                echo "Optimising Nix store (hardlinking)"
                nix store optimise
                echo "Signing all storepaths (this needs to be hooked somehow)"
                nix path-info --all | nix store sign --stdin --key-file /etc/nix-key/nix_ed25519 
              fi
              # chill
              SLEEP=$(shuf -i ${lis}-${uis} -n 1)
              echo Sleeping for "$SLEEP" seconds
              sleep "$SLEEP"
            done
          '';
      };
      log-type = "file";
      logfile = "/var/log/gc.log";
      depends-on = [
        "setup"
        "nix-daemon"
      ];
    };
  };
}
