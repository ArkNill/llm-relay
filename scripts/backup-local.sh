#!/usr/bin/env bash
# llm-relay local source code backup — 3-layer protection
# Layer 1: git bundle → local disk
# Layer 2: git bundle → DGX1
# Layer 3: git bundle → DGX2
#
# Usage:
#   ./scripts/backup-local.sh          # full backup to all 3 layers
#   ./scripts/backup-local.sh local    # layer 1 only (fast)
#
# Cron (daily 04:17):
#   17 4 * * * /home/hmjhp/GitHub/llm-relay/scripts/backup-local.sh >> /home/hmjhp/.llm-relay/backup.log 2>&1

set -euo pipefail

REPO_DIR="$HOME/GitHub/llm-relay"
BACKUP_DIR="$HOME/.llm-relay/backups"
DGX1_HOST="dgx"
DGX2_HOST="dgx2"
REMOTE_DIR="backups/llm-relay"
KEEP_LOCAL=7    # keep last N local bundles
KEEP_REMOTE=14  # keep last N remote bundles

TS=$(date +%Y%m%d-%H%M%S)
BUNDLE_NAME="llm-relay-${TS}.bundle"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── Layer 1: local git bundle ──
log "Layer 1: creating git bundle..."
mkdir -p "$BACKUP_DIR"
cd "$REPO_DIR"

# Bundle ALL refs (branches, tags, stashes)
git bundle create "$BACKUP_DIR/$BUNDLE_NAME" --all
BUNDLE_SIZE=$(du -h "$BACKUP_DIR/$BUNDLE_NAME" | cut -f1)
log "Layer 1: $BUNDLE_NAME ($BUNDLE_SIZE) saved to $BACKUP_DIR"

# Verify bundle integrity
if git bundle verify "$BACKUP_DIR/$BUNDLE_NAME" >/dev/null 2>&1; then
    log "Layer 1: bundle verification OK"
else
    log "ERROR: bundle verification FAILED — aborting remote sync"
    exit 1
fi

# Rotate old local bundles (keep last N)
cd "$BACKUP_DIR"
ls -1t llm-relay-*.bundle 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm -f
LOCAL_COUNT=$(ls -1 llm-relay-*.bundle 2>/dev/null | wc -l)
log "Layer 1: $LOCAL_COUNT bundles retained (max $KEEP_LOCAL)"

# Stop here if "local" mode
if [ "${1:-all}" = "local" ]; then
    log "Layer 1 only — done."
    exit 0
fi

# ── Layer 2: DGX1 remote ──
log "Layer 2: syncing to DGX1..."
if ssh -o ConnectTimeout=5 -o BatchMode=yes "$DGX1_HOST" "mkdir -p $REMOTE_DIR" 2>/dev/null; then
    scp -q "$BACKUP_DIR/$BUNDLE_NAME" "$DGX1_HOST:$REMOTE_DIR/$BUNDLE_NAME"
    # Rotate remote
    ssh "$DGX1_HOST" "cd $REMOTE_DIR && ls -1t llm-relay-*.bundle 2>/dev/null | tail -n +$((KEEP_REMOTE + 1)) | xargs -r rm -f" 2>/dev/null
    REMOTE1_COUNT=$(ssh "$DGX1_HOST" "ls -1 $REMOTE_DIR/llm-relay-*.bundle 2>/dev/null | wc -l")
    log "Layer 2: DGX1 OK ($REMOTE1_COUNT bundles)"
else
    log "WARNING: DGX1 unreachable — Layer 2 skipped"
fi

# ── Layer 3: DGX2 remote ──
log "Layer 3: syncing to DGX2..."
if ssh -o ConnectTimeout=5 -o BatchMode=yes "$DGX2_HOST" "mkdir -p $REMOTE_DIR" 2>/dev/null; then
    scp -q "$BACKUP_DIR/$BUNDLE_NAME" "$DGX2_HOST:$REMOTE_DIR/$BUNDLE_NAME"
    # Rotate remote
    ssh "$DGX2_HOST" "cd $REMOTE_DIR && ls -1t llm-relay-*.bundle 2>/dev/null | tail -n +$((KEEP_REMOTE + 1)) | xargs -r rm -f" 2>/dev/null
    REMOTE2_COUNT=$(ssh "$DGX2_HOST" "ls -1 $REMOTE_DIR/llm-relay-*.bundle 2>/dev/null | wc -l")
    log "Layer 3: DGX2 OK ($REMOTE2_COUNT bundles)"
else
    log "WARNING: DGX2 unreachable — Layer 3 skipped"
fi

log "Backup complete: $BUNDLE_NAME ($BUNDLE_SIZE)"
log "  Local: $BACKUP_DIR ($LOCAL_COUNT bundles)"
log "  DGX1:  ~/$REMOTE_DIR"
log "  DGX2:  ~/$REMOTE_DIR"
