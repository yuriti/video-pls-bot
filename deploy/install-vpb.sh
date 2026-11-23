#!/bin/sh
set -eu
VPB_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$VPB_PROJECT_DIR"
exec "$VPB_PROJECT_DIR/.venv/bin/python" -m tools.vpb.bootstrap "$@"
