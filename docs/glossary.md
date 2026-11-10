# Tax Leak — Glossary

Use this page for the domain words shared by the Design Spec (product rules) and Technical Spec v2, the build plan under the heading "Tax Leak Technical Spec v2 (Lean)". Where they disagree, the Design Spec wins. The v1.5 text above that heading is superseded and is not a source for these definitions. Money on the wire is a decimal string (`{"value": "10.00", "currency": "USD"}`), never a binary float; engine amounts and rule references are copied exactly, and every environment uses synthetic data only.

## Holds, review, and labels

**Hold.** A block placed before any engine call because a required fact is missing or unconfirmed. The only hold reasons are `MISSING_REQUIRED_FACT` and `UNCONFIRMED_REQUIRED_FACT`. A hold covers the whole item: no engine call for any line or jurisdiction, no approval, no tax amount, estimate, or zero, and no entry in a total or workpaper. Partial holds are outside the demo. Nobody can release a hold by hand. The only exit is a sourced fact that the catalog check admits, followed by a full re-check. A hold is not a silent zero.

**Missing required fact.** No admitted fact fills the field. A rejected proposal, "I don't know," a redirect, or silence counts as no fact.

**Unconfirmed required fact.** A value is present, but the source is not an accepted one, it conflicts with another fact, or it is incomplete. A state split that does not cover every seat is incomplete. If extracted totals or tax differ from the Ramp charge (`entity_amount`) or Ramp `ocr.taxes` by more than one cent, the fact stays unconfirmed and the item holds.

**Question.** One plain-language ask to the spend owner, naming the vendor, amount, and date. Only a sourced answer can admit a fact and re-run the check. "I don't know," naming someone else, silence, or a timeout keeps the hold.

**Needs human review (NHR).** The second outcome, used when the system cannot produce an engine-supported result. Facts may already be complete. NHR carries no amount, cannot be approved, and never enters a total or workpaper. The reviewer may `leave_open` or `send_to_advisor`. No one gives it a treatment, a label, or an amount. NHR is not a hold: a hold is a fact gap before the engine; NHR is the route when category, jurisdiction, or rule coverage cannot support a result.

**NHR reason.** Why the result is needs-human-review. The closed set is `NO_SUPPORTED_CATEGORY`, `UNDECLARED_JURISDICTION`, `ENGINE_UNSUPPORTED`, and `RULE_TRANSITION_UNRESOLVED`.

- `NO_SUPPORTED_CATEGORY` — the category is outside the demo's coverage.
- `UNDECLARED_JURISDICTION` — a confirmed location, or tax charged, names a state other than CA, WA, or NY. The whole item goes to NHR. No state is dropped or treated as zero.
- `ENGINE_UNSUPPORTED` — the engine reports the case unsupported. An unreviewed or placeholder entry uses this reason when the gap is not an unresolved transition.
- `RULE_TRANSITION_UNRESOLVED` — effective-date transition handling is not reviewed. No amount and no approval.

A missing rule reference on an otherwise supported response is a determination error, not an NHR reason. An empty jurisdiction set holds; it is not `UNDECLARED_JURISDICTION`.

**Determination error.** A technical failure of the engine call: timeout, failed call, malformed response, or a supported response with no rule reference. It has no amount and no label. It is retried and listed on its own. An error is not zero, and it does not become NHR by default.

**Label.** The only two result labels, hard rule H1. `ENGINE_SUPPORTED` means a reviewed rule produced the result ("reviewed rule / engine-supported"). `NEEDS_HUMAN_REVIEW` means needs human review. There is no third label and no tax-result confidence score. A determination error has neither label. Fact confidence may be shown; it never decides tax or releases a hold.

**Approve.** A named person's decision on one `ENGINE_SUPPORTED` result at its current version. It records who approved and when. The approval path is `LIABILITY` for a liability, `REFUND_PURSUIT` for a refund candidate, or `CHARGED_CORRECTLY` for no adjustment. For a refund, the path string is `REFUND_PURSUIT` and the direction string stays `REFUND_CANDIDATE`. Approve is refused for a hold, an NHR result, a determination error, a superseded version, and a version already decided. The command refuses; hiding the button is not enough. No system, model, threshold, or bulk action approves. Approval does not change the engine's treatment, direction, or amount.

