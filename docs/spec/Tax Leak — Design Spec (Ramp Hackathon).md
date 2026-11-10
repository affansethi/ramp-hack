# Tax Leak — Design Spec (Lean)

*Product rules only. Synthetic data, simulated filing. Reconciled with Technical Spec Tab 2 “Tax Leak updated Affan” v2 on November 7, 2026 (Pacific). Tab 2 is the active build plan; Tab 1 is historical. Product behavior is defined here; any new disagreement must be resolved explicitly, never by mixing old instructions.*

## 1 Product in one page

**Problem.** Growing multistate companies get purchase-side sales and use tax wrong in two directions:

- **Overpaid.** A vendor charges tax that may not be owed. Getting it back is a separate refund process.  
- **Underpaid.** No tax was charged, and the buyer owes use tax that sits unreported until an audit finds it.

The hard part is usually the facts, not the arithmetic. Invoices do not say where a SaaS tool is used or by whom. Those facts sit in people's heads, and nobody asks for them before close.

**Product.** Tax Leak adds purchase sales/use tax handling to the Ramp spend workflow:

1. **Gather facts.** AI reads invoices and terms, proposes the facts that matter, and links each one to its source.  
2. **Ask for what is missing.** If a required fact is missing, the item is held and the right person gets one plain-language question.  
3. **Calculate.** A mock / limited reviewed rule engine returns the treatment, amount, and rule reference. The AI never decides tax.  
4. **Approve.** A named human approves or rejects each engine-supported result.  
5. **Prepare filing.** Approved liabilities roll into a jurisdiction/period workpaper, which runs through a simulated filing lifecycle.

**Buyer.** The finance lead (controller, VP Finance, head of accounting) at a growing multistate company on Ramp with no indirect-tax team.

**Wedge.** Avalara, Vertex, and similar tools already calculate and file. Tax Leak does not claim better calculation or filing. The hypothesis is narrower: it is easier to collect missing facts and resolve exceptions before close inside the spend workflow, where the vendor, invoice, amount, and requester already live. The demo can show the workflow. It cannot prove market demand.

**Demo proof. Seven synthetic cases cover CA/WA SaaS, NY hardware, a safe unresolved CA transition, missing evidence and webhook replay (Section 6). The shared review/approval flow produces a WA Q4 2026 draft workpaper. Extraction is replay-labeled on stage; rule recalculation is deterministic.**

**Evidence discipline.** The origin story is an anecdote, not traction. Vendor survey figures are directional context only.

## 2 Scope and non-goals

**In scope (demo)**

- Company **purchase** sales and use tax, U.S. only.  
- Remote-access SaaS plus one NY tangible-hardware fixture; no mixed-invoice splitting.  
- Declared jurisdictions fixed to CA, WA and NY; reviewed fixture coverage is category-specific.  
- AI fact extraction with source links; targeted missing-fact questions shown in-app to a simulated recipient.  
- Deterministic results from owner-reviewed, effective-dated CA/WA SaaS and NY hardware rules and rates. Unreviewed rules, transitions and unsupported cases route NHR; placeholder fixtures are visibly illustrative, never validated legal determinations.  
- Holds, human review, approval, separate liability and refund-candidate totals.  
- One WA Q4 2026 workpaper: 2026-10-01 to 2026-12-31, period assigned by invoice date; DRAFT → READY\_TO\_FILE\_SIMULATED, frozen version.  
- **Simulated** filing lifecycle only.  
- Synthetic documents and fictional companies only.

**Non-goals**

- No live filing, remittance, or payment. No contact with any tax authority.  
- No income, franchise, payroll, VAT/GST, or non-U.S. tax. No sales-side tax collection.  
- No tax rules, legal conclusions, or treatment written by AI.  
- No human-decided treatment in the demo.  
- No "savings," "recovered," or traction claims.  
- No automated vendor refund claims, nexus studies, or registration.  
- Not a replacement for a tax engine's calculation or filing.  
- Later, not demo: reminders/escalation, automatic re-routing, bulk approval, partial holds, trail export, extra refund statuses and mixed-invoice splitting. Demo outputs are journal-entry and vendor-refund-letter drafts plus WA workpaper export; none is transmitted. Optional P1 audit-exposure estimate stays off until its sourced basis and assumptions are verified under `docs/content-verification.md`; it never estimates held/NHR units or claims savings.

## 3 Core user flow (demo)

**Actors.** Finance lead (reviewer and approver; one reviewer in the demo). Spend owner (answers fact questions; not a tax user). Outside advisor (read-only on items sent to them).

