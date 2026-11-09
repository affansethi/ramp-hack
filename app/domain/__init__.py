"""Pipeline stages as pure-ish functions (architecture §2).

Domain code never performs I/O directly; adapters are injected. Stages:
ingest, deduplicate, admit facts, cross-check, classify, hold check,
jurisdiction set, engine orchestration, review/approve, ledgers, totals,
close, workpaper.
"""
