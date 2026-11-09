"""Closed enums (AGENTS.md): adding a value is a contract change needing
both owners (AF and RJ). Values trace to docs/architecture.md §3."""

from enum import StrEnum


class Label(StrEnum):
    """The only two determination labels (hard rule H1)."""

    ENGINE_SUPPORTED = "ENGINE_SUPPORTED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


class Direction(StrEnum):
    LIABILITY = "LIABILITY"
    REFUND_CANDIDATE = "REFUND_CANDIDATE"
    CHARGED_CORRECTLY = "CHARGED_CORRECTLY"


class NHRReason(StrEnum):
    NO_SUPPORTED_CATEGORY = "NO_SUPPORTED_CATEGORY"
    UNDECLARED_JURISDICTION = "UNDECLARED_JURISDICTION"
    ENGINE_UNSUPPORTED = "ENGINE_UNSUPPORTED"
    RULE_TRANSITION_UNRESOLVED = "RULE_TRANSITION_UNRESOLVED"


class HoldReason(StrEnum):
    MISSING_REQUIRED_FACT = "MISSING_REQUIRED_FACT"
    UNCONFIRMED_REQUIRED_FACT = "UNCONFIRMED_REQUIRED_FACT"


class FactOrigin(StrEnum):
    AI_EXTRACTED = "AI_EXTRACTED"
    HUMAN_SUPPLIED = "HUMAN_SUPPLIED"
    RAMP_RECORD = "RAMP_RECORD"


class FactConfirmation(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"


class WorkpaperStatus(StrEnum):
    DRAFT = "DRAFT"
    READY_TO_FILE_SIMULATED = "READY_TO_FILE_SIMULATED"


class DataMode(StrEnum):
    """Synthetic data only, in every environment."""

    SYNTHETIC = "synthetic"


class EngineMode(StrEnum):
    MOCK_REVIEWED_RULESET = "mock_reviewed_ruleset"


class DemoMode(StrEnum):
    ON = "true"


class LLMMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"
