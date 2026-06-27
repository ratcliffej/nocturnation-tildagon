"""CI guard for nocturnation/protocol/_generated.py.

Re-runs the protocol-constants generator (in the sibling Docs repo)
against its YAML SOT and asserts the checked-in _generated.py is
byte-identical. Catches:

  * Hand-edits to _generated.py (it is auto-output; touch the YAML
    and run tools/regen_constants.sh).
  * SOT edits that were not followed by a regen.
  * Generator changes that bump output formatting (regen + commit).

Cross-repo path assumption: nocturnation-docs sits at ../../Docs
relative to this firmware repo. Matches the user's local layout
(NocturNation/{Tildagon, StickC, Docs}/) and is documented in
tools/regen_constants.sh.
"""

import pathlib
import subprocess
import sys
import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT.parent.parent / "Docs"
GENERATOR = DOCS_ROOT / "tools" / "gen_protocol_constants.py"
GENERATED = REPO_ROOT / "nocturnation" / "protocol" / "_generated.py"


def _docs_available():
    return GENERATOR.is_file() and (DOCS_ROOT / "protocol" / "constants.yaml").is_file()


@pytest.mark.skipif(not _docs_available(),
                    reason="sibling Docs repo not present (CI: clone alongside)")
def test_generated_module_matches_sot():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--py"],
        capture_output=True, text=True, check=True,
    )
    expected = result.stdout
    actual = GENERATED.read_text()
    assert actual == expected, (
        "nocturnation/protocol/_generated.py is out of sync with "
        "Docs/protocol/constants.yaml. Run tools/regen_constants.sh "
        "and commit the result."
    )
