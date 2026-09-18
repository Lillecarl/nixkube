# SPDX-License-Identifier: MIT

# The Python suites, one derivation each.
#
# pyproject.nix's builders have no `pytestCheckHook` equivalent: a package
# propagates nothing, so its own build cannot import what it depends on. A
# suite therefore runs against a venv, which is also closer to what ships.
#
# **Each suite is its own `checks` member.** Before this they rode along inside
# the packages, and `checkMembers.pynixd-nixkube-tests` named a package rather
# than a test result -- so a suite that stopped running would have looked
# exactly like a suite that passed.
{
  lib,
  pkgs,
  pythonSet,
}:

let
  # Only `tests/` and the `pyproject.toml` that configures pytest. The project
  # itself comes from the venv, and `tests/__init__.py` makes pytest put this
  # directory on `sys.path` rather than the package's own source.
  suiteSource =
    projectRoot:
    lib.fileset.toSource {
      root = projectRoot;
      fileset = lib.fileset.unions [
        (lib.fileset.fileFilter (file: file.hasExt "py") (projectRoot + "/tests"))
        (projectRoot + "/pyproject.toml")
      ];
    };

  mkSuite =
    {
      name,
      projectRoot,
      # `pythonSet.mkVirtualEnv`'s spec: which projects, with which extras.
      spec,
      env ? { },
      nativeBuildInputs ? [ ],
    }:
    pkgs.runCommand "${name}-tests"
      (
        env
        // {
          nativeBuildInputs = [
            (pythonSet.mkVirtualEnv "${name}-test-env" spec)
          ]
          ++ nativeBuildInputs;
        }
      )
      ''
        cp -r ${suiteSource projectRoot}/. .
        chmod -R +w .
        pytest -p no:cacheprovider tests
        touch "$out"
      '';
in
{
  nixkube-tests = mkSuite {
    name = "nixkube";
    projectRoot = ../pkgs/nixkube;
    spec.nixkube = [ "test" ];
  };

  pynixd-nixkube-tests = mkSuite {
    name = "pynixd-nixkube";
    projectRoot = ../pkgs/pynixd-nixkube;
    spec.pynixd-nixkube = [ "test" ];
    # `pynixd_nixkube/setup.py` reads both at import time, so a suite that
    # does not set them fails on collection rather than on an assertion.
    env = {
      FAKE_NSS = "${pkgs.pynixd-nixkube.fakeNss}";
      CA_CERTS = "${pkgs.dockerTools.caCertificates}";
    };
  };

  nri-wait-tests = mkSuite {
    name = "nri-wait";
    projectRoot = ../pkgs/nri-wait;
    spec.nri-wait = [ "test" ];
  };

  grpclib-ttrpc-tests = mkSuite {
    name = "grpclib-ttrpc";
    projectRoot = ../pkgs/grpclib-ttrpc;
    spec.grpclib-ttrpc = [ "test" ];
    env.TTRPC_TEST_SERVER = lib.getExe (pkgs.callPackage ../pkgs/grpclib-ttrpc/test-server.nix { });
  };

  grpclib-nri-tests = mkSuite {
    name = "grpclib-nri";
    projectRoot = ../pkgs/grpclib-nri;
    spec.grpclib-nri = [ "test" ];
    env.NRI_TEST_SERVER = lib.getExe (pkgs.callPackage ../pkgs/grpclib-nri/test-server.nix { });
  };
}