**Reject.** A named person's decision that records a reason and excludes the result from totals and the workpaper. Reject does not invent a different treatment, and it does not mean the purchase had no tax. A new result requires a fact change and a re-run.

**Flag.** The system placing a determined item on the review queue. Intake and flagging do not change an approved total. A flag is not an approval.

**Review queue.** Three lists, kept apart: ready for review, needs human review, and held. The held list shows the missing fact, who was asked, when, and whether they replied.

## Direction and ledger lines

**Direction.** For an `ENGINE_SUPPORTED` result only: `LIABILITY`, `REFUND_CANDIDATE`, or `CHARGED_CORRECTLY`. The engine's direction is copied. NHR and errors have no direction. A person cannot set direction. Direction is not a label.

**Derived direction.** When the engine omits direction, it is derived by comparing the engine's expected tax with the admitted tax charged, and `derived_direction` is set to true. Expected tax above tax charged is `LIABILITY`; below is `REFUND_CANDIDATE`; equal is `CHARGED_CORRECTLY`. The comparison is logged. The amount still comes from the engine. Deriving direction does not recompute it.

**No adjustment (charged correctly).** Direction `CHARGED_CORRECTLY`: tax was charged correctly. When direction is derived, that means expected tax equals the admitted tax charged. The result still needs named approval, with approval path `CHARGED_CORRECTLY`. No amount enters any total. The workpaper may carry an informational count. A person cannot set this outcome as a treatment.

**Liability line.** An approved use-tax amount the buyer owes, copied from the engine onto one line in one jurisdiction, and eligible for that jurisdiction's workpaper. It exists only after a named person approves a `LIABILITY` result. It is not an estimate and not a model figure.

**Refund candidate.** Tax the vendor charged that the engine indicates appears not owed or overcharged. Direction is `REFUND_CANDIDATE`. It is tracked on its own list, apart from liabilities. Before approval, the amount may show on the item and the review list, and totals get a count only. A refund candidate is not cash, and it is never subtracted from liabilities.

**Approved for pursuit.** The only refund-candidate status in the demo: unit status `APPROVED_FOR_PURSUIT`, approval path `REFUND_PURSUIT`. A named person approved pursuing the refund. The direction value remains `REFUND_CANDIDATE`. Those three strings name one decision. Approved for pursuit is not cash received, not a sent claim, and not money back.

**Unreviewed.** An `ENGINE_SUPPORTED` result no named person has approved or rejected. It stays out of every money total. Unreviewed is not NHR and not a hold.

## Facts and evidence

**Admitted fact.** A sourced value that has passed the verified required-fact catalog, including any recorded confirmation the catalog requires. It keeps origin, source, evidence rung, and confirmation, and it links to evidence: document, page, exact passage, and provenance. The model only proposes facts. Confidence alone never admits a fact. A model field for rate, tax-decision amount, treatment, label, direction, or a legal conclusion fails with `FORBIDDEN_FIELD`.

**Fact origin.** Where the value came from. The closed set is `AI_EXTRACTED`, `HUMAN_SUPPLIED`, and `RAMP_RECORD`. Origin records provenance. It is not a tax decision.

**Evidence rung (location ladder).** Ordered SaaS evidence for user location by state, strongest first, stored as rung 1 through 5.

1. Seat export or roster.
2. Named answer.
3. SSO or HRIS business location. It has to be business-location data.
4. Order-form ship-to. Needs a named single-office confirmation.
5. Matched office address. Needs a named single-office confirmation.

The ladder is ordered evidence, not automatic admission. A low-confidence proposal always needs a recorded confirmation, on any rung. The record keeps source, rung, confidence, whether it was confirmed, and who confirmed it. Conflicts or partial seat coverage stay unconfirmed and the item holds. A rung is not a tax jurisdiction, and the model does not assign one. Bill-to is not a rung.

