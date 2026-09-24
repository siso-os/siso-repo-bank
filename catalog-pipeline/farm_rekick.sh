#!/usr/bin/env bash
# farm_rekick.sh — keep JARVIS's star-census farm running to completion + auto-ingest into the catalog.
# research-lead owns this (it needs the full census). JARVIS owns the farm script itself.
#
# The farm exits on hourly GitHub-budget-low (sets tier PARTIAL, saves progress.json) but does NOT
# re-kick itself. This watcher re-runs the resumable farm after each budget reset until all 7 tiers
# report complete, and imports any new/updated shards into research/repo-catalog after each pass.
#
# Usage:  bash farm_rekick.sh            # loop until census complete
#         bash farm_rekick.sh --once     # single pass (one farm run + ingest), for cron/manual
set -uo pipefail

FARM_DIR="/Users/shaansisodia/SISO_Workspace/.agents/scratch/github-farm"
FARM_SCRIPT="$FARM_DIR/farm_github_urls.mjs"
PROGRESS="$FARM_DIR/progress.json"
CATALOG_DIR="/Users/shaansisodia/SISO_Workspace/SISO_Agent_Base/research/repo-catalog"
CATALOG="$CATALOG_DIR/catalog.py"
ROUND="round-0002"   # JARVIS census lands in round-0002
LOG="$CATALOG_DIR/farm_rekick.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }

# all 7 tiers complete?  (returns 0 = complete, 1 = more to do)
census_complete() {
  python3 - "$PROGRESS" <<'PY'
import json, sys
TIERS = {"100k_plus","50k_100k","10k_50k","5k_10k","1k_5k","500_1k","100_500"}
try:
    p = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)  # no progress yet -> not complete
tiers = p.get("tiers", {})
done = {k for k,v in tiers.items() if v.get("status") == "complete"}
sys.exit(0 if TIERS.issubset(done) else 1)
PY
}

# import every JARVIS shard into the catalog (idempotent: already-seen rows skip, never double-count)
ingest_shards() {
  local total_new=0
  for f in "$FARM_DIR"/urls_*.jsonl; do
    [ -f "$f" ] || continue
    out=$(python3 "$CATALOG" import-raw --source "$f" --format harvest-jsonl --round "$ROUND" 2>&1)
    log "ingest: $out"
  done
  uniq=$(python3 "$CATALOG" stats 2>/dev/null | awk -F': ' '/unique_canonical_ids/{print $2}')
  log "catalog now: $uniq unique repos"
}

# is a farm process already running? (JARVIS's pane, or a prior re-kick) — respect the split: only ONE farmer
farm_running() {
  pgrep -f "farm_github_urls.mjs" >/dev/null 2>&1
}

run_pass() {
  if farm_running; then
    # JARVIS's farm (or another) is already farming — do NOT launch a duplicate. Just ingest what's landed.
    log "farm already running (pid $(pgrep -f farm_github_urls.mjs | tr '\n' ' ')) — NOT re-kicking; ingest-only this pass"
    ingest_shards
    return
  fi
  # farm is NOT running and census incomplete => it stalled. NOW we own the re-kick.
  log "no farm running + census incomplete — re-kicking the resumable farm"
  node "$FARM_SCRIPT" >>"$LOG" 2>&1
  log "farm pass exited (rc=$?) — ingesting shards"
  ingest_shards
}

if [ "${1:-}" = "--once" ]; then
  run_pass
  census_complete && log "CENSUS COMPLETE (all 7 tiers)" || log "census incomplete — re-run later"
  exit 0
fi

# loop mode: run, ingest, sleep past budget reset, repeat until complete
PASS=0
while true; do
  PASS=$((PASS+1))
  log "=== rekick pass #$PASS ==="
  run_pass
  if census_complete; then
    log "CENSUS COMPLETE (all 7 tiers) after $PASS passes — stopping watcher"
    break
  fi
  if farm_running; then
    # JARVIS still farming — just poll/ingest every ~10min, don't sleep a full hour
    log "farm still running — ingest-poll in 10m"
    sleep 600
  else
    # farm stalled (budget hit) — sleep past the hourly GraphQL reset before re-kicking
    log "farm stalled — sleeping 65m until budget reset, then re-kick"
    sleep 3900
  fi
done
