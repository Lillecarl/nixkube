# One umbrella revision for a whole workflow run.
#
# `nix/sources.nix` resolves the umbrella from UMBRELLA_REV when it is set.
# Without it that reference is unlocked, so every job resolves the head of the
# default branch again. `umbrella land` pushes the working copies, which starts
# the run, and the umbrella lock commit follows seconds later. A run that
# straddles the push reads two revisions.
#
# Measured in run 35026926963: seven seconds between the first job starting and
# the lock commit, two different `cacheEnv` store paths, and `build-manifests`
# asking for one that nothing had built -- "1 of 544 paths in the closure are on
# no substituter". nanopynix issue #301.
#
# So one job resolves the head, over the git protocol rather than through
# api.github.com, and every other job takes the answer through `needs`. It costs
# three seconds.
#
# It wraps `evalWorkflow` rather than being written into each workflow, so a
# workflow or a job added later cannot be the one that forgets it.
evalWorkflow: workflow:
let
  jobName = "umbrella-rev";

  resolver = {
    runs-on = "ubuntu-24.04";
    timeout-minutes = 5;
    outputs.rev = "\${{ steps.resolve.outputs.rev }}";
    steps = [
      {
        id = "resolve";
        name = "Resolve the umbrella revision";
        run = "git ls-remote https://github.com/nixidae/nixidae main | cut -f1 | sed 's/^/rev=/' >> \"$GITHUB_OUTPUT\"";
      }
    ];
  };

  # `needs` is a string, a list or absent, and it has to stay whichever it was
  # with this one added.
  asList =
    value:
    if value == null then
      [ ]
    else if builtins.isList value then
      value
    else
      [ value ];

  pin =
    _: job:
    job
    // {
      needs = asList (job.needs or null) ++ [ jobName ];
      # A job that sets UMBRELLA_REV itself wins, because its own `env` is
      # written after this one.
      env = {
        UMBRELLA_REV = "\${{ needs.${jobName}.outputs.rev }}";
      }
      // (if job.env or null == null then { } else job.env);
    };
in
evalWorkflow (
  workflow
  // {
    jobs = builtins.mapAttrs pin workflow.jobs // {
      ${jobName} = resolver;
    };
  }
)
