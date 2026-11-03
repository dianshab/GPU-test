# Offline test suite for the remote Pearl miner controller.
#
# Everything here runs without a GPU, a pool account or a rented instance:
# tests/mock_krig.py impersonates the miner's CLI and telemetry surface, and
# lifecycle tests use the test-only "spawn" identity backend.
#
# Usage:  ./run-tests.sh
# Exit code 0 means every check passed.

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
python=${PYTHON:-python3}

cd "$root"

echo "== python syntax =="
"$python" -m py_compile scripts/prl_miner.py tests/mock_krig.py tests/test_units.py tests/test_lifecycle.py

echo "== shell syntax (scripts/prl.sh) =="
bash -n scripts/prl.sh

echo "== unit tests =="
"$python" -m unittest tests.test_units -v

echo "== lifecycle tests (mock miner) =="
"$python" -m unittest tests.test_lifecycle -v

echo "ALL TESTS PASSED (Windows host reader: run run-tests.ps1 for the PowerShell checks)"