**Background flow, purchase to reviewable item**

1. **Intake. A Ramp-shaped synthetic webhook/simulator supplies purchase data and is deduplicated. Flag → review → approve → totals; neither webhook ingestion nor flagging contributes to approved totals.**  
2. **Extract and ground. AI proposes source-linked facts, including invoice amounts, never tax-decision amounts or treatment. Admitted sources include document/terms/seat-export spans, recorded named answers and the confirmed location ladder in Section 4\. Recorded extraction is visibly labeled replay.**  
3. **Classify. AI proposes remote-access SaaS or tangible hardware; the signed category-specific catalog selects required facts. This is not a taxability call.**  
4. **Fact check. Compare admitted facts against the signed category-specific catalog. Missing/unconfirmed required facts hold. Unsupported category, state outside CA/WA/NY, unreviewed rule/rate or unresolved transition routes NHR. Otherwise call the engine; no approval or totals until named human review.**  
5. **Hold and ask.** The missing fact is named in plain language and one question goes to the spend owner. The item stays out of approval and totals. When a sourced answer arrives, the full fact check re-runs.  
6. **Determine.** The engine is called once per line per jurisdiction. It returns a treatment, amount, and rule reference, or reports the case unsupported.  
7. **Label and route.** Each result gets one of two labels and, if engine-supported, one direction: liability, refund candidate, or no adjustment.

**Finance lead flow**

1. **Queue.** Three separate lists: *Ready for review*, *Needs human review*, *Held* (with the missing fact, who was asked, when, and reply status).  
2. **Figures.** Three figures side by side, never combined: approved liabilities, refund candidates approved for pursuit, held items (count and purchase amount only).  
3. **Review.** For one item: purchase, facts with source links, jurisdiction, engine amount, rule reference, label, direction.  
4. **Decide.** Approve, reject with a reason, or edit/supply a fact (which re-runs the item). For needs-human-review items: leave open or send to the advisor.  
5. **Close.** Pre-close view shows open items (held, needs human review, unreviewed). Confirm approved liabilities by jurisdiction.  
6. **Workpaper.** Generate the WA draft from approved liabilities only, with an excluded section and a separate refund-candidate list.  
7. **Simulated filing. Freeze DRAFT as READY\_TO\_FILE\_SIMULATED. Later fact or approval changes create a new draft version, never mutate the frozen one. No submitted/accepted filing claim or outbound authority call.**  
8. **Trail.** Any approved line, rejected item, or workpaper opens to its sources, facts, answers, engine request and response, label, approver, and change history.

**Spend owner flow.** Receives a question naming the vendor, amount, and date. Answers in a structured form (for example, users by state), optionally attaching a seat-assignment export, or says "I don't know," or names someone else. Only a sourced answer can move the item. "I don't know," a redirect, silence, or a timeout keeps it held.

**Re-runs.** Any fact change sends the item back through the fact check and, if complete, the engine. Earlier results are superseded, never overwritten. The reviewer approves the new result, not the old one.

## 4 Inputs and display behavior

### 4.1 Facts that matter

Required facts come from the required-fact catalog verified under `docs/content-verification.md`. AI never authors that catalog. For the demo, the candidate set is:

| Fact | Level | Accepted source | Not accepted |
| :---- | :---- | :---- | :---- |
| Vendor name, invoice date, line description, line amount, buyer entity | Required | Invoice or spend-record span | AI inference without a span |
| Tax charged (amount), including "none shown" | Required | Invoice or spend-record span | Assumed zero because no tax line was found |
| Tax charged (jurisdiction), when tax was charged | Required | Invoice span | Inferred from vendor location |
| Delivery method, access model, service period, seat count | Required (remote-access SaaS) | Invoice, order form, or terms span | AI assumption that "SaaS" means no media |
| **User location by state**, covering all seats | Required for SaaS, per jurisdiction | Confirmed SaaS ladder: seat export/roster → named answer → SSO/HRIS business location → order-form ship-to → matched office address. Last two require single-office confirmation; low-confidence proposals need explicit confirmation. | Bill-to alone; unconfirmed office/address inference; conflicting or incomplete seat coverage. Hardware requires confirmed delivery evidence, not the SaaS ladder. |
| Bill-to (informational); ship-to/delivery (required for hardware) | Bill-to informational; confirmed delivery required for hardware | Invoice span | Bill-to never establishes use. Confirmed ship-to can establish hardware delivery, or SaaS use only under the documented single-office confirmation rule. |

