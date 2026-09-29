#!/usr/bin/env bash
#
# Dispatch a workflow, retrying through transient GitHub failures.
#
# Every self-continuing chain in this repo hands off to its successor with a
# single `gh workflow run`. That one call is the whole chain's single point of
# failure: on 2026-08-17 it returned
#
#     could not create workflow dispatch event: HTTP 503:
#     No server is currently available to service your request.
#
# and a chain that had just finished Local 48 — 13,626 rows, the largest gap in
# the dataset — stopped dead. The run's own work had succeeded and been pushed;
# only the hand-off failed, and nothing was scheduled to resume.
#
# A 503 from GitHub's API is exactly the kind of failure worth retrying: it is
# transient, it is not our fault, and the cost of retrying is a few seconds
# against losing hours of chained progress.
#
# Usage:  scripts/dispatch_retry.sh <workflow.yml> --ref main -f key=value ...
#
# Exits non-zero only after every attempt fails, so a genuinely broken dispatch
# (bad input, disabled workflow, missing token) still fails the step loudly
# rather than being swallowed.

# Deliberately no `set -e`: a failed attempt must fall through to the retry
# rather than abort the script on the first non-zero exit.
set -uo pipefail

ATTEMPTS="${DISPATCH_ATTEMPTS:-5}"
BACKOFF=(5 15 30 60)

for attempt in $(seq 1 "$ATTEMPTS"); do
  if out=$(gh workflow run "$@" 2>&1); then
    echo "Dispatched on attempt ${attempt}."
    exit 0
  fi
  echo "Dispatch attempt ${attempt}/${ATTEMPTS} failed: ${out}"

  # A workflow that is disabled, or an input the workflow does not define, will
  # fail identically on every attempt. Retrying those just delays a failure that
  # a human has to fix anyway, so stop immediately and say why.
  case "$out" in
    *"disabled"*|*"Unexpected inputs"*|*"could not find any workflows"*|*"HTTP 404"*)
      echo "::error::Dispatch failed for a reason retrying cannot fix: ${out}"
      exit 1
      ;;
  esac

  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    delay="${BACKOFF[$((attempt - 1))]:-60}"
    echo "Retrying in ${delay}s..."
    sleep "$delay"
  fi
done

echo "::error::Dispatch failed after ${ATTEMPTS} attempts — the chain stops here. Re-run the workflow to resume."
exit 1
