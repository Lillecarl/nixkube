# SPDX-License-Identifier: MIT

let
  default = import ./. { };
  inherit (default) pkgs;

  pypkgs =
    pp:
    with pp;
    [
      pytest
      pytest-asyncio
      hypothesis
      sphinx
      myst-parser
      furo
    ]
    ++ pkgs.nixkube.dependencies
    ++ pkgs.pynixd-nixkube.dependencies;
  python = pkgs.python3.withPackages pypkgs;

  # `pylsp-mypy` fails its own test suite on python3.14 in the pinned
  # nixpkgs, so the whole shell refused to build -- and with it `check`,
  # whose first step is `nix-shell --run "pyright ..."`. An editor
  # integration was able to fail CI.
  #
  #   FAILED test/test_plugin.py::test_plugin - assert 2 == 1
  #   FAILED test/test_plugin.py::test_handling_of_line_endings[...] - KeyError: 'code'
  #   FAILED test/test_plugin.py::test_multiple_workspaces - assert 1 == 0
  #   ... 7 failed, 32 passed
  #
  # Broken upstream, not here: `cache.nixos.org` returns 404 for it, so
  # nixpkgs' own builders did not get it either. Every other tool in this
  # shell returns 200.
  #
  # Skipping its tests is safe in a way that skipping Nix's was not (see
  # `pkgs/default.nix`). There is no cache entry to lose, because there is no
  # successful build to cache, and nothing outside this shell holds it -- it
  # ships in no image and no environment.
  pylsp-mypy = pkgs.python3Packages.pylsp-mypy.overridePythonAttrs (_: {
    doCheck = false;
    doInstallCheck = false;
  });
  xonsh = pkgs.xonsh.override {
    extraPackages = pypkgs;
  };
in
pkgs.mkShell {
  packages = [
    python
    xonsh
    pkgs.cachix
    pkgs.cargo
    pkgs.go
    pkgs.just
    pkgs.kluctl
    pkgs.kubectx
    pkgs.pyright
    pylsp-mypy
    pkgs.python3Packages.pylsp-rope
    pkgs.python3Packages.python-lsp-ruff
    pkgs.python3Packages.python-lsp-server
    pkgs.regctl
    pkgs.ruff
    pkgs.rustc
    pkgs.ty
    pkgs.skopeo
    pkgs.stern
    pkgs.dive
    default.treefmt
  ];
  shellHook = # bash
    ''
      # Make LSPs that are too stupid to run python to check environment happy
      export PYTHONPATH="${python}/${python.sitePackages}:$PYTHONPATH"
    '';
}
