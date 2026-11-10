# tests/gates/

Never-fail release gates RG-1..RG-9 and H1/H5 (architecture §6). These
must stay green on every push; a failing financial gate is never waived.
`test_verify_content.py` exercises `make verify-content` with synthetic evidence.
