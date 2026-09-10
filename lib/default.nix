# SPDX-License-Identifier: MIT

self: lib: {
  # `lib.cleanSource`, plus what Python leaves behind.
  #
  # `cleanSource` drops VCS directories and editor leftovers. It keeps
  # `__pycache__`, `.pytest_cache` and `.pyc` files, and every one of those
  # lands in a source tree the moment somebody runs the tests.
  #
  # So a developer who has run pytest builds a different derivation from CI,
  # which checks out a clean tree. Different package, different `cacheEnv`,
  # different manifest -- and a cluster then asks its substituters for a store
  # path that CI never built. Measured on this repository: eight such
  # directories under `pkgs/`, and `pynixd-nixkube` and `nixkube` both hashed
  # differently because of them.
  #
  # `.mypy_cache` and `.ruff_cache` are here for the same reason, before
  # somebody meets them the hard way.
  cleanPythonSource =
    src:
    lib.cleanSourceWith {
      src = lib.cleanSource src;
      filter =
        path: type:
        let
          base = baseNameOf (toString path);
        in
        !(
          (
            type == "directory"
            && builtins.elem base [
              "__pycache__"
              ".pytest_cache"
              ".mypy_cache"
              ".ruff_cache"
              ".ropeproject"
            ]
          )
          || lib.hasSuffix ".pyc" base
          || lib.hasSuffix ".pyo" base
          || lib.hasSuffix ".egg-info" base
        );
    };
}
