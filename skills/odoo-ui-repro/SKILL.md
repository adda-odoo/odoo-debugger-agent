---
name: odoo-ui-repro
description: Reproduce and root-cause Odoo bugs through the web client using the browser_* tools of the odoo-debug MCP server. Use when an issue is visible in the UI, involves JavaScript or Owl errors, asset bundles, views or client-side RPC, or when a data-only investigation has not explained the reported behaviour.
---

# Reproducing Odoo bugs in the browser

The `odoo-debug` MCP server drives a headless Chromium inside this container against
the Odoo server started by `start_odoo`. Use it when the bug lives in the view layer —
a form that silently refuses to save, an Owl component that crashes, an action that
opens the wrong view, an asset bundle that fails to build — or when SQL and the ORM
agree the data is fine but the user still sees something wrong.

This does not replace data inspection. The browser tells you *what* the user sees;
`query_psql`, `get_odoo_logs` and `search_records` tell you *why*. The point is to run
both and correlate.

## Prerequisites

The database must be confirmed anonymized, exactly as for every other tool here — the
browser renders the same data `query_psql` returns, so it is gated the same way and
re-checked on every call. Odoo must be running (`start_odoo`), and the checked-out
branch should match the database version (`get_odoo_version`, `switch_odoo_branch`).

Credentials are post-scrub: admin answers to login `user2`, password `admin`.
`browser_open` handles this for you by reusing the session the ORM tools already hold,
so you should never have to type them.

## The loop

1. **`browser_open`** with the database name. The first call after a scrub takes tens of
   seconds — Odoo is rebuilding the `web.assets_web` bundle, because scrubbing deletes
   the generated asset attachments. The warm-up time is reported separately; it is not
   a page-render problem.
2. **`browser_snapshot`** to see what is on screen. This is an accessibility tree, not
   markup — read it, then build selectors from the roles and names it shows.
3. **`browser_goto` / `browser_click` / `browser_fill` / `browser_press`** to walk the
   reported steps. Each waits for Odoo to settle and reports the resulting breadcrumb,
   plus any error dialog or notification that appeared.
4. **`browser_console`** and **`browser_network`** for what the page will not tell you
   directly: uncaught JS exceptions, failed RPCs, 404s on assets or images.
5. **Correlate.** A failed `call_kw` gives you a model, a method and usually a server
   traceback tail. Take it to `get_odoo_logs` for the full traceback, then to
   `query_psql` or `get_model_fields` for the data that triggered it.
6. **`browser_close`** when done, always — it frees the Chromium process.

## Four traps specific to Odoo

These will each cost you a debugging session if you meet them cold.

- **A JSON-RPC error is an HTTP 200.** Odoo answers a server-side exception with status
  200 and an `error` object in the body. Filtering by status code alone shows a clean
  page while the UI is covered in error dialogs. `browser_network` parses the body for
  you and surfaces the exception name, message and traceback tail — trust that over
  the status column.
- **The page is never "network idle".** The bus holds a websocket open for the page's
  lifetime, so waiting for network silence waits forever. The tools count in-flight
  non-bus RPCs instead; you never need to add a sleep.
- **`body.o_web_client` means nothing.** It is set server-side in the template, so it is
  present the instant the HTML arrives, long before Owl mounts anything. `.o_navbar`
  and `.o_action_manager` are the real client-side signals.
- **`.o_loading_indicator` has a blind spot.** It only renders after a 250ms timer, so a
  fast RPC never shows it. It tells you the page *is* busy, never that it is idle.

## Selectors

Prefer what `browser_snapshot` shows you — `button:has-text('Confirm')`,
`[name='partner_id'] input` — over a long class chain. Owl replaces DOM nodes when it
re-renders, and Playwright's automatic retry only saves you if the selector still
matches the new node.

For a many2one or tags field, `browser_fill` with `press_enter=True`. Without it the
typed text is left dangling in the autocomplete and the record will not save, which
looks exactly like the bug you were trying to reproduce.

## When the UI will not tell you

`browser_eval` runs JavaScript in the page. With `browser_open(debug=True)` — the
default — the web client exposes `odoo.__WOWL_DEBUG__.root`, so
`odoo.__WOWL_DEBUG__.root.env.services.action.currentController.action.res_model` and
similar reads let you inspect what the client actually thinks it is showing. Return
plain values; component instances and DOM nodes are not serializable.

## Evidence

`browser_screenshot` writes a PNG to the artifacts directory, which is bind-mounted to
the host, so it survives the container exiting. Use it when the bug is visual. For
everything else a snapshot says more and costs less.

## Running Odoo's own tours

Chromium is on `PATH` in this container, so `odoo-bin --test-enable --test-tags ...`
can run `HttpCase.browser_js` tours. After a fix, a relevant tour is a stronger check
than repeating the manual steps.
