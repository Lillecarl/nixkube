#!/usr/bin/env bash
# Vendored from umbrella/bin/walkback.sh. CI needs it before Nix runs, so
# it cannot come through the umbrella: finding the umbrella is its job.
# The umbrella revision that locks the nearest landed ancestor of HEAD.
#
# A child cannot hold the revision of the umbrella that locks it: the
# umbrella's commit holds the child's hash, so a child commit holding the
# umbrella's hash would need a hash that contains itself. `umbrella mark`
# publishes the answer out of the tree instead, one ref per child revision:
#
#     refs/umbrella/<child>/<child revision>  ->  the umbrella commit
#
# A commit that nobody landed has no ref. Its ancestors do, so walk back and
# take the first hit. That is the umbrella this branch grew from, which is
# the one that supplies every other source.
#
# One network call: `git ls-remote` reads every ref of this child at once.
#
# Prints `<umbrella revision>` on stdout. Everything else goes to stderr, so
# a CI step can redirect stdout into $GITHUB_OUTPUT.
set -euo pipefail

UMBRELLA_URL="${1:?usage: walkback.sh <umbrella url> <child name> [max commits]}"
CHILD="${2:?usage: walkback.sh <umbrella url> <child name> [max commits]}"
MAX="${3:-100}"

declare -A LOCKED
while read -r umbrella_rev refname; do
  LOCKED["${refname#refs/umbrella/"$CHILD"/}"]="$umbrella_rev"
done < <(git ls-remote "$UMBRELLA_URL" "refs/umbrella/$CHILD/*")

if [ "${#LOCKED[@]}" -eq 0 ]; then
  echo "walkback: the umbrella has never locked a revision of '$CHILD'." >&2
  echo "walkback: run 'umbrella mark' in the umbrella to publish the refs." >&2
  exit 1
fi

steps=0
while read -r sha; do
  if [[ -v "LOCKED[$sha]" ]]; then
    echo "walkback: ${sha:0:8} is locked by umbrella ${LOCKED[$sha]:0:8}, $steps step(s) back" >&2
    echo "${LOCKED[$sha]}"
    exit 0
  fi
  steps=$((steps + 1))
done < <(git rev-list --max-count="$MAX" HEAD)

# A shallow clone stops early and looks the same as a branch nobody landed,
# so say which depth was searched.
echo "walkback: no ancestor of HEAD in the last $steps commit(s) is locked." >&2
echo "walkback: deepen the checkout (fetch-depth) or land the branch point." >&2
exit 1
