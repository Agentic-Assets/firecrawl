# AGENTIC-2277 Package-Readiness Remediation Addendum

## Change

The preflight now treats every syntactically valid CLI or MCP package pin as
`degraded` with `immutable_package_spec_declared_not_doctor_verified`.
It no longer inspects wrapper-default text or returns `ready` from static
evidence. `ready` remains reserved for a future schema-validated, fresh,
body-free compatibility-doctor receipt mechanism.

## Regression proof

The focused test creates direct wrappers whose exact defaults match supplied
CLI and MCP pins. Both capabilities remain `degraded`, and repeated
`--require cli --require mcp` exits 1 in offline mode. The test performs no
HTTP request or package resolution.

## Verification

- Focused unittest: 14 tests passed.
- Focused pytest: 14 tests passed, 19 subtests passed.
- `python3 -m py_compile` for the preflight and its test: passed.
- `git diff --check`: passed.

No wrapper, compatibility doctor, runtime, `.env`, CRE, or Linear path was
changed.
