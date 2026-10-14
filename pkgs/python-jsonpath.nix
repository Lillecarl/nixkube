# SPDX-License-Identifier: Apache-2.0

{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  # build-system
  hatchling,
  hatch-vcs,
}:
buildPythonPackage rec {
  pname = "python-jsonpath";
  version = "2.0.1";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "jg-rp";
    repo = "python-jsonpath";
    tag = "v${version}";
    hash = "sha256-PkoZs6b/dtb9u1308D6LQF6kg39DslJufI/QpKMkZiQ=";
  };

  build-system = [
    hatchling
    hatch-vcs
  ];

  dependencies = [ ];

  meta = with lib; {
    description = "A flexible JSONPath engine for Python with JSON Pointer and JSON Patch";
    homepage = "https://github.com/hephex/asyncache";
    license = licenses.mit;
    maintainers = with maintainers; [ lillecarl ];
  };
}
