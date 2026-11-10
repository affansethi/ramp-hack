# Content verification (owner review or agent verification)

Status: active from 2026-11-10. Tax content activates through **either** an owner review **or** the agent verification protocol below. Neither owner is required: an item an owner has not reviewed can still activate through agent verification. Unreviewed, unverified items stay `candidate`, which the app treats as inactive (needs-human-review or a hold, never a guessed amount).

## What it covers and the active values

| Item | File | Active when |
|---|---|---|
| Rule entries, timing basis | `rules/entries.yaml` | `review_status: reviewed` + `reviewed_by` + `reviewed_at` |
| Rate records | `rules/rates.yaml` | `review_status: reviewed` + `reviewed_by` |
| Rounding basis | `rules/rounding.yaml` | `review_status: reviewed` + `reviewed_by` + `source_url`, mode `nearest_cent_half_up` only |
| Required-fact catalog (S3) | `rules/catalog.yaml` | `status: signed` + `signed_off_by` |
| Filing-basis text for drafts | `rules/filing-basis.txt` (one `[section]` per route) | written by an owner, or agent-verified with evidence |
| Golden expected results | `fixtures/goldens/` | `status: signed` + `signed_off_by` |

`PLACEHOLDER:` ids never activate, whoever reviewed them.

## Route A: owner review

An owner (AF) checks the item against its primary source and sets `reviewed_by: AF` (or `signed_off_by: AF`) and the date, as in PRs #96 (rules, rates, rounding) and #98 (filing-basis text). No evidence file is required for an owner-reviewed item. Owner-reviewed items still get an agent evidence audit (see below).

## Route B: agent verification

An item activates with `reviewed_by: agent-verified` (or `signed_off_by: agent-verified`) only when all of these hold:

1. **Primary sources only.** Every factual claim (treatment, effective dates, timing basis, rate, rounding rule, required facts, filing route and deadline) cites an official source: statute, regulation, or published guidance or rate table of the taxing authority, on a domain listed in `rules/evidence/allowlist.yaml`. Secondary sites may help find a source but are never cited.
2. **Evidence on file.** `rules/evidence/<item>.yaml` records per claim: URL, authority type, pinpoint citation, the exact quoted passage, retrieval date, and a text snapshot under `rules/evidence/snapshots/` with its SHA-256. The quote must appear in the snapshot.
3. **Research pass.** A research agent gathers sources, writes the evidence file and proposes values, with a `role: research` attestation (lane, model, issue, date).
4. **Blind verification pass.** A second agent on a different lane and model family answers the same questions from primary sources before reading the research conclusions, writes `rules/evidence/verification/<item>.<lane>.yaml`, then compares and adds a `role: verify`, `blind: true` attestation (`agree` or `disagree`).
5. **Agreement.** Every claim matches exactly (dates, decimal strings, enums; text in substance). One mismatch keeps the item `candidate`.
6. **Gate.** `make verify-content` (inside `make check`, so CI's `test` job) fails if an agent-verified item lacks the evidence, the two attestations from distinct lanes and model families, an allowlisted source per claim, or a quote matching its snapshot.
7. **Merge review.** Content PRs are `risk:high`, so they get the Astra review before auto-merge.

Item ids for evidence files: the rule id; `rate-<JUR>-<location_code>-<quarter>`; `rounding-<id>`; `catalog-<CATEGORY>`; `basis-<section>`; `golden-<fixture id>`.

## Evidence audit of owner-reviewed items

Agents add evidence files for owner-reviewed items too (research plus blind verification, same format). If a primary source contradicts an owner-reviewed value, the agent does not change it: it comments on the issue with the source and quote and opens a correction issue. The owner, or an agent-verified correction, settles it.

## Goldens

Expected results are derived by hand from the fixture documents and the active rules, rates and rounding basis, independently by two agents on different model families (or written by an owner). Nobody runs the app, engine or tests to produce an expected value. Both derivations must match string for string before `signed_off_by: agent-verified`. NHR and hold goldens carry no amount. CI compares app output with active goldens; a mismatch is a bug, never a reason to edit the golden to match.

## Filing-basis text

Each section cites the official refund or reporting route. Strings never say a letter was sent, never say filed or paid unless the same phrase says simulated, and are never presented as filing advice.

## Disagreements

A mismatch or a missing official source leaves an agent-verified item `candidate`; the verifier comments with both answers and sources. The epic lead may run one tie-break pass on a third lane. If that does not settle it, the item stays inactive and the demo shows NHR for it.
