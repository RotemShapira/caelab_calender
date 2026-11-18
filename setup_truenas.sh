#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/lab-scheduler"
exec bash ./setup_truenas.sh "$@"