Rules that apply to every fact:

- **Tax charged \= zero** reaches the engine only when a sourced fact shows zero. A silent invoice holds the item.  
- **User location** is confirmed only when the state split covers the full seat count. Partial coverage or conflicting sources leave it unconfirmed.  
- Location ladder is ordered evidence, not automatic admission: keep source, rung, confidence, confirmed and confirmed\_by. SSO/HRIS must be business-location data; low confidence requires confirmation. Order-form ship-to or matched office address requires a named single-office confirmation. Bill-to never establishes use; conflicts/partial seat coverage hold. NY hardware instead requires confirmed delivery/ship-to evidence; include sourced shipping amount (explicit zero allowed) for the signed rule's taxable base.  
- AI confidence is shown but never alone admits a fact or releases a hold; low-confidence location proposals require explicit recorded confirmation under the catalog.

### 4.2 What is shown vs blocked

| Thing | Shown | Blocked |
| :---- | :---- | :---- |
| Approved liability | Engine amount, copied exactly; rule reference; label; approver and time | Any AI rounding or adjustment |
| Refund candidate (approved for pursuit) | Amount in question, on its own list, titled as not recovered cash | Netting against liabilities; "savings"/"recovered" wording |
| Unreviewed refund-direction result | Amount on the item view and review list | Any amount in any total (count only) |
| Held item | Count, purchase amount labeled "purchase amount, not tax," missing fact, who was asked, when, reply status | Tax amount, estimate, placeholder, or zero |
| Needs human review | Reason | Any amount; approve; any treatment |
| Determination error | Item and error state; retry | Any amount; display as zero |
| Approved no adjustment | Label, rule reference, informational count on the workpaper | Any amount in any total |
| Rejected | Reason; listed as excluded | Any amount; "no tax" meaning |
| Jurisdiction with no approved lines | "No approved liabilities" with held and open counts | A bare 0.00 |
| Rule reference | SUPPORTED: real reviewed-rule-set ID. PLACEHOLDER: is visibly illustrative metadata on an unsupported/NHR fixture only, never a supported legal claim. | Unmarked fake citation |
| Filing state | DRAFT or READY\_TO\_FILE\_SIMULATED; frozen ready version, new draft for later changes | "Filed," "paid," confirmation numbers, payment references |
| Explanations | Pointers to engine output and rule references | Any AI-written statement of law |

A persistent "Synthetic data • simulated filing" banner stays on screen. Cached AI output is labeled as a recorded replay, never shown under a live label.

## 5 Definitions: hold / NHR / error / approve

### Hold

A block placed **before any engine call** because a required fact is **missing** or **unconfirmed**. These are the only two hold reasons.

- *Missing:* no admitted fact fills the field. Rejected proposals, "I don't know," redirects, and silence count as no fact.  
- *Unconfirmed:* a fact exists but its source type is not accepted (for example, an HQ address offered as user location), it conflicts with another fact, or it is incomplete (for example, a state split that does not cover all seats).

Effects: no engine call for any line or jurisdiction of the item (whole-item hold), approve unavailable, excluded from every total and workpaper, no tax figure anywhere. The only exit is a sourced fact admitted by the rule-based check, followed by a full re-check that passes. There is no manual release action for anyone. AI saying "complete" never releases a hold.

### Needs human review (NHR)

The second of the two labels. Facts may be complete, but the system cannot produce an engine-supported result because:

- the category is unsupported,  
- a confirmed location fact names a state outside CA/WA/NY (the whole item goes to NHR; no state is dropped or treated as zero), or  
- the invoice shows tax charged for a state outside CA/WA/NY (same whole-item rule), or  
- the engine reports unsupported, a rule/rate is unreviewed, or an effective-date transition is unresolved (RULE\_TRANSITION\_UNRESOLVED).

NHR carries no amount, cannot be approved, and never enters any total or workpaper. The reviewer may leave it open or send it to the advisor. No one can give it a treatment, a label, or an amount. Hold and NHR are different things, and both stay out of every total.

### Error

A **determination error** is a technical failure: engine timeout, failed call, or a supported response that has no rule reference. It has no amount and no label. It is retried and listed on its own engine-errors list. An error or timeout never becomes zero, never becomes NHR by default, and never enters a total.

### Approve

A named, authenticated person's explicit decision on one **engine-supported** result at its current version. It records the name and time. Approval paths are separate actions on separate lists:

