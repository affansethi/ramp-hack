# rules/

Owner-authored YAML: rule entries (`effective_from/to`, `timing_basis`,
`primary_source_url`, `review_status`, `reviewed_by/at`), the S3
required-fact catalog, and rate tables.

Agents implement the evaluator only. Agents never author rule entries,
rates, or the catalog's substance: RJ drafts, AF signs. Only `reviewed`
entries activate; `PLACEHOLDER:` entries stay inactive and route NHR.
