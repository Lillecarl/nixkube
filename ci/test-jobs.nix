# SPDX-License-Identifier: MIT

# The Jobs `kubenixCITest` creates, and which of them are asserted.
#
# Two readers, which is why this is a file rather than a `let` in one of
# them: `ci/workflows/ci.nix` builds the kind jobs' `kubectl wait` and
# `kubectl delete` lines out of it, and `nix/uml/ci.nix` hands it to the
# guest test that does the same steps. A list that lived in one of those
# would be a list the other one silently disagreed with.
{
  # Everything the deployment creates, and so everything the cleanup step
  # has to delete.
  deployed = [
    "flake-hello"
    "expr-hello"
    "path-hello"
    "commandpath-hello"
    "env-ssl"
    "invalid-storepath-hello"
    "invalid-flake-hello"
    "invalid-expr-hello"
    "nri-hello-ro"
    "nri-hello-rw"
  ];

  # The ones whose completion is actually waited for.
  #
  # The difference between the two lists is issue #31: `commandpath-hello`
  # and the three `invalid-*` Jobs are deployed and deleted but never
  # asserted, so a regression in any of them passes. Deliberate for the
  # `invalid-*` three, which are expected to fail; not deliberate for
  # `commandpath-hello`.
  asserted = [
    "flake-hello"
    "expr-hello"
    "path-hello"
    "env-ssl"
    "nri-hello-ro"
    "nri-hello-rw"
  ];
}
