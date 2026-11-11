"""Run the real app on temp-signed copies of rules/ for the browser e2e test.

The repo's rules/ is never changed: copies are marked signed in a temp
directory, exactly like the API tests' _activate helper.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "8765"
    tmp = Path(tempfile.mkdtemp(prefix="taxleak-e2e-"))
    rules = tmp / "rules"
    rules.mkdir()
    for path in (ROOT / "rules").iterdir():
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if path.name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
                text = text.replace(
                    "review_status: candidate", "review_status: reviewed"
                )
                text = text.replace("status: candidate", "status: signed")
                text = text.replace("reviewed_by:\n", "reviewed_by: AF\n")
                text = text.replace("signed_off_by:\n", "signed_off_by: AF\n")
                text = text.replace("signed_off_by: null", "signed_off_by: AF")
                text = text.replace("reviewed_by: null", "reviewed_by: AF")
            (rules / path.name).write_text(text, encoding="utf-8")
    os.environ.update(
        DATA_MODE="synthetic",
        ENGINE_MODE="mock_reviewed_ruleset",
        DEMO_MODE="true",
        LLM_MODE="replay",
        RULES_DIR=str(rules),
        DATABASE_URL="sqlite:///" + (tmp / "e2e.db").as_posix(),
        RAMP_WEBHOOK_SECRET="e2e-secret",
    )
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=int(port), log_level="warning")


if __name__ == "__main__":
    main()