**Confirmation.** Whether the source is accepted for that fact. The two states are `CONFIRMED` and `UNCONFIRMED`. An unconfirmed required fact holds the item under `UNCONFIRMED_REQUIRED_FACT`. Shown confidence is not confirmation.

**Bill-to.** The invoice bill-to address, kept as context for why a vendor may have charged tax. Bill-to never confirms where software is used, and it never confirms hardware delivery. Confirmed ship-to can establish hardware delivery. It can establish SaaS use only under the named single-office rule.

**No tax line.** A null tax-charged value: the document shows no tax line. That is not a tax line of `0.00`. Zero reaches the engine only when a sourced fact shows `0.00` with evidence. When the catalog requires tax charged, a silent invoice holds the item. An assumed zero is not an admitted fact.

**Required-fact catalog (S3).** The verified list of facts each category needs, which sources count, and which rungs are accepted. Entries are verified under `docs/content-verification.md`. An entry stays `candidate` until it is `verified`. The model never authors the catalog, and neither does an agent. Rule entries use the same active word, `verified`, under effective dating. The catalog decides whether facts are complete enough to leave a hold. It is not a taxability ruling. A candidate entry does not admit a fact.

**Remote-access SaaS.** Hosted software accessed electronically, with no delivered copy. The category value is `REMOTE_ACCESS_SAAS`. Required facts include delivery method, access model, service period, seat count, and user location by state for every seat. The location ladder above is the SaaS ladder. Classifying a line as SaaS is not a taxability call, and the model may not assume that "SaaS" means no media was delivered.

**Computer hardware.** Tangible hardware, the demo's separate delivery case. The category value is `COMPUTER_HARDWARE`. It needs confirmed delivery or ship-to evidence, not the SaaS ladder. A sourced shipping amount, including an explicit zero, is a fact the verified rule uses for its base. Bill-to is not that evidence. The demo does not split a mixed invoice across categories.

**Unsupported category.** Category value `NO_SUPPORTED_CATEGORY`. The item routes to NHR with NHR reason `NO_SUPPORTED_CATEGORY`. It is not a third label.

## Purchases and intake

**Purchase.** One canonical invoice in the spend flow: buyer entity, vendor, normalized invoice number, invoice date, and the linked Ramp transaction and bill ids. A card charge and a later bill for the same invoice are one purchase. Identity is entity, vendor, and normalized invoice number. Date and amount only corroborate. An ambiguous match goes to review. It is never merged by deleting one side.

**Purchase line.** One row on that purchase: description, quantity, unit price, gross, discount, line net, and service period. The engine is called once per line per jurisdiction. It is not called for a held or NHR item. Line net is the printed amount after discount. It is not liabilities minus refunds.

**Document.** A synthetic invoice, receipt, or attachment, stored as a blob and identified by SHA-256, with a MIME type and a page count. Document text is evidence for extraction. It is not instructions to the model. No real customer, vendor, employee, or Ramp document is used, including as a template.

**Source event.** A Ramp-shaped webhook or simulator payload after signature verification: source, external event id, payload hash, and signature result. A bad `X-Ramp-Signature` is rejected with `BAD_SIGNATURE` before anything is stored. The same event id returns the existing object and starts no second job.

**Dedup.** Two guards. Event dedup is the external event id, including retries and out-of-order delivery. Invoice dedup folds a card charge and a later bill into the one purchase above. Dedup does not approve anything, and it does not drop the earlier trail.

## Determination lifecycle

**Determination.** The stored result of one engine request: one purchase line, one jurisdiction, one facts version, one rule-set version. It carries label, NHR reason, direction, `derived_direction`, rate, location code, expected tax, engine amount, rule reference, and unit status. Amounts are the engine's decimal strings, copied as-is. The app does not round, recompute, or fill one in.

