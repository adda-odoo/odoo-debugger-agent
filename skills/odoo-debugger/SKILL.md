---
name: odoo-debugger
description: Dedicated agent skill for debugging Odoo databases and code using the odoo-debug MCP server tools. Use when tasked to investigate Odoo issues, trace bugs, query databases, profile performance, or run the Odoo server.
---

# Odoo Debugger

This skill provides a structured workflow for debugging Odoo applications using the `odoo-debug` MCP server. It acts as an orchestrator, allowing you to fetch, scrub, run, and deeply inspect Odoo databases.

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

### 2. Live Debugging (DAP)
To trace Python execution, inspect variables, or catch exceptions as they happen:

1.  Start Odoo in debug mode using `attach_debugger`.
2.  Set breakpoints using `set_breakpoint`.
3.  Connect the client using `connect_debugger` (this releases the startup hold).
4.  Trigger the bug (e.g., via a cURL request, browser automation, or waiting for a cron job).
5.  Once execution pauses, use `get_call_stack` to find your frame ID.
6.  Inspect state using `get_variables` or `evaluate_expression`.
7.  Resume with `resume_execution` or pause running threads with `pause_execution`.
8.  Clean up using `stop_odoo`.

### 3. Performance Profiling
If the issue is related to speed, memory leaks, or hangs, do not guess. Use the profiling tools:

*   **CPU Profiling:** Use `profile_cpu` to generate a py-spy flamegraph. This is ideal for finding hot code paths.
*   **Memory Profiling:** Use `memray_attach` to track allocations, followed by `memray_detach`, and finally `memray_flamegraph` to visualize the memory footprint.
*   **Instant Diagnostics:** Use `get_process_info` for a quick glance at memory/threads, or `dump_stacks` to instantly see what all threads are currently doing (ideal for diagnosing deadlocks).

### 4. General Server Execution
If you just need the server running to test an API or reproduce an issue:

*   `start_odoo`: Starts the server in the background.
*   `get_odoo_logs`: View the output (crucial for finding tracebacks).
*   `stop_odoo`: Always remember to shut down the server when finished.

## Best Practices

*   **Never assume the schema:** Odoo schemas change drastically between versions (e.g., `hr.contract` vs `hr.version` in v19). Always use `get_model_fields` or `query_psql` to check `information_schema` before writing large queries.
*   **Logs are your friend:** If an ORM call fails, the answer is usually in `get_odoo_logs`.
*   **Clean up:** Ensure `stop_odoo` and `memray_detach` are called before ending your session to prevent zombie processes.
