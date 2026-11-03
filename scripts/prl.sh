#!/usr/bin/env bash
# ESI Pearl (PRL) remote miner - thin entry point.
#
# Everything real lives in prl_miner.py (Python 3.8+, standard library only).
# This wrapper exists so operators can type ./prl.sh start instead of a long
# python command.
#
# Usage:
#   ./prl.sh prepare --allow-download
#   ./prl.sh check
#   ./prl.sh start
#   ./prl.sh status
#   ./prl.sh stop
#   ./prl.sh logs --source miner --lines 60
#
# Environment:
#   PRL_HOME   force the state root (default: the bundle root, else ~/esi-pearl)
#   PYTHON     interpreter to use (default: python3)

set -u

here=$(cd "$(dirname "$0")" && pwd)
python=${PYTHON:-python3}

if ! command -v "$python" >/dev/null 2>&1; then
    echo "error: '$python' not found. Install Python 3.8+ (e.g. apt-get install -y python3) or set PYTHON=/path/to/python3." >&2
    exit 2
fi

script="$here/prl_miner.py"
if [ ! -f "$script" ]; then
    echo "error: prl_miner.py not found next to this wrapper ($here)" >&2
    exit 2
fi

# PRL_HOME is passed through explicitly only when the operator set it; otherwise
# prl_miner.py resolves the bundle root next to this wrapper (or ~/esi-pearl).
if [ -n "${PRL_HOME:-}" ]; then
    exec "$python" "$script" --home "$PRL_HOME" "$@"
fi

exec "$python" "$script" "$@"
