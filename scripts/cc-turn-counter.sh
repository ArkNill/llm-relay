#!/bin/bash
# CC Stop hook: real-time turn counter + terminal identification
# - Writes turn status to ${CLAUDE_CONFIG_DIR:-~/.claude}/turn-status (side-channel, zero LLM context pollution)
# - POSTs terminal info (TTY, PID, term name) to llm-relay for display page
# Fallback: transcript grep if API unreachable.
set -euo pipefail

# Disable via env var
[ "${CC_TURN_COUNTER_DISABLED:-0}" = "1" ] && exit 0

# Status file location — honor CLAUDE_CONFIG_DIR for alternate config dirs.
# Falls back to ~/.claude when unset (stock).
STATUS_DIR="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
STATUS_FILE="${STATUS_DIR}/turn-status"

# === Phase 1: Parse stdin JSON ===
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')

[ -z "$SESSION_ID" ] && exit 0

# === Phase 2: Collect terminal info ===
# Script's own controlling TTY (inherited from CC)
MY_TTY=$(ps -o tty= -p $$ 2>/dev/null | tr -d ' ' || echo "")

CC_PID=""
TTY=""

if [ -n "$MY_TTY" ] && [ "$MY_TTY" != "?" ]; then
    # Find the claude process on the same TTY — robust against intermediate shells
    CC_PID=$(ps -eo pid=,tty=,comm= --no-headers 2>/dev/null | \
        awk -v tty="$MY_TTY" '$2==tty && $3=="claude" {print $1; exit}' | tr -d ' ')
    TTY="/dev/$MY_TTY"
fi

# Fallback: walk up from $PPID to find claude ancestor
if [ -z "$CC_PID" ]; then
    _walk_pid="$PPID"
    for _i in 1 2 3 4 5 6; do
        [ -z "$_walk_pid" ] || [ "$_walk_pid" = "0" ] || [ "$_walk_pid" = "1" ] && break
        _cmd=$(ps -o comm= -p "$_walk_pid" 2>/dev/null | tr -d ' ')
        if [ "$_cmd" = "claude" ]; then
            CC_PID="$_walk_pid"
            _t=$(ps -o tty= -p "$_walk_pid" 2>/dev/null | tr -d ' ')
            [ -n "$_t" ] && [ "$_t" != "?" ] && TTY="/dev/$_t"
            break
        fi
        _walk_pid=$(ps -o ppid= -p "$_walk_pid" 2>/dev/null | tr -d ' ')
    done
fi

# Walk up parent chain from CC to find terminal emulator (skip shells/login)
TERM_PID=""
TERM_NAME=""
if [ -n "$CC_PID" ]; then
    _walk_pid=$(ps -o ppid= -p "$CC_PID" 2>/dev/null | tr -d ' ')
    for _i in 1 2 3 4 5; do
        [ -z "$_walk_pid" ] || [ "$_walk_pid" = "0" ] || [ "$_walk_pid" = "1" ] && break
        _cmd=$(ps -o comm= -p "$_walk_pid" 2>/dev/null | tr -d ' ')
        case "$_cmd" in
            bash|zsh|fish|sh|dash|login|su|sudo)
                _walk_pid=$(ps -o ppid= -p "$_walk_pid" 2>/dev/null | tr -d ' ')
                continue
                ;;
            "")
                break
                ;;
            *)
                TERM_PID="$_walk_pid"
                TERM_NAME="$_cmd"
                break
                ;;
        esac
    done
fi

# POST terminal info to llm-relay (fire-and-forget, short timeout)
API_PORT="${CC_TURN_API_PORT:-8083}"
if command -v curl >/dev/null 2>&1; then
    TERM_JSON=$(jq -n \
        --arg sid "$SESSION_ID" \
        --arg tty "$TTY" \
        --arg cc_pid "$CC_PID" \
        --arg term_pid "$TERM_PID" \
        --arg term_name "$TERM_NAME" \
        '{session_id: $sid, tty: $tty, cc_pid: ($cc_pid | tonumber? // null), term_pid: ($term_pid | tonumber? // null), term_name: $term_name}' 2>/dev/null)
    if [ -n "$TERM_JSON" ]; then
        curl -s --connect-timeout 0.3 --max-time 0.5 -X POST \
            -H "Content-Type: application/json" \
            -d "$TERM_JSON" \
            "http://localhost:${API_PORT}/api/v1/session-terminal" >/dev/null 2>&1 || true
    fi
