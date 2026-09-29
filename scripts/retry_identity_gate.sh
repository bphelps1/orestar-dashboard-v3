#!/usr/bin/env bash
# Retry an exact identity verification after a shared ORESTAR/F5 cooldown.
#
# Keeping the failed workflow alive during sleep is intentional: younger
# ORESTAR workflows see it in await-orestar and stay out of the lane, so the
# cooldown is actually quiet rather than merely delaying this one caller.

set -euo pipefail

if [ "$#" -ne 7 ]; then
  echo "usage: $0 <retry-index> <ref> <filer-ids> <start-year> <end-date> <resume-auto> <source-run-id>" >&2
  exit 2
fi

RETRY_INDEX=$1
REF=$2
FILER_IDS=$3
START_YEAR=$4
END_DATE=$5
RESUME_AUTO=$6
SOURCE_RUN_ID=$7
MAX_RETRIES=2
COOLDOWN_SECONDS=1200

case "$RETRY_INDEX" in
  ''|*[!0-9]*)
    echo "::error::Invalid identity-gate retry index: $RETRY_INDEX"
    exit 2
    ;;
esac

if [ -z "$REF" ] || [ -z "$FILER_IDS" ] || [ -z "$START_YEAR" ] || [ -z "$END_DATE" ]; then
  echo "::error::Identity-gate retry requires a ref, filer IDs, and frozen range."
  exit 2
fi

if [[ ! "$END_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "::error::Invalid identity-gate end date: $END_DATE"
  exit 2
fi

case "$SOURCE_RUN_ID" in
  ''|*[!0-9]*)
    echo "::error::Invalid identity-gate source run ID: $SOURCE_RUN_ID"
    exit 2
    ;;
esac

case "$RESUME_AUTO" in
  true|false) ;;
  *)
    echo "::error::Invalid resume-auto value: $RESUME_AUTO"
    exit 2
    ;;
esac

if [ "$RETRY_INDEX" -ge "$MAX_RETRIES" ]; then
  echo "Identity verification retry limit reached ($MAX_RETRIES) — leaving progress in place."
  exit 0
fi

NEXT_RETRY=$((RETRY_INDEX + 1))
NOT_BEFORE=$(( $(date +%s) + COOLDOWN_SECONDS ))
DISPATCH_SCRIPT=${IDENTITY_GATE_DISPATCH_SCRIPT:-scripts/dispatch_retry.sh}
echo "::warning::Exact verification was inconclusive. Queueing retry $NEXT_RETRY/$MAX_RETRIES, then holding the ORESTAR lane quiet for 20 minutes."
bash "$DISPATCH_SCRIPT" coverage-diff.yml --ref "$REF" \
  -f filer_ids="$FILER_IDS" \
  -f start_year="$START_YEAR" \
  -f end_date="$END_DATE" \
  -f recheck=true \
  -f require_no_missing=true \
  -f resume_auto_backfill="$RESUME_AUTO" \
  -f verification_retry="$NEXT_RETRY" \
  -f verification_parent_run_id="$SOURCE_RUN_ID" \
  -f verification_not_before="$NOT_BEFORE"

# Queue first so this retry is older than any unrelated workflow that arrives
# during the cooldown. await-orestar keeps it behind this still-running gate;
# when the sleep ends, total ordering gives it the lane before later arrivals.
sleep "$COOLDOWN_SECONDS"
