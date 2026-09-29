#!/usr/bin/env bash
# push_data.sh — commit generated data files from CI and push, without ever
# deepening the shallow clone.
#
# Why this exists: the workflows used
#     git pull --rebase -X ours && git push
# on a `fetch-depth: 1` checkout. When main had moved, that pull had to deepen
# the shallow history of a repo carrying ~143 MB of data files, which stalled
# and died with "fetch-pack: unexpected disconnect while reading sideband
# packet" — burning the full 300s timeout on every attempt (~50 min per job)
# and then *silently succeeding*, because the retry loop's last command was
# `sleep`. Jobs looked like they failed at the end while their real work had
# already been written to Postgres.
#
# Strategy: push first (the common case needs no fetch at all). Only if the
# push is rejected, fetch just the new tip (--depth=1, no deepening), reset
# onto it, restore our generated files, and re-commit. Fails loudly.
#
# Usage: scripts/push_data.sh "<commit message>" <path> [path...]
# Deliberately no `set -u`: guarding empty arrays against it needs constructs
# like ${arr[@]+"${arr[@]}"} and ${#arr[@]-0} whose behaviour differs between
# bash 3.2 (macOS, where this gets tested) and bash 5 (CI). One of those
# variants parsed locally, failed on CI, and turned a SUCCESSFUL push into a
# reported failure. Plain array syntax is worth more here than -u.
set -o pipefail

MSG="${1:?commit message required}"; shift
[ "$#" -gt 0 ] || { echo "no paths given"; exit 2; }
PATHS=("$@")
BRANCH="${GITHUB_REF_NAME:-main}"
GIT_TIMEOUT="${GIT_TIMEOUT:-120}"

git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

git add -- "${PATHS[@]}" 2>/dev/null || true
if git diff --cached --quiet; then
  echo "No changes to commit."
  exit 0
fi
git commit -m "$MSG"

# Snapshot what we just committed so it can be replayed onto a moved branch.
# NB: `mapfile` is bash 4+; this runs on macOS bash 3.2 during local testing,
# so read the list portably instead.
CHANGED=()
while IFS= read -r line; do
  [ -n "$line" ] && CHANGED+=("$line")
done < <(git diff --name-only HEAD~1 HEAD)

SNAP="$(mktemp -d)"
for f in "${CHANGED[@]}"; do
  [ -f "$f" ] || continue
  mkdir -p "$SNAP/$(dirname "$f")"
  cp "$f" "$SNAP/$f"
done

# `timeout` is GNU coreutils — present on CI runners, absent on stock macOS.
# Degrade to a plain call rather than failing every git command outright.
if command -v timeout >/dev/null 2>&1; then
  run_git() { GIT_TERMINAL_PROMPT=0 timeout -k 10 "$GIT_TIMEOUT" git "$@"; }
else
  run_git() { GIT_TERMINAL_PROMPT=0 git "$@"; }
fi

for attempt in 1 2 3; do
  if run_git push origin "HEAD:$BRANCH"; then
    echo "Pushed ${#CHANGED[@]} file(s) on attempt $attempt."
    exit 0
  fi
  echo "Push attempt $attempt failed — re-syncing with origin/$BRANCH (shallow)…"
  # --depth=1 keeps this a shallow fetch: never deepens, so it stays small.
  if ! run_git fetch --depth=1 origin "$BRANCH"; then
    echo "  fetch failed; retrying"
    sleep $((attempt * 10))
    continue
  fi
  git reset --hard FETCH_HEAD
  for f in "${CHANGED[@]}"; do
    [ -f "$SNAP/$f" ] || continue
    mkdir -p "$(dirname "$f")"
    cp "$SNAP/$f" "$f"
  done
  git add -- "${PATHS[@]}" 2>/dev/null || true
  if git diff --cached --quiet; then
    echo "Remote already has these changes — nothing to push."
    exit 0
  fi
  git commit -m "$MSG"
  sleep $((attempt * 5))
done

echo "::error::Could not push after 3 attempts. The generated data is already in"
echo "::error::Postgres; only the repo copy is stale."
exit 1
