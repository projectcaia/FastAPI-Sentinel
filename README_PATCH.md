# Sentinel Patch for ASCII idempotency key (2025‑08‑30)

This folder contains a patched version of the Sentinel `main.py` file that
addresses the Unicode encoding error encountered when forwarding alerts to
ConnectorHub.

## Problem

When Sentinel forwards alerts to ConnectorHub it uses the alert’s `index` and
timestamp to build an `idempotency_key`. Some indices include the Greek
character `Δ` (e.g., `ΔVIX`, `ΔSPX`). HTTP headers only support ASCII, so
including `Δ` in the `Idempotency‑Key` header causes a `UnicodeEncodeError`:

```
Hub forward error: 'ascii' codec can't encode character '\u0394'
```

## Solution

The patched `main.py` sanitizes the index value before constructing the
idempotency key. Non‑ASCII characters are stripped, leaving only letters,
numbers, underscores and hyphens. The sanitized key is used consistently in
both the request body and the HTTP header.

### Key points

* Added `_sanitize_index` helper to remove non‑ASCII characters.
* Sanitization applied when generating a new idempotency key and when a key is
  provided externally.
* Updated logging to clarify patched behaviour.

## How to apply

1. Clone or download the existing `FastAPI‑Sentinel` repository.
2. Replace the original `main.py` file at the repository root with the
   patched `main.py` from this folder.
3. Commit and push your changes to GitHub or your deployment platform (e.g.,
   Railway). Redeploy the service.
4. Watch the logs; alerts containing Greek letters should now forward
   successfully to ConnectorHub.

If you need to restore other files, ensure you merge this patch into your
current codebase rather than deleting existing FastAPI routes or worker code.
