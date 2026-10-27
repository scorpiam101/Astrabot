#!/usr/bin/env bash
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/stop.sh"
exec "$DIR/start.sh"
