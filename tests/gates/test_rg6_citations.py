"""RG-6: every supported citation resolves to a stored rule with a source URL.

No URL is fetched in CI; the check is against the rules the engine loaded.
"""

from fastapi.testclient import TestClient

from app.domain.ruleset import load_ruleset, rule_is_active
from tests.gates.conftest import _run


def test_every_supported_citation_resolves_to_a_stored_sourced_rule(
    signed_client: TestClient,
) -> None:
    client = signed_client
    ruleset = load_ruleset(client.app.state.ctx.rules_dir)
    rules = {rule.id: rule for rule in ruleset.rules}
    seen = 0
    for scenario in ("case1", "case2", "case3", "case4"):
        created = _run(client, scenario, f"rg6-{scenario}")
        detail = client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
        for det in detail["determinations"]:
            if det["label"] != "ENGINE_SUPPORTED":
                assert det["rule_source_url"] is None
                continue
            seen += 1
            rule = rules.get(det["rule_reference"])
            assert rule is not None, det["rule_reference"]
            assert rule_is_active(rule)
            assert rule.primary_source_url.startswith("https://")
            assert det["rule_source_url"] == rule.primary_source_url
    assert seen >= 3
