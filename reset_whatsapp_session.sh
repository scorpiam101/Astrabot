#!/usr/bin/env bash
# Replaces the WhatsApp session with a fresh one. The old session is BACKED UP, never deleted.
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

say "This will unlink this app from WhatsApp on the phone side of the bridge."
say "Your old session files will be moved to a backup folder (not deleted)."
say "Your contacts, conversations and settings are NOT affected."
if [ ! -d "$AUTH_DIR" ] || [ -z "$(ls -A "$AUTH_DIR" 2>/dev/null)" ]; then
  say "There is no saved WhatsApp session. Nothing to reset. Just pair from the dashboard."
  exit 0
fi
printf 'Type YES (capital letters) to continue: '
read -r answer
[ "$answer" = "YES" ] || { say "Cancelled. Nothing was changed."; exit 0; }

stop_service bridge "$BRIDGE_MARK" "WhatsApp bridge"
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$DATA_DIR/backups/wa_auth-$stamp"
mkdir -p "$DATA_DIR/backups"; chmod 700 "$DATA_DIR/backups"
mv "$AUTH_DIR" "$backup" || { bad "Could not back up the old session. Nothing was changed."; exit 1; }
mkdir -p "$AUTH_DIR"; chmod 700 "$AUTH_DIR"
ok "Old session backed up to: $backup"
say "To restore it later: ./stop.sh, then move that folder back to $AUTH_DIR (after moving the new one away)."
say "Starting again..."
exec "$APP_DIR/start.sh"
