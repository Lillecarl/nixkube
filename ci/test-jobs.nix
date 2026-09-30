# SPDX-License-Identifier: MIT

# The Jobs `kubenixCITest` creates, and what each one is supposed to do.
#
# A file of its own and not a `let` in `nix/uml/ci.nix`, because the
# workload definitions in kubenix/ci/ and the test that asserts them are
# two places, and this is the one list both sides can be checked against.
rec {
  # The ones that have to finish.
  #
  # `commandpath-hello` is here because issue #31 was that it was not: it
  # was deployed and deleted without anything looking at it, so it could
  # have been broken for months. It asks the driver to find the store path
  # in the container's command rather than in a volumeAttribute, which is
  # the one request shape nothing else here makes.
  asserted = [
    "flake-hello"
    "expr-hello"
    "path-hello"
    "commandpath-hello"
    "env-ssl"
    "nri-hello-ro"
    "nri-hello-rw"
  ];

  # The ones that have to *not* finish.
  #
  # Each asks for something that cannot be built: a store path of zeroes, a
  # flake that is not there, an expression that does not evaluate. A driver
  # that mounted anything for one of these would be a driver that mounts
  # the wrong thing quietly, so "the Job never succeeded" is the assertion,
  # and it is as much a regression gate as the list above.
  rejected = [
    "invalid-storepath-hello"
    "invalid-flake-hello"
    "invalid-expr-hello"
  ];

  # Everything the deployment creates, and so everything the cleanup step
  # has to delete. Derived, so a Job cannot be added to one of the lists
  # above and then left behind by the cleanup.
  deployed = asserted ++ rejected;
}