- **Liability:** the line enters approved liabilities and is eligible for the workpaper.  
- **Refund candidate:** it is approved for pursuit and enters the refund-candidate list. It is still a candidate, not cash.  
- **No adjustment:** recorded with label and rule reference. No amount enters any total.

Approve is refused for held items, NHR results, stale (superseded) versions, and anything already decided. The command itself refuses, not just the button. No system, AI, threshold, or bulk action approves. Approval never changes the engine's treatment, direction, or amount.

**Reject** records a reason and excludes the result. It does not create a different treatment. The only route to a new result is a fact change and a re-run.

**Labels and directions.** Labels: `ENGINE_SUPPORTED` ("reviewed rule / engine-supported") or `NEEDS_HUMAN_REVIEW` ("needs human review"). Directions, for engine-supported results only: liability, refund candidate, or no adjustment (charged correctly). If the engine returns no direction, it is derived by comparing the engine's expected tax with the sourced tax-charged fact, and the derivation is logged. The amount still comes from the engine.

## 6 Seven demo cases and pass/fail gates

Synthetic fixtures, not legal advice. Engine outputs must match verified, effective-dated rules and rate records under `docs/content-verification.md`. Figures below are illustrative hand-check targets until their sources and expected results are verified; placeholders never establish supported tax treatment.

### Case 1 — WA liability

Invoice date 2026-11-15; monthly service 2026-11-01 to 2026-11-30; synthetic SaaS charge \$1,440, Seattle seat evidence, explicitly sourced \$0 vendor tax. At the owner-approved illustrative 10.55% rate, engine output is \$151.92, WA-RAS-001, location code 1726\. Review then approve; only afterward add it to WA Q4 liabilities/workpaper. Fail: a different amount from the approved golden, missing source/rate/code, or totals changing merely on flagging. This monthly fixture replaces the ambiguous annual-seat example; no annual or cross-year timing rule is inferred.

### Case 2 — CA vs WA

Add a second line with otherwise identical monthly terms and amount, users in San Francisco and \$0 vendor tax. Fixture target, pending verification of CA-RAS-001 under `docs/content-verification.md`: CHARGED\_CORRECTLY, no liability. If the rule is not verified, show NHR instead; never force the fixture target. Compare with Case 1; requests differ only by line, jurisdiction and location. Show evidence and source-linked rule outputs; approve each separately. Fail: AI tax judgment, unsourced contrast, or non-approved units in totals.

### Case 3 — CA overcharge

Service period 2026-12-01 to 2026-12-31; invoice date 2026-12-15; monthly SaaS, San Francisco user evidence, price \$962.32 and sourced vendor tax \$83.00. Fixture target after CA-RAS-001 and the golden are verified under `docs/content-verification.md`: REFUND\_CANDIDATE \$83.00; without a verified rule, NHR instead. Human approval for pursuit creates its separate total and a DRAFT vendor letter using basis text from `rules/filing_basis.yaml` verified under `docs/content-verification.md`. No letter is sent. Fail: netting, recovery/savings claims, or AI-generated legal conclusions.

### Case 4 — Unresolved CA transition

Change Case 3's service period to 2027-01-01 through 2027-01-31; invoice date remains 2026-12-15 for this timing-isolation test. Until the applicable transition rule, rate and handling are verified under `docs/content-verification.md`, return NHR RULE\_TRANSITION\_UNRESOLVED, no amount, approval, refund or total contribution. PLACEHOLDER:CA-DP-001 has catalog review\_status=candidate (inactive), illustrative metadata only; PLACEHOLDER: is its identifier prefix, not a result label or a status. Pass: deterministic date change causes the safe NHR stop; model extraction may remain a labeled replay. A supported 2027 verdict requires a separate verified rule and newly verified golden, not merely replacing the date.

### Case 5 — NY hardware

Synthetic laptop \$2,100, confirmed delivery in NYC, sourced \$0 vendor tax. Owner-approved 8.875% illustrative fixture yields \$186.38 with zero shipping explicitly sourced; positive shipping requires a separately reviewed fixture/base and golden. Show NY-TPP-001, code 8081 and the reviewed rate/location source; missing source/rate/code fails. Without confirmed delivery, hold; never use bill-to. Any registration/form note is an owner-reviewed draft, not filing or advice from the model.

### Case 6 — Missing evidence

Multistate SaaS invoice with HQ bill-to only and no confirmed user split. Hold for user location by state; ask for a seat export or named answer. Show recipient, time and purchase amount labeled not tax. Pass: no engine call, no tax figure/zero, no approval or total contribution. I don't know, redirects, conflicts and silence keep the hold. After accepted evidence, re-check; outside CA/WA/NY routes the whole item to NHR.

