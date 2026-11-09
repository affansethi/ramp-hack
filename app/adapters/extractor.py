"""DocumentExtractor seam (decision D7).

Two bounded calls per document: Call A (extraction) and Call B (category
+ question drafts). Providers: openai / anthropic / google / replay.
Replay serves recorded responses and is the CI/stage mode. The extractor
proposes source-linked facts only — it never emits rates, tax-decision
amounts, treatment, labels or direction; such a field fails with
FORBIDDEN_FIELD. "No tax line" (null) differs from "tax line of 0.00".
"""

from typing import Protocol


class DocumentExtractor(Protocol):
    """Provider-agnostic extraction interface. Implementation lands with
    the fixtures/recordings work item; the protocol fixes the boundary."""
