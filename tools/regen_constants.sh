#!/usr/bin/env bash
# Regenerate nocturnation/protocol/_generated.py from the protocol-
# constants SOT in the sibling nocturnation-docs repo.
#
# Run from the firmware repo root:
#   ./tools/regen_constants.sh
#
# The accompanying pytest (tests/test_protocol_constants_generated.py)
# re-runs the generator and fails if the checked-in file drifts from
# the SOT, so committing without running this script first is caught
# in CI.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCS_ROOT="$(cd "$REPO_ROOT/../../Docs" && pwd)"

python3 "$DOCS_ROOT/tools/gen_protocol_constants.py" --py \
  > "$REPO_ROOT/nocturnation/protocol/_generated.py"

echo "Regenerated nocturnation/protocol/_generated.py from $DOCS_ROOT/protocol/constants.yaml"
