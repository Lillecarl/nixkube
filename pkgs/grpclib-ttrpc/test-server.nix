# SPDX-License-Identifier: MIT

# The ttRPC server `tests/test_server_integration.py` talks to. Real containerd
# code, so the suite tests the protocol rather than a Python model of it.
{
  lib,
  buildGo125Module,
}:

buildGo125Module {
  pname = "ttrpc-test-server";
  version = "0.1.0";

  src = lib.cleanSource ./go;
  proxyVendor = true;
  vendorHash = "sha256-voE9iZ0rUp/iCNROLiKjuQdQS9rLVqPK0SlSGp0kPuU=";
  doCheck = false;

  ldflags = [
    "-s"
    "-w"
  ];

  meta.mainProgram = "grpclib-ttrpc-test-server";
}