**Supersede.** A new fact, rule-set, or determination version replaces the earlier result for review, totals, and outputs. The earlier result stays in the trail, marked `SUPERSEDED`, and is never overwritten or deleted. The named person approves the new version. A superseded approval is not still in force. Totals, drafts, and the workpaper use the current approved version only.

**Unit status.** The lifecycle of one determination. The closed set is `REQUESTED`, `DETERMINATION_ERROR`, `NHR_ENGINE`, `SENT_TO_ADVISOR`, `READY_FOR_REVIEW`, `APPROVED`, `APPROVED_FOR_PURSUIT`, `REJECTED`, and `SUPERSEDED`. `DETERMINATION_ERROR` is the error path. `NHR_ENGINE` is needs-human-review on a determination. `SENT_TO_ADVISOR` is the refer path. `READY_FOR_REVIEW` is waiting on the named person. `APPROVED` and `APPROVED_FOR_PURSUIT` are the two approval statuses; pursuit is only the refund path. The specs include `REQUESTED` in the set and do not give it a further definition.

**Item status.** The lifecycle of the purchase, which is not the same set. The closed set is `INTAKEN`, `OUT_OF_SCOPE`, `FACTS_ADMITTED`, `CLASSIFIED`, `HELD`, `NHR_PRE_DETERMINATION`, `READY_FOR_DETERMINATION`, and `DETERMINED`. A whole-item hold uses `HELD`. NHR before a determination exists (unsupported category, undeclared jurisdiction) uses `NHR_PRE_DETERMINATION`. `NHR_ENGINE` is not an item status.

## Outputs, filing, and totals

**Simulated filing.** The only filing lifecycle. Nothing is sent to a tax authority, no letter is transmitted, and no money moves. There is no submitted, accepted, filed, or paid state. The words "filed" and "paid" do not appear unless they are paired with "simulated."

**Journal entry draft.** A draft use-tax entry for current approved liability units: debit Use Tax Expense, credit Use Tax Payable, lines copied from those units. It is not posted and not a payment.

**Refund letter draft.** A draft letter for a refund candidate approved for pursuit. It uses the vendor, invoice, copied amount, and basis text from `rules/filing_basis.yaml` verified under `docs/content-verification.md`. The model does not write that basis. The letter stays a draft and is never sent.

**Workpaper.** The document for one filing key: entity, jurisdiction, and period, built only from current approved liability lines. The demo period is Washington Q4 2026, 2026-10-01 through 2026-12-31, assigned by invoice date (`invoice_date`). Held, NHR, error, unreviewed, rejected, and superseded units appear in an excluded section with no tax amounts. Refund candidates stay on a separate list. A key with no approved lines says "No approved liabilities" and does not show a bare 0.00. Marking ready freezes that version. A later change creates a new draft and a visible flag. It does not edit the frozen version.

**DRAFT.** Workpaper status before simulated ready-to-file, and the mark on journal-entry and refund-letter drafts. A frozen export is also marked draft and simulated, so it cannot be read as a real return. Draft means not filed.

**READY_TO_FILE_SIMULATED.** The only status after `DRAFT`. There is no transition back. Ready-to-file simulated is not a filing confirmation and not a payment.

**Totals.** Shown side by side and never added together:

- Approved liabilities, by jurisdiction and period.
- Refund candidates approved for pursuit, labeled not recovered cash.
- Held items: a count, plus purchase amount labeled "purchase amount, not tax."

Needs-human-review and determination errors are counts or lists with reasons, not amounts. Unreviewed results contribute a count only. Nothing enters a money total until a named person approves the current unit. There is no net, no savings figure, and no recovered figure. Optional audit exposure stays off until its basis and assumptions are verified under `docs/content-verification.md`. If shown, it is labeled "Illustrative: if an auditor finds it first" and uses approved liabilities only. It is not savings, and it does not take held, NHR, unreviewed, or error units as inputs.

## Engine and rules

