# NRI wait OCI hook - waits for Nix builds via ZeroMQ
# Runs inside chroot(/var/lib/nix-csi) so standard glibc is fine
{
  lib,
  buildPythonApplication,
  hatchling,
  pyzmq,
  pytest,
  pytest-timeout,
  pytestCheckHook,
}:

buildPythonApplication {
  pname = "nri-wait";
  version = "0.1.0";

  src = lib.cleanPythonSource ./.;

  pyproject = true;

  build-system = [ hatchling ];
  dependencies = [ pyzmq ];

  # These tests are the whole reason the hook is trustworthy: they hold it
  # to timing the daemon's silence rather than the build. They need no
  # cluster and no network, only two ZeroMQ sockets, so the package build
  # is the right place for them.
  nativeCheckInputs = [
    pytest
    pytest-timeout
    pytestCheckHook
  ];

  # A hang here is not theoretical. On 2026-09-10 both CI builders stopped
  # dead on this derivation and printed nothing for 44 and 60 minutes, until
  # the job timeout killed them. The same derivation
  # (28a1pz6akwrdi5qn4jk420pv52mx4zzi) builds in six seconds outside GHA, so
  # the cause is still unknown.
  #
  # Every test here waits on a socket, and every wait is bounded by TIMEOUT=1
  # in the suite. So 60s is a hundredfold margin, and reaching it means
  # something is stuck rather than slow. The timeout turns a silent hour into
  # a failure with the stack of the thread that stopped.
  pytestFlags = [
    "--timeout=60"
    "--timeout-method=thread"
  ];

  meta = with lib; {
    description = "OCI hook that waits for NRI build completion";
    homepage = "https://github.com/lillecarl/nix-csi";
    license = licenses.mit;
    maintainers = with maintainers; [ lillecarl ];
    platforms = platforms.linux;
  };
}
