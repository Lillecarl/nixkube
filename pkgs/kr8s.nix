# SPDX-License-Identifier: Apache-2.0

{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  # dependencies
  cachetools,
  cryptography,
  exceptiongroup,
  packaging,
  pyyaml,
  python-jsonpath,
  anyio,
  httpx,
  httpx-ws,
  python-box,
  # build-system
  hatchling,
  hatch-vcs,
}:
buildPythonPackage (finalAttrs: {
  pname = "kr8s";
  version = "0.20.15";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "kr8s-org";
    repo = "kr8s";
    tag = "v${finalAttrs.version}";
    hash = "sha256-98ImCNHNjmwKKQAGitvlpwCC+efcCKsbHbF2vcLIXE0=";
  };

  build-system = [
    hatchling
    hatch-vcs
  ];

  dependencies = [
    cachetools
    cryptography
    exceptiongroup
    packaging
    pyyaml
    python-jsonpath
    anyio
    httpx
    httpx-ws
    python-box
  ];

  pythonImportsCheck = [ "kr8s" ];

  meta = with lib; {
    description = "A Python client library for Kubernetes";
    homepage = "https://github.com/kr8s-org/kr8s";
    license = licenses.mit;
    maintainers = with maintainers; [ lillecarl ];
  };
})