**Engine.** The in-process mock of a limited reviewed rule set (`engine_mode=mock_reviewed_ruleset`). It is the only source of treatment, tax-decision amount, and direction. One request per line per jurisdiction. A held or NHR item is never sent. A supported response carries treatment, expected tax, adjustment amount, an optional direction, rule reference, rate, and location code. Outside coverage the engine reports unsupported, with no amount, and the result is NHR `ENGINE_UNSUPPORTED`. The engine is not the model.

**Treatment.** How a reviewed rule treats the line for purchase-side sales or use tax. Only the engine emits it, and only from a reviewed entry that applies. A person may approve, reject, supply or correct a fact, or refer an NHR item. A person cannot set treatment, label, direction, or a tax-decision amount. Sourced printed invoice amounts are facts and may be corrected; that correction versions the fact and supersedes the old result.

**Tax-decision amount.** The engine's expected tax and its adjustment amount. The adjustment is tax owed on a liability, the amount in question on a refund candidate, or none when charged correctly. The determination stores that adjustment as the engine amount, the engine's decimal string, copied as-is. Tax charged as printed on the invoice is a fact, which the model may extract with evidence. A printed amount is not a tax-decision amount. The model does not generate rates, expected tax, or amounts owed.

**Rule reference.** The verified rule and rate reference copied onto a supported determination, unchanged. A supported result needs a real effective-dated reference verified under `docs/content-verification.md`. A source title by itself is not that reference. If a supported response arrives without one, the outcome is a determination error with no amount, not a guess and not a zero.

**PLACEHOLDER entry.** A rule entry whose id starts with the prefix `PLACEHOLDER:`. The prefix is not a label, not a unit status, and not a third result. The entry stays inactive and illustrative. It cannot support an approval or a total. Placeholder and other unreviewed entries route to NHR with no amount: `RULE_TRANSITION_UNRESOLVED` when the gap is an unresolved transition, otherwise `ENGINE_UNSUPPORTED`.

**Effective dating.** Each rule entry has an effective from and an effective to. The engine picks the entry from those dates and the timing basis verified under `docs/content-verification.md`. The reporting period is chosen separately, by invoice date. Demo SaaS uses monthly service periods. No annual or cross-year timing is inferred. Hardware uses its own verified delivery-date rule. Unverified timing and an unresolved transition route to NHR `RULE_TRANSITION_UNRESOLVED`, with no amount and no approval. Changing a date does not by itself make a result engine-supported. Only a verified entry activates. A `candidate` rule entry is inactive.

## Scope and run modes

**Declared jurisdictions.** The states the engine may be asked about. Fixed to CA, WA, and NY. Any other state on a confirmed location, or on tax charged, is whole-item NHR `UNDECLARED_JURISDICTION`. Declared does not mean every category is covered in every one of those states. Coverage is whatever the reviewed rules actually state.

**Synthetic data.** The only data mode (`data_mode=synthetic`). Documents, vendors, and companies are fictional, and only synthetic documents are sent to a model. The app refuses any other data mode. Every screen keeps a synthetic-data banner. The Design Spec states that banner both as "Synthetic data • simulated filing" and as "Synthetic demo data"; the technical spec uses "Synthetic demo data." Replayed model output is labeled replay. Synthetic data is not a switch for a real-customer pilot.

**Replay, record, and live.** The model call mode (`llm_mode`). `live` calls the provider. `record` calls the provider and saves the response. `replay` returns a saved response and fails when none is stored. CI and the stage demo run `replay`. A replay is labeled replay and is never presented as a live model call. Rule recalculation stays deterministic in every mode. That recalculation is not a live model call.

## Command guards

**Idempotency key.** The client key required on every mutation. The same key with the same body returns the original response. The same key with a different body is refused. A retried webhook or command must not create a second purchase, approval, or amount.

**Expected version.** The version the caller last read, sent on a command that changes a fact, a determination, or a workpaper. If the stored version has already moved, the command is refused and writes nothing. The caller acts on the new version. The expected version is not the idempotency key. One stops a duplicate submit. The other stops a decision against a stale record.
