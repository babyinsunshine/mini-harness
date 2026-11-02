# MCP Schema Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with one final independent review.

**Goal:** Reduce MCP schema discovery and argument-correction round trips.

**Architecture:** One closure-local catalog cache serves the existing two MCP tools. Batch discovery exposes selected full schemas; JSON Schema validation rejects invalid arguments before remote execution and reuses existing error/artifact handling.

**Tech Stack:** Python 3.11+, LangChain tools, FastMCP client, jsonschema 4.

**Spec:** `docs/superpowers/specs/2026-11-02-mcp-schema-design.md`

## Global Constraints

- Keep two MCP tools, existing permissions, image results, timeouts and cancellation behavior.
- Replace discovery tool_name with tool_names list; do not alter call_mcp_tool's singular tool_name.
- Cache per server and tool-set lifetime, memory only; reconnect fetches fresh metadata.
- Validate but never mutate arguments or retry remote actions; keep jsonschema optional with MCP.
- Continue in the existing isolated fix/windows-mcp worktree and PR18; preserve personal configs and main.

## Review Focus

- Invalid arguments must not reach tools/call; successful correction produces exactly one effect.
- Batch results retain complete metadata, including long schemas; empty/unknown names are explicit.
- Catalog cache must isolate servers and reset on a new session/tool set.
- Remote execution failures must not be mislabeled as validation failures or retried.
- Large schema error feedback must be recoverable from artifacts; base installation stays usable without MCP.

### Task 1: Batch discovery, validation and documentation

**Files:** `src/mini_harness/tools/mcp.py`, `tests/test_mcp.py`, `pyproject.toml`, `uv.lock`, `docs/architecture.md`, `docs/windows-mcp.md`.

**Interfaces:** `list_mcp_tools(server: str | None=None, tool_names: list[str] | None=None) -> dict`; `call_mcp_tool` signature unchanged.

- [x] Add real protocol request recording to the MCP fixture. Write tests for ordered batch schemas, empty/unknown names, cache reuse/isolation/reopen, local validation and one-time corrected execution, long errors and unchanged runtime failures.
- [x] Run new tests against current code; confirm failures reflect missing batch discovery and local validation.
- [x] Add minimal catalog helper and lazy jsonschema import/validation; declare jsonschema>=4.20,<5 in the MCP extra and update lock offline. Migrate singular discovery calls and docs.
- [x] Run MCP/image/CLI regressions, then pytest, Ruff, locked dependency checks, CLI help and secret scan. Commit the tested change.
- [x] Obtain one read-only independent review and address findings.

Publish to existing PR18 and verify CI after validation; keep both PRs unmerged and preserve the worktree.


## Validation

- Seven new regression cases first failed against the original implementation (five core cases and two cache lifecycle cases).
- MCP suite: **25 passed in 74.05s**. Complete suite: **379 passed in 216.35s**.
- Independent review: no findings; independently reran **3 rejection/correction and runtime-error tests in 5.15s**.
- Real default-model + Windows MCP test: **19.8s, four model requests**. One Wait/Scroll batch, local rejection of Wait(seconds=0), successful Wait(duration=0), no schema requery and no other desktop actions.
- A separate offline base-only installation contained neither fastmcp nor jsonschema and passed CLI help.
- Ruff, CLI help, locked dependency verification, diff checks and Gitleaks passed. No dependency versions were upgraded.
