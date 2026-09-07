# NRI wait OCI hook - waits for Nix builds via ZeroMQ
# Runs inside chroot(/var/lib/nix-csi) so standard glibc is fine
{
  lib,
  buildPythonApplication,
  hatchling,
  pyzmq,
  pytest,
  pytestCheckHook,
}:

buildPythonApplication {
  pname = "nri-wait";
  version = "0.1.0";

  src = lib.cleanSource ./.;

  pyproject = true;

  build-system = [ hatchling ];
  dependencies = [ pyzmq ];

  # These tests are the whole reason the hook is trustworthy: they hold it
  # to timing the daemon's silence rather than the build. They need no
  # cluster and no network, only two ZeroMQ sockets, so the package build
  # is the right place for them.
  nativeCheckInputs = [
    pytest
    pytestCheckHook
  ];

  meta = with lib; {
    description = "OCI hook that waits for NRI build completion";
    homepage = "https://github.com/lillecarl/nix-csi";
    license = licenses.mit;
    maintainers = with maintainers; [ lillecarl ];
    platforms = platforms.linux;
  };
}
