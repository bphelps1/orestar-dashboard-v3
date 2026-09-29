#!/usr/bin/env bash
# Queue one account-summary retry after a narrowly proven ORESTAR/F5 block.
#
# The child authenticates the exact failed parent attempt, waits behind older
# ORESTAR work, and owns the quiet cooldown immediately before scraping.  The
# frozen selector cutoff and base chain index are preserved: a blocked attempt
# is not progress and must not advance or restart the sweep.

set -euo pipefail

if [ "$#" -ne 9 ]; then
  echo "usage: $0 <retry-index> <ref> <max-filers> <max-age-days> <current-only> <refresh-before-ts> <chain-index> <source-run-id> <source-run-attempt>" >&2
  exit 2
fi

RETRY_INDEX=$1
REF=$2
MAX_FILERS=$3
MAX_AGE_DAYS=$4
CURRENT_ONLY=$5
REFRESH_BEFORE_TS=$6
CHAIN_INDEX=$7
SOURCE_RUN_ID=$8
SOURCE_RUN_ATTEMPT=$9
MAX_RETRIES=2

case "$RETRY_INDEX" in
  0|1|2) ;;
  *) echo "::error::Invalid account-summary retry index: $RETRY_INDEX"; exit 2 ;;
esac
for VALUE_NAME in MAX_FILERS MAX_AGE_DAYS CHAIN_INDEX SOURCE_RUN_ID SOURCE_RUN_ATTEMPT; do
  VALUE=${!VALUE_NAME}
  case "$VALUE" in
    ''|*[!0-9]*) echo "::error::Invalid account-summary ${VALUE_NAME,,}: $VALUE"; exit 2 ;;
  esac
done
case "$CURRENT_ONLY" in
  true|false) ;;
  *) echo "::error::Invalid account-summary current-only value: $CURRENT_ONLY"; exit 2 ;;
esac
if [[ ! "$REFRESH_BEFORE_TS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "::error::Invalid account-summary frozen cutoff: $REFRESH_BEFORE_TS"
  exit 2
fi
if [ -z "$REF" ]; then
  echo "::error::Account-summary retry requires a branch ref."
  exit 2
fi

if [ "$RETRY_INDEX" -ge "$MAX_RETRIES" ]; then
  echo "Account-summary F5 retry limit reached ($MAX_RETRIES) — leaving the frozen sweep resumable."
  exit 0
fi

NEXT_RETRY=$((RETRY_INDEX + 1))
# Carry the complete frozen selector in the logical retry identity. The child
# rejects any dispatch or coordination requeue that mutates even one field.
HANDOFF="blocked:${NEXT_RETRY}:${SOURCE_RUN_ID}:${SOURCE_RUN_ATTEMPT}:${CHAIN_INDEX}:${REFRESH_BEFORE_TS}:${MAX_FILERS}:${MAX_AGE_DAYS}:${CURRENT_ONLY}"
DISPATCH_SCRIPT=${ACCOUNT_SUMMARY_RETRY_DISPATCH_SCRIPT:-scripts/dispatch_retry.sh}

echo "::warning::ORESTAR refused the account-summary scraper. Queueing cooled retry $NEXT_RETRY/$MAX_RETRIES on a fresh runner."
bash "$DISPATCH_SCRIPT" earliest-balances.yml --ref "$REF" \
  -f max_filers="$MAX_FILERS" \
  -f filer_ids="" \
  -f max_age_days="$MAX_AGE_DAYS" \
  -f force=false \
  -f current_only="$CURRENT_ONLY" \
  -f refresh_before_ts="$REFRESH_BEFORE_TS" \
  -f chain_index="$CHAIN_INDEX" \
  -f retry_handoff="$HANDOFF"