fi

# === Phase 3: Query llm-relay API for turn count ===
TURNS=""
ZONE=""
MESSAGE=""
API_OK=false

RESP=$(curl -s --connect-timeout 0.3 --max-time 0.5 \
    "http://localhost:${API_PORT}/api/v1/turns/${SESSION_ID}" 2>/dev/null) && {
    TURNS=$(echo "$RESP" | jq -r '.turns // empty')
    ZONE=$(echo "$RESP" | jq -r '.zone // empty')
    MESSAGE=$(echo "$RESP" | jq -r '.message // empty')
    [ -n "$TURNS" ] && API_OK=true
}

# === Phase 4: Fallback — transcript counting ===
FALLBACK=""
if [ "$API_OK" = false ] && [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
    TURNS=$(grep -c '"type":"assistant"' "$TRANSCRIPT_PATH" 2>/dev/null || echo "0")
    FALLBACK=" (local)"
    if [ "$TURNS" -lt 200 ]; then ZONE="green"
    elif [ "$TURNS" -lt 250 ]; then ZONE="yellow"
    elif [ "$TURNS" -lt 300 ]; then ZONE="orange"
    else ZONE="red"; fi
fi

[ -z "$TURNS" ] && exit 0

# === Phase 5: Format ===
RED_THRESHOLD="${CC_TURN_RED:-300}"

case "$ZONE" in
    green)  ICON="G" ;;
    yellow) ICON="Y" ;;
    orange) ICON="O" ;;
    red)    ICON="R" ;;
    *)      ICON="-" ;;
esac

DURATION=""
if [ "$API_OK" = true ]; then
    DUR_S=$(echo "$RESP" | jq -r '.duration_s // 0')
    if [ "$DUR_S" != "0" ] && [ "$DUR_S" != "null" ]; then
        DUR_M=$(echo "$DUR_S" | awk '{printf "%d", $1/60}')
        if [ "$DUR_M" -ge 60 ]; then
            DUR_H=$(echo "$DUR_M" | awk '{printf "%d", $1/60}')
            DUR_RM=$(echo "$DUR_M $DUR_H" | awk '{printf "%d", $1-$2*60}')
            DURATION=" ${DUR_H}h${DUR_RM}m"
        else
            DURATION=" ${DUR_M}m"
        fi
    fi
fi

TTY_SHORT="${TTY#/dev/}"
TTY_LABEL=""
[ -n "$TTY_SHORT" ] && TTY_LABEL=" ${TTY_SHORT}"

LINE="[${ICON}] Turn ${TURNS}/${RED_THRESHOLD}${DURATION}${TTY_LABEL}${FALLBACK}"

# === Phase 6: Write to status file ===
SID_SHORT="${SESSION_ID:0:8}"
TIMESTAMP=$(date '+%H:%M')

TMPFILE=$(mktemp "${STATUS_DIR}/.turn-status.XXXXXX")

if [ -f "$STATUS_FILE" ]; then
    grep -v "^${SID_SHORT}" "$STATUS_FILE" > "$TMPFILE" 2>/dev/null || true
fi
echo "${SID_SHORT} ${LINE} [${TIMESTAMP}]" >> "$TMPFILE"

sort -o "$TMPFILE" "$TMPFILE"
mv -f "$TMPFILE" "$STATUS_FILE"

if [ -n "$MESSAGE" ] && [ "$MESSAGE" != "null" ]; then
    echo "  >> ${SID_SHORT}: ${MESSAGE}" >> "$STATUS_FILE"
fi

exit 0
