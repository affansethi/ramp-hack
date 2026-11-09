"""Test-wide setup: startup mode guards require these to be set
(docs/architecture.md §1). Tests always run in synthetic + replay."""

import os

os.environ.setdefault("DATA_MODE", "synthetic")
os.environ.setdefault("ENGINE_MODE", "mock_reviewed_ruleset")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("LLM_MODE", "replay")
