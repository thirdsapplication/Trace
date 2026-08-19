# Testing Accessibility capture

What the suite covers, what only a real Mac can prove, and the exact order to
check it in. Written for whoever picks this up next, including future me.

## Why hand-testing is not optional here

Every unit test in `test_ax.py` runs against a fake adapter, because the real one
is pyobjc talking to another process across an IPC boundary that no CI can
exercise. That split is load-bearing and it has already caught its own limits:

> The first version of `AXAdapter` hung everything off
> `AXUIElementCreateSystemWide()`. Every unit test passed, the module imported,
> nothing raised — and in the capture sidecar's process every system-wide read
> returns `kAXErrorCannotComplete (-25204)`. It would have captured **zero events,
> forever**, and looked healthy doing it.

That is the same failure `window.py` documents for NSWorkspace: a headless process
Tauri spawns pumps no run loop. The fix is to build a per-application element from
the frontmost PID (`AXUIElementCreateApplication`), which works. `RealAdapterTest`
now guards it, but it **skips silently** when Accessibility is not granted — so a
green suite on an ungranted machine proves nothing about this.

**Rule: any change to `AXAdapter` must be re-verified with the probe below, on a
machine where Accessibility is granted.**

## 1. Automated (30 seconds)

```bash
SUPABASE_URL="" SUPABASE_ANON_KEY="" SUPABASE_SERVICE_ROLE_KEY="" \
  python -m pytest trace/tests/test_ax.py trace/tests/test_validate.py \
                   trace/tests/test_sources.py -v
```

Confirm `RealAdapterTest` shows **PASSED, not SKIPPED**. If it skipped, grant
Accessibility and fully relaunch the terminal — macOS does not apply the
permission to an already-running process.

Full suite before any push:

```bash
SUPABASE_URL="" SUPABASE_ANON_KEY="" SUPABASE_SERVICE_ROLE_KEY="" \
  python -m pytest trace/tests/ -q
```

## 2. The probe — the real test (5 minutes)

```bash
python -m trace.capture.probe --tree        # what can this app even expose?
python -m trace.capture.probe --seconds 60  # live, while you work
```

Run it, switch to the app you want to test, and work normally. Every printed line
is one raw event that would have been written.

| Line | Meaning |
|---|---|
| `FOCUS` | navigation context — which control you moved to |
| `CHANGED` | **the ones that matter** — a value you actually set |
| `CLICK … target=None` | element under the cursor could not be resolved: usually canvas-drawn, invisible to AX |
| `READ` | a capped snapshot of the surrounding window |

### The Shopify acceptance test

This is the workflow the whole change exists for: one window, minimal typing,
mostly button-pushing.

1. `python -m trace.capture.probe --seconds 120`
2. Add a product in Shopify admin: title, description, price, an image, a variant,
   set Status, hit Save.
3. Read the summary. It should show:
   - **`values you set` well above zero.** This is the acceptance criterion. Zero
     means dropdowns and fields are not recoverable and the session is no better
     than before.
   - `Status` and any vendor/type dropdown appearing as `CHANGED` lines.
   - `Price` and `Title` appearing once each on blur, **not** once per poll tick.
     Repeated `CHANGED` lines for one text field means the blur-flush regressed.
   - `CLICK` lines naming real buttons (`button 'Save'`), not `target=None`.

Known and expected to be missing: the uploaded image's content, drag-to-reorder,
anything canvas-rendered.

### Also worth probing

- **Safari** — the case with no extension at all. Previously invisible.
- **A native app** (Preview, Mail) — was limitation #1 in the README.
- **A password field** — must appear as `password 'Password' = ''`. A value here
  is a serious bug, not a cosmetic one.

## 3. Window titles without Screen Recording

The claim that demotes Screen Recording to optional. To check it honestly you have
to remove the permission:

1. System Settings → Privacy & Security → **Screen Recording** → turn the terminal
   (or Trace) **off**. Fully quit and relaunch it.
2. `python -m trace.capture.probe --tree`

`Focused window:` must still print a real title. It now comes from
`kAXTitleAttribute` via the AX fallback rather than `kCGWindowName`. If it prints
`None`, the fallback is not wired and Screen Recording is still load-bearing.

Turn it back on afterwards.

## 4. In the app

Browser only — `./launch_ui.command` → `127.0.0.1:8765/app`. Never Tauri for UI
checks: it needs a rebuild, and ad-hoc signing revokes capture permissions each
time.

- **Record screen** now runs a pre-flight. With Accessibility denied it shows a
  warning strip and a **Grant Accessibility** button that triggers the real macOS
  dialog. With everything granted it shows nothing at all.
- **Privacy policy → "What is captured"** has been rewritten to disclose that
  labels and values of controls are read. Confirm it reads honestly to you; this
  is the user-facing claim and it is a launch blocker either way.
- A full capture → review cycle, checking the event log names actual fields
  ("set Status to Active") rather than only window titles.

## 5. Signing — the thing that will bite

TCC keys permission grants to the code signature. Ad-hoc signing rotates the
identity on every rebuild, so **every rebuild revokes Accessibility** and capture
silently returns to window-titles-only. This was survivable when AX was unused; it
is not now.

A stable Developer ID signature is the fix. Until then, expect to re-grant after
each build and check the probe first when a session looks empty.

## Not done yet

- Go-live is **not** gated on capture health. `capture_health()` reports a warning
  and nothing blocks, so an agent can still be built from a session with no
  semantic signal. Making it blocking is a flow change that wants a decision.
- `structured_read` carries no `url`. The browser extension supplies one; AX could
  read `AXURL` from a web area, which would give Safari real URLs.
- The AX click resolver uses the frontmost app element. A click into a
  non-frontmost window resolves against the wrong app; rare, and it degrades to
  `target=None` rather than to a wrong answer.
