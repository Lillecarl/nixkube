# Preview rendering

`ekn render --package-placeholders` and `ekn diff --package-placeholders` defer nixkube's node and cache closures.
These commands use `ekn.lib.packageRef` before interpolating package paths.
Normal commands retain real store paths and their string context.

Each role's internal `nixConfig.nixConf` resolves to `{ file, text }`.
The default generator uses nixpkgs' formatter for both representations.
Preview reads `text`; normal rendering reads the generated file, including its build-time validation.

Existing custom package assignments remain valid:

```nix
nixkube.node.nixConfig.nixConf = lib.mkForce (pkgs.writeText "nix.conf" "max-jobs = 2\n");
```

The module converts such a package into `{ file = package; text = null; }`.
Both modes then read the custom file. A package's arbitrary `.text` attribute is ignored.
To defer a custom file, supply its exact text explicitly:

```nix
let
  text = "max-jobs = 2\n";
in
{
  nixkube.node.nixConfig.nixConf = {
    file = pkgs.writeText "nix.conf" text;
    inherit text;
  };
}
```

A file-only override clears the default preview text.
Keep explicit custom text equal to the file contents. The custom file controls its own validation.
