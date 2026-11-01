# Windows MCP Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; implement inline and request one final independent review.

**Goal:** Improve Windows operation reliability and deliver MCP screenshots without a second model/tool round trip.

**Architecture:** Keep the two generic MCP tools. Add opt-in standard image blocks to the existing result adapter and pin the locally installed Windows server to the tested upstream commit.

**Tech Stack:** Python, LangChain tools, FastMCP, Windows PowerShell, uv.

**Spec:** `docs/superpowers/specs/2026-11-01-windows-mcp-design.md`

## Global Constraints

- Preserve the agent loop, existing permissions, default text-only MCP results and 60-second timeout.
- Pin Windows-MCP to `94b52cfb48076cb793e710351517fa18a316cd12`, use `relaxed`, never `--no-control`.
- No personal paths, keys, local configurations, screenshots or sessions in commits.

## Review Focus

- Image blocks must reach the model after a complete group of tool results.
- Long text and structured data must remain available when images are requested.
- Text-only models must retain the existing default.
- Reopening an MCP PNG through read_image must reuse its artifact.
- Startup configuration must use the tested source package without a PYTHONPATH overlay.
- Large tool descriptions must not hide names in the summary catalog; full schemas remain retrievable by name.

### Task 1: Opt-in images and Windows setup

**Files:** `src/mini_harness/tools/mcp.py`, `tests/test_mcp.py`, `examples/mcp.windows-wsl.json`, `docs/architecture.md`, `docs/windows-mcp.md`.

**Interfaces:** `call_mcp_tool(server, tool_name, arguments, include_images: bool = False) -> dict | list[dict]`; reuse `responses.model_messages()` unchanged.

- [x] Add real subprocess tests for inline images with short/long text and structured data; assert saved bytes and no duplicate after read_image.
- [x] Run new tests and observe missing image blocks (RED).
- [x] Add minimal adapter change; run MCP and image regressions (GREEN).
- [x] Reproduce oversized discovery from the first real run; keep server catalogs short and expose full metadata by tool_name. Verify RED then GREEN.
- [x] Publish generic source-install and WSL startup instructions with the pinned commit, offline cache and targeted warning filters.
- [ ] Run the real harness against the owned test form and verify the resulting input/click count. Back up and switch the ignored local config after success.
- [ ] Run pytest, Ruff, diff checks and secret scanning; request one independent review, address findings, create a draft PR stacked on PR17.
