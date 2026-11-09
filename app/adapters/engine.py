"""TaxEngine seam (decision D6).

Deterministic, in-process evaluation over owner-authored rules/*.yaml +
rate tables. One request per line x jurisdiction; effective-date rule
selection. The only source of treatment, tax-decision amounts and
direction. Never emits an amount without a reviewed rule/rate reference;
placeholder/unreviewed entries route NHR. Amount strings are copied
byte-for-byte, never rounded or recomputed.
"""

from typing import Protocol


class TaxEngine(Protocol):
    """In-process rule evaluator. Implementation (evaluator only, never
    rule entries) lands with the rules/rate-tables work item."""
