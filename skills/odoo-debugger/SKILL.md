---
name: odoo-debugger
description: Dedicated agent skill for debugging Odoo databases and code using the odoo-debug MCP server tools. Use when tasked to investigate Odoo issues, trace bugs, query databases, profile performance, or run the Odoo server.
---

# Odoo Debugger

This skill provides a structured workflow for debugging Odoo applications using the `odoo-debug` MCP server. It acts as an orchestrator, allowing you to fetch, scrub, run, and root-cause issues in Odoo databases through data inspection, logs, profiling, and reproduction in the web client — not through a live step-through debugger.

## 📚 Official Documentation
Reference the official Odoo documentation for version-specific implementation details:
- [Odoo 19.0 Documentation](https://www.odoo.com/documentation/19.0/)
- [Odoo 18.0 Documentation](https://www.odoo.com/documentation/18.0/)
- [Odoo 17.0 Documentation](https://www.odoo.com/documentation/17.0/)

## ⚠️ Mandatory Prerequisite
Whenever you debug an Odoo issue, you MUST apply systematic debugging discipline while utilizing the Odoo MCP tools below: reproduce the issue, form a single explicit hypothesis, then verify it against real data or execution before acting. Never guess — confirm each assumption with a tool before building on it.

## ⚠️ Security Prerequisites (CRITICAL)

Before performing any debugging or querying against an Odoo database, you **MUST** ensure the database has been confirmed anonymized. The MCP tools strictly enforce this — they check for a marker that is only written after a structural clean/dirty diff confirms the scrub actually took — but you should explicitly follow this flow:

1.  **Check whether it's already scrubbed.** A database is normally copied over from `main` and scrubbed on the host (`start.sh <db_name>`) before the container even starts, so most of the time nothing further is needed.
2.  **If it isn't** (missing the marker), call `scrub_database` with just the database name — it scrubs it in place and only unlocks the other tools if the diff check comes back clean. If it doesn't, the tool reports why instead of unlocking — review and re-run.
3.  **Ensure branch alignment** by calling `switch_odoo_branch` to match the target database version (verifiable via `get_odoo_version`).

## Core Workflows

### 1. Database Inspection & Querying
If you need to look at specific records without running the full Odoo application layer, use direct SQL or ORM tools.

*   `query_psql`: Direct, read-only SQL queries (e.g., `SELECT * FROM res_partner LIMIT 5`). Use for fast introspection.
*   `explain_query`: If a query is slow, run it through this tool to see the Postgres query plan.
*   `search_records`: Query records using the Odoo ORM (JSON-2 API). This is safer for translated (`JSONB`) fields and follows Odoo's domain syntax. Requires the server to be running.
*   `get_model_fields`: Use this to discover available fields and their types before writing complex `search_records` or `query_psql` queries.
*   `call_method`: Call methods on an Odoo model (e.g., `action_confirm`, `read`) using the JSON-2 API. You may use this to debug certain methods whose outputs or effects you are uncertain about.
 You can pass positional args and kwargs using JSON syntax. Requires the server to be running.

### 2. Performance Profiling
If the issue is related to speed, memory leaks, or hangs, do not guess. Use the profiling tools:

*   **CPU Profiling:** Use `profile_cpu` to generate a py-spy flamegraph. This is ideal for finding hot code paths.
*   **Memory Profiling:** Use `memray_attach` to track allocations, followed by `memray_detach`, and finally `memray_flamegraph` to visualize the memory footprint.
*   **Instant Diagnostics:** Use `get_process_info` for a quick glance at memory/threads, or `dump_stacks` to instantly see what all threads are currently doing (ideal for diagnosing deadlocks).

### 3. Reproducing in the Web Client
If the issue is visible in the UI — a view that renders wrong, a JavaScript or Owl error, an action that misbehaves, an asset bundle that won't build — drive the web client instead of inferring it from the data. See the `odoo-ui-repro` skill for the full workflow and the Odoo-specific traps; the tools are:

*   `browser_open`: Launch headless Chromium against the running server and log in (reuses the ORM tools' session, so no login form). Requires `start_odoo`.
*   `browser_snapshot`: An accessibility-tree view of what is on screen. This is the primary way to see the page — read it, then build selectors from the roles and names it shows.
*   `browser_goto` / `browser_click` / `browser_fill` / `browser_press`: Navigate and interact. Each waits for Odoo to settle and reports the resulting breadcrumb plus any error dialog.
*   `browser_console`: JavaScript exceptions, console output, renderer crashes.
*   `browser_network`: Requests, with `/web/dataset/call_kw` decoded to model/method and the server traceback extracted. Odoo returns server-side exceptions as HTTP 200 with an error body, so read this rather than the status code.
*   `browser_eval`: Run JavaScript in the page when nothing else reaches — Owl component state, services off `odoo.__WOWL_DEBUG__.root.env`.
*   `browser_screenshot`: PNG to the artifacts directory. Use for visual bugs only; a snapshot says more per token.
*   `browser_close`: Frees the Chromium process. Always call it.

### 4. General Server Execution
If you just need the server running to test an API or reproduce an issue:

*   `start_odoo`: Starts the server in the background.
*   `get_odoo_logs`: View the output (crucial for finding tracebacks).
*   `stop_odoo`: Always remember to shut down the server when finished.

## Best Practices

*   **Never assume the schema:** Odoo schemas change drastically between versions (e.g., `hr.contract` vs `hr.version` in v19). Always use `get_model_fields` or `query_psql` to check `information_schema` before writing large queries.
*   **Logs are your friend:** If an ORM call fails, the answer is usually in `get_odoo_logs`.
*   **Clean up:** Ensure `browser_close`, `stop_odoo` and `memray_detach` are called before ending your session to prevent zombie processes.
