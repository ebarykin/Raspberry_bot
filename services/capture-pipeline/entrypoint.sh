#!/bin/bash
set -eo pipefail
exec python3 -u /app/main.py "$@"
