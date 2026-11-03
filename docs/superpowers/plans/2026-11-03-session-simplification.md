# Session Simplification Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline and one final independent review.

**Goal:** Skip duplicate message serialization and share existing artifact saving across text/images/MCP.

**Architecture:** Add the early duplicate guard to Session.record_message. Extend existing artifact helpers to str/bytes with a suffix, then reuse them from the two binary callers.

**Tech Stack:** Python standard library and existing LangChain message serialization.

**Spec:** `docs/superpowers/specs/2026-11-03-session-simplification-design.md`

## Global Constraints

- Preserve old SHA256 filenames, UTF-8 bytes/newlines, default .txt suffix and existing-file reuse.
- Preserve ID assignment/reopen behavior, media validation, permissions, interruption recovery and MCP result format.
- No added dependency, class or module; continue on fix/windows-mcp/PR18, leave main and personal files alone.

## Review Focus

- IDs assigned to new messages and IDs loaded after reopening must both skip repeated serialization.
- Existing log dedup remains intact; the conversion-count check wraps the real serializer and actual log writes.
- Binary bytes, non-ASCII text and CRLF must remain exact; existing text paths must not change.
- Saving an already present artifact must preserve its bytes and modification time.
- Image/MCP callers must retain their format/size checks and saved-path/multimodal behavior.

### Task 1: Simplify the existing Session utilities

**Files:** `src/mini_harness/session.py`, `src/mini_harness/tools/images.py`, `src/mini_harness/tools/mcp.py`, `tests/test_session_tools.py`.

**Interfaces:** `artifact_path(content: str | bytes, suffix: str = ".txt") -> Path`; same signature for `save_artifact`; tool APIs unchanged.

- [x] Add serializer-count/reopen and exact-byte/artifact-reuse tests; run them before code changes (RED).
- [x] Add early dedup and common saving; remove duplicate hashing/writing and unused imports from image/MCP callers (GREEN).
- [x] Run session/image/MCP regressions, full pytest, Ruff, CLI help and secret checks; commit the tested change.
- [x] Request one independent read-only review.

Update PR18 and verify CI after validation; preserve the worktree and leave PRs unmerged.


## Validation

- Four regression cases first failed against the original code (repeated serialization and missing binary/suffix support).
- Related session/image/MCP tests: **55 passed in 46.67s**; full suite: **383 passed in 201.47s**.
- Independent review: no findings; **10 focused tests passed** independently.
- Ruff, CLI help, diff checks and Gitleaks passed.
- No model API or Windows desktop action was needed for this shared-storage refactor. Existing format/size/permission and interruption tests remain in the full suite.
