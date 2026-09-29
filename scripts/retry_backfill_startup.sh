#!/usr/bin/env bash
# Queue one automatic identity-backfill startup retry on a fresh runner.
#
# The retry child carries its handoff in the otherwise plain end_date input,
# because workflow_dispatch already uses GitHub's maximum ten inputs. Its
# unique concurrency group starts it without risking pending-run eviction; the
# child then waits for every older scraper and holds a full quiet cooldown
# immediately before it touches ORESTAR.

set -euo pipefail

if [ "$#" -ne 13 ]; then
  echo "usage: $0 <retry-index> <ref> <filer-ids> <start-year> <end-date> <date-field> <chain-index> <resume-auto> <identity-resume> <reset-auto> <verification-ids> <source-run-id> <source-run-attempt>" >&2
  exit 2
fi

RETRY_INDEX=$1
REF=$2
FILER_IDS=$3
START_YEAR=$4
END_DATE=$5
DATE_FIELD=$6
CHAIN_INDEX=$7
RESUME_AUTO=$8
IDENTITY_RESUME=$9
RESET_AUTO=${10}
VERIFICATION_IDS=${11}
SOURCE_RUN_ID=${12}
SOURCE_RUN_ATTEMPT=${13}
MAX_RETRIES=2

case "$RETRY_INDEX" in
  0|1|2) ;;
  *) echo "::error::Invalid backfill startup retry index: $RETRY_INDEX"; exit 2 ;;
esac
case "$START_YEAR" in
  ''|*[!0-9]*) echo "::error::Invalid backfill startup start year: $START_YEAR"; exit 2 ;;
esac
case "$CHAIN_INDEX" in
  ''|*[!0-9]*) echo "::error::Invalid backfill chain index: $CHAIN_INDEX"; exit 2 ;;
esac
case "$SOURCE_RUN_ID" in
  ''|*[!0-9]*) echo "::error::Invalid backfill startup source run ID: $SOURCE_RUN_ID"; exit 2 ;;
esac
case "$SOURCE_RUN_ATTEMPT" in
  ''|*[!0-9]*) echo "::error::Invalid backfill startup source attempt: $SOURCE_RUN_ATTEMPT"; exit 2 ;;
esac
if [[ ! "$END_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "::error::Invalid backfill startup end date: $END_DATE"
  exit 2
fi
case "$DATE_FIELD" in
  filed|tran) ;;
  *) echo "::error::Invalid backfill date field: $DATE_FIELD"; exit 2 ;;
esac
if [ "$RESUME_AUTO" != "true" ]; then
  echo "::error::A startup retry must remain in the automatic remediation chain."
  exit 2
fi
case "$IDENTITY_RESUME" in
  true|false) ;;
  *) echo "::error::Invalid identity-resume state: $IDENTITY_RESUME"; exit 2 ;;
esac
case "$RESET_AUTO" in
  true|false) ;;
  *) echo "::error::Invalid reset-auto state: $RESET_AUTO"; exit 2 ;;
esac
if [ -z "$REF" ] || [ -z "$FILER_IDS" ] || [ -z "$VERIFICATION_IDS" ]; then
  echo "::error::Backfill startup retry requires a ref, filer IDs, and verification IDs."
  exit 2
fi
if [[ ! "$FILER_IDS" =~ ^[0-9]+([ ]+[0-9]+)*$ ]] || \
   [[ ! "$VERIFICATION_IDS" =~ ^[0-9]+([ ]+[0-9]+)*$ ]]; then
  echo "::error::Backfill startup retry requires space-separated numeric filer IDs."
  exit 2
fi
for FID in $FILER_IDS; do
  case " $VERIFICATION_IDS " in
    *" $FID "*) ;;
    *) echo "::error::Retry filer $FID is absent from the verification scope."; exit 2 ;;
  esac
done

if [ "$RETRY_INDEX" -ge "$MAX_RETRIES" ]; then
  echo "Identity backfill startup retry limit reached ($MAX_RETRIES) — leaving progress in place."
  exit 0
fi

NEXT_RETRY=$((RETRY_INDEX + 1))
HANDOFF="startup:${END_DATE}:${NEXT_RETRY}:${SOURCE_RUN_ID}:${SOURCE_RUN_ATTEMPT}:${IDENTITY_RESUME}"
DISPATCH_SCRIPT=${BACKFILL_RETRY_DISPATCH_SCRIPT:-scripts/dispatch_retry.sh}

echo "::warning::Initial ORESTAR setup was inconclusive. Queueing fresh-run startup retry $NEXT_RETRY/$MAX_RETRIES with a full quiet cooldown."
bash "$DISPATCH_SCRIPT" backfill.yml --ref "$REF" \
  -f filer_ids="$FILER_IDS" \
  -f start_year="$START_YEAR" \
  -f end_date="$HANDOFF" \
  -f date_field="$DATE_FIELD" \
  -f chain_index="$CHAIN_INDEX" \
  -f identity_remediation=true \
  -f resume_auto="$RESUME_AUTO" \
  -f reset_auto="$RESET_AUTO" \
  -f verification_filer_ids="$VERIFICATION_IDS"
