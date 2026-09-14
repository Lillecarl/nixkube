# The gates, as their own entry point, so `nix build --file ./checks.nix all`
# runs what CI runs. Same shape as easykubenix's checks.nix.
(import ./. { }).checks
