# Content verification protocol (replaces owner sign-off)

Status: active from 2026-11-10. This replaces every "RJ drafts, AF signs" step in the specs, `AGENTS.md`, `rules/README.md`, `fixtures/README.md` and decision D9. Where an older passage still says an owner signs, read it as "verified under this protocol".

## What it covers

Tax content that the app treats as authoritative:

| Item | File | Activation field |
|---|---|---|
| Required-fact catalog (S3) | `rules/catalog.yaml` | `status: verified` |
| Rule entries, timing basis | `rules/entries.yaml` | `review_status: verified` |
| Rate records | `rules/rates.yaml` | `review_status: verified` |
| Rounding basis | `rules/entries.yaml` (a rule field or a dedicated entry) | `review_status: verified` |
| Golden expected results | `fixtures/goldens/` | `status: verified` |
| Filing-basis text for draft letters | `rules/filing_basis.yaml` | `status: verified` |

Nothing here is legal advice, and the demo stays synthetic. The protocol exists so that what the app shows is traceable to official primary sources and was checked by two independent agents.

## The rule

An item becomes active only when all of these hold. Anything short of that stays `candidate`, which the app already treats as inactive: needs-human-review (NHR) or a hold, never a guessed amount.

1. **Primary sources only.** Every factual claim (treatment, effective dates, timing basis, rate, rounding rule, required facts, filing route and deadline) cites at least one official source: statute, regulation, or published guidance or rate table from the taxing authority. Allowed domains are listed in `rules/evidence/allowlist.yaml` (for example `app.leg.wa.gov`, `dor.wa.gov`, `cdtfa.ca.gov`, `leginfo.legislature.ca.gov`, `tax.ny.gov`, `nysenate.gov`). Blogs, vendor sites, AI answers and secondary summaries may help find a source but are never cited.
2. **Evidence on file.** `rules/evidence/<item>.yaml` records, per claim: URL, authority type, pinpoint citation (section, page or table row), the exact quoted passage, retrieval date, and a text snapshot of the page under `rules/evidence/snapshots/` with its SHA-256. The quote must appear in the snapshot.
3. **Research pass.** A research agent gathers the sources, writes the evidence file, and proposes the item's values. It records an attestation (`role: research`, lane, model, issue, date).
4. **Blind verification pass.** A second agent, on a different lane and a different model family, answers the same questions from the primary sources before it reads the research agent's conclusions, writes its own answers to `rules/evidence/verification/<item>.<lane>.yaml`, then compares. It records an attestation (`role: verify`, `blind: true`, `agree` or `disagree`, notes).
5. **Agreement.** Every claim must match exactly (dates, decimal strings, enum values; text claims must agree in substance). A single mismatch keeps the item `candidate`.
6. **Gate.** `make verify-content` (run inside `make check`, so CI's `test` job) fails if an active item lacks the evidence, the two attestations from distinct lanes and model families, an allowlisted source per claim, or a quote that matches its snapshot. Rates also need the official table for the item's quarter.
7. **Merge review.** Content PRs are `risk:high`, so the dispatcher's Astra review (GPT-6 Astra) checks the evidence before auto-merge.

## Disagreements and gaps

- A mismatch, a missing official source, or a source that does not clearly cover the demo period leaves the item `candidate`. The verifier comments on the issue with both answers and the sources.
- The epic lead or a follow-up issue may run one tie-break pass with a third lane. If that still does not settle it, the item stays inactive and the demo shows NHR for it. That is a correct outcome, not a failure.
- `PLACEHOLDER:` ids never activate, whatever their evidence says.

## Goldens

- Expected results are derived by hand from the fixture documents and the verified rules and rates, independently by the research and verification agents. Neither may run the app, the engine or its tests to produce an expected value.
- Both derivations must match string-for-string before the golden is `verified`. NHR and hold goldens carry no amount.
- CI then compares app output with the verified goldens. A mismatch is a bug in the app or in the golden, never a reason to edit the golden to match.

## Filing-basis text

- Each string cites the official refund or reporting route (statute or agency instructions) and the deadline that source states.
- Strings never say a letter was sent, or that anything was filed or paid, unless the same phrase says simulated, and they are never presented as filing advice.

## Who does what

| Step | Who |
|---|---|
| Research | A builder lane with network access (default Cursor/Grok) |
| Blind verification | A different lane and model family (default Codex) |
| Tie-break, if needed | A third lane chosen by the epic lead |
| Gate | CI (`make verify-content`) |
| Merge | Dispatcher, after CI and an approving Astra review |

The owners (RJ and AF) have no signing step. They may still veto any item by setting it back to `candidate` with a reason.
