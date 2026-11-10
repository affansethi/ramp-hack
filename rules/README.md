# rules/

Verified content YAML: rule entries (`effective_from/to`, `timing_basis`,
`primary_source_url`, `review_status`), the S3 required-fact catalog, rate
tables, and (later) filing-basis text.

Values change only under `docs/content-verification.md`: official primary
sources, an evidence file under `rules/evidence/`, a research pass and a
blind verification pass by a different lane and model family, and the
`make verify-content` gate. Only verified entries activate; `candidate` and
`PLACEHOLDER:` entries stay inactive and route NHR. No owner signs.