### Case 7 — Replay and recovery

Replay the same webhook identity; return the existing object, with no second purchase, job effect, approval or amount. Also restart the worker with an unfinished job: durable leases recover it without duplicating effects. Pass: records and totals unchanged by replay; restarting never silently loses work.

### Cross-case workpaper and demo gate

WA Q4 2026 (2026-10-01 to 2026-12-31), invoice-date assignment. Workpaper includes only current approved WA liability units, with held/NHR/error/unreviewed units excluded without tax amounts; approved refund candidates remain on a separate list/total. DRAFT becomes READY\_TO\_FILE\_SIMULATED and freezes; later changes create a new draft version. No tax authority call, payment, letter sending or accepted/submitted filing state. The hosted demo uses replay-labeled extraction and live deterministic rule evaluation: flag → review → approve → totals. Every screen says Synthetic demo data; no model replay is labeled live AI.

## 7 Product constraints (hard rules)

**H1 Two labels only.** Every result is `ENGINE_SUPPORTED` ("reviewed rule / engine-supported") or `NEEDS_HUMAN_REVIEW` ("needs human review"). No third label exists.

**H2 Holds are not a silent zero.** A missing or unconfirmed required fact places a hold: no engine call, no approval, no tax amount, estimate, or zero. Errors and timeouts never become zero.

**H3 Refunds are never netted.** Approved liabilities and refund candidates (approved for pursuit) have separate totals in every view, report, and workpaper. No net, "savings," or "recovered" figure exists until confirmed.

**H4 No live filing. Only DRAFT → READY\_TO\_FILE\_SIMULATED; immutable ready versions. Nothing is transmitted and no money moves.**

**H5 No human-decided treatment in the demo.** A person may approve, reject, supply or edit a fact, or leave an NHR item open or send it to the advisor. No person sets a treatment, label, direction, or amount.

**Totals and amounts**

- No held, NHR, unreviewed, rejected, superseded, or error unit appears in any total.  
- Nothing counts until a named human approves it.  
- Engine amounts and rule references are copied exactly. No AI or app rounding, adjustment, or fill-in.  
- SUPPORTED results require real owner-reviewed, effective-dated rule and rate references copied exactly. PLACEHOLDER: entries are visibly illustrative and cannot support approval or totals; unresolved applicability routes NHR with no amount.

**Who decides what**

- The engine (mock / limited reviewed rule set) is the only source of treatment, amount, and direction.  
- AI extracts, grounds, classifies, and drafts questions. Its output is always a proposal. It never decides tax.  
- Every admitted fact links to an accepted source with provenance: document/terms/seat-export span, business SSO/HRIS record, or named answer/confirmation under the catalog. Hardware needs confirmed delivery evidence; bill-to alone never confirms use.

## 8 Glossary (short; only terms needed to read this doc)

- **Admitted fact: a sourced candidate passing the signed category-specific catalog, including required recorded confirmation; see Section 4's location ladder.**  
- **Approved for pursuit:** the only refund-candidate status in the demo. It means a human approved pursuing the refund. It is not cash.  
- **Declared jurisdictions:** the states the engine may be asked about. Fixed to CA, WA and NY in the demo; supported category/rule coverage is explicit, not every category in every state.  
- **Direction:** liability, refund candidate, or no adjustment, for engine-supported results only.  
- **Engine:** the mock / limited reviewed rule set behind all results. It is the only source of treatment and amount.  
- **Liability line:** an approved use-tax amount owed, eligible for the workpaper.  
- **NHR:** needs human review (Section 5).  
- **No adjustment:** an engine-supported result where tax was charged correctly. It needs approval and has no amount in totals.  
- **Refund candidate:** tax charged that the engine indicates appears not owed or overcharged. Tracked apart from liabilities.  
- **Remote-access SaaS: hosted software accessed electronically, with no delivered copy. Computer hardware: the separate NY delivery fixture.**  
- **Required-fact catalog:** the signed list of facts each category needs, taken from engine inputs or the reviewed rule set, never from AI.  
- **Simulated filing: DRAFT → READY\_TO\_FILE\_SIMULATED only; freezes that workpaper version. No actual submitted, accepted, filed or paid state.**  
- **Superseded:** an earlier result replaced by a re-run. It is kept in the trail, never deleted.  
- **Workpaper:** the jurisdiction/period document built from approved liability lines only, with an excluded section and a separate refund list.

Status: lean Design Spec — product rules only.
