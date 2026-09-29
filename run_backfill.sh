#!/usr/bin/env bash
# Trigger the self-chaining backfill workflow and follow its first run.
# Requires: gh CLI installed and authenticated (brew install gh && gh auth login)

set -euo pipefail

REPO="${REPO:-bphelps1/orestar-dashboard-v3}"
WORKFLOW="backfill.yml"
START_YEAR="2017"

echo "Triggering $WORKFLOW in $REPO..."
gh workflow run "$WORKFLOW" --repo "$REPO" -f start_year="$START_YEAR"

# Wait for GitHub to register the dispatch, then follow it. Successor batches
# are dispatched by the workflow itself and persist progress in Supabase
# Storage; GitHub repository contents are intentionally code-only.
sleep 10
RUN_ID=$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --limit 1 \
  --json databaseId --jq '.[0].databaseId')
echo "Following initial run $RUN_ID (successor links appear in its log)..."
gh run watch "$RUN_ID" --repo "$REPO" --exit-status
