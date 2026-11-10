# rules/

Tax content YAML and text: rule entries (`effective_from/to`, `timing_basis`,
`primary_source_url`, `review_status`, `reviewed_by/at`), rate tables, the
rounding basis (`rounding.yaml`), the S3 required-fact catalog, and the
filing-basis strings for draft letters (`filing-basis.txt`).

An item activates through `docs/content-verification.md`: owner review
(`reviewed_by: AF`) or agent verification (`reviewed_by: agent-verified`,
with evidence under `evidence/`). `candidate` and `PLACEHOLDER:` entries stay
inactive and route NHR.
