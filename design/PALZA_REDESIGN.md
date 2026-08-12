# Palza redesign — implementation spec for Claude Code

Companion files:
- `palza-app.css` — **drop-in replacement for `trace/ui/app_static/app.css`** (same selectors, new language, plus opt-in classes for the new structures). Restyle-only changes need nothing else.
- `Palza Final.dc.html` — the visual source of truth. Every reference like **(14)** below is a numbered frame in that file. Match it, don't reinterpret it.

Repo: `thirdsapplication/Trace`, UI at `trace/ui/app_static/` (`app.js` render functions hold the markup; `app.css` holds the skin).

---

## 1. Brand

- **Name:** the app is now **Palza** (lowercase wordmark "palza"). Replace every "Trace" string, `<title>`, and the `.brand` element.
- **Fonts:** Google Fonts `Manrope` (700/800 — headlines, wordmark, big numbers) + `Inter` (400–700 — everything else). IBM Plex Mono is retired; former mono labels become Inter 11px, 700, uppercase, letter-spacing .12em. Update the `<link>` in `index.html`:
  `https://fonts.googleapis.com/css2?family=Manrope:wght@700;800&family=Inter:wght@400;500;600;700&display=swap`
- **Palette** (all in `:root` of palza-app.css):
  - canvas `#FCFCFA` · panel `#FFFFFF` · inset `#F7F8FA` · lines `#EEF0F4`/`#E5E7EB`
  - ink `#121A2F` (headings, me-bubbles) · gray `#667085` · faint `#98A2B3`
  - **Core Blue `#315EF6` = action**: primary buttons, active nav, selected, focus, progress
  - **Action Coral `#FF6655` = needs you**: badges, rec dot, stopped runs, unread dots. Never decorative.
  - green `#45B86B`/`#2E8F50` = LIVE/success · yellow `#FFC83D`/`#C89011` = warnings/expired · red `#D4442E` = destructive only
  - **Agent colors** (§4): coral, yellow, green, lilac `#A58AE8`, blue + 20 extended shades
- **Rule:** 80% neutral, one dominant agent color per moment. Never rainbow-wash a screen.
- **Voice:** supportive strategist. Sentence case everywhere. No emoji, no "Great question!", no exclamation-point pileups. Agents state what they did, why, and the next move ("Two things need you. Everything else ran clean.").

### Logo (the "routes" mark)
Five colored routes converging on an ink dot with a white center. Inline SVG — reuse verbatim:

```html
<svg width="32" height="26" viewBox="0 0 110 90" fill="none">
  <path d="M6 9 C52 9 52 45 82 45"  stroke="#FF6655" stroke-width="10" stroke-linecap="round"/>
  <path d="M6 27 C48 27 48 45 82 45" stroke="#FFC83D" stroke-width="10" stroke-linecap="round"/>
  <path d="M6 45 H82"                stroke="#315EF6" stroke-width="10" stroke-linecap="round"/>
  <path d="M6 63 C48 63 48 45 82 45" stroke="#45B86B" stroke-width="10" stroke-linecap="round"/>
  <path d="M6 81 C52 81 52 45 82 45" stroke="#A58AE8" stroke-width="10" stroke-linecap="round"/>
  <circle cx="90" cy="45" r="14" fill="#121A2F"/><circle cx="90" cy="45" r="5.5" fill="#FFFFFF"/>
</svg>
```

Sidebar lockup = mark + `palza` in Manrope 800, 19px, letter-spacing -0.03em **(14)**.

- **Dock/app icon (03):** light (`#FCFCFA` squircle) and dark (`#121A2F` squircle) shipped as ONE adaptive macOS icon. ≤64px: simplify to 3 routes (coral/blue/green). 32px menubar glyph: white routes on `#315EF6`. Show a small dot under the dock icon while an agent is mid-run; coral badge count when blocked.

### Opening splash animation (02)
On app launch (and as the "Reading your day…" loader, 07): routes draw in, dot pops, wordmark rises. Keyframes `palzaDraw` / `palzaPop` / `palzaRise` are in palza-app.css. Per-path inline styles, 4.2s loop, stagger .12s per route:

```html
<path ... stroke-dasharray="170" style="animation:palzaDraw 4.2s ease-in-out infinite"/>
<circle ... style="transform-box:fill-box;transform-origin:center;animation:palzaPop 4.2s ease-in-out infinite"/>
<div class="brand" style="animation:palzaRise 4.2s ease-in-out infinite">palza</div>
```

Splash shows once per launch (~1 loop) then cross-fades to Home. Respect `prefers-reduced-motion: reduce` → static logo.

---

## 2. App shell — persistent left sidebar (14, 17, 18)

**Structural change** (edit render functions): replace the topbar-only navigation with `.shell > .sidebar + .main`. Classes provided in palza-app.css.

- Sidebar contents top→bottom: brand lockup · `Home` · `Messages` (+`.ct-badge` unread) · `Groups` (+ "Your groups" mini-list with stacked color tiles, 17) · `Sharing` · `Settings` · spacer · `+ New workflow` (`.btn-accent .full`, ink) · signed-in user chip.
- Active item: `.nav-item.active` (blue on `#EEF2FF`).
- The old `.topbar` remains only inside the setup wizard (which is full-screen, no sidebar) and the review overlay.
- `.wrap`'s 760px column becomes the `.main` content column; keep max-width ~880px for readability.

## 3. Routing / first-run logic

- **New user:** Create account → Welcome (04) → Privacy & terms (05, checkbox gates the button) → wizard steps 1–7 (06–13). The wizard is one-time-per-workflow; `Record` is step 1.
- **Returning user:** land on **Home (14)** always. Never re-enter the wizard unless they click `+ New workflow`.
- Persist `hasCompletedFirstRun` per account; terms acceptance stored with timestamp.

## 4. Agent identity system (19, 29)

- Each agent: `name`, `color` (hex), derived `bg` tint and `fg` text tone. Store all three; the CSS consumes them as inline custom properties on any agent-scoped element:
  `style="--ag:#45B86B; --ag-bg:#E8F6EC; --ag-fg:#2E8F50"` — `.ag-av`, `.dot`, `.st-ring`, `.dm-bubble .ag-name` all read them.
- Core 5: `#FF6655`(bg `#FFEDEA`/fg `#D4442E`-ish coral), `#FFC83D`(`#FFF6DE`/`#C89011`), `#45B86B`(`#E8F6EC`/`#2E8F50`), `#A58AE8`(`#F1EBFC`/`#8465CC`), `#315EF6`(`#EEF2FF`/`#315EF6`).
- Extended 20 (4 shades × 5 families), used when core colors are taken:
  coral `#FFB3A6 #FF8A73 #E04A38 #B33A2B` · yellow `#FFE49A #FFD966 #E0A818 #A87F12` · green `#A8E0BC #7BD097 #2E8F50 #1F6B3A` · lilac `#D9CCF6 #C3AFF0 #8465CC #64489F` · blue `#A9BDFB #7B98F9 #2447C4 #1A3390`.
- **Assignment at Go live (29):** auto-pick the unused shade with max hue distance from live agents. Taken swatches render at 35% opacity with the owner's initial badge. No repeats until all 25 used; then twins are allowed (distinguished by initial).
- **Editing (19):** one popover — name field + 5 core swatches + "More shades" grid. Entry points: pencil next to the name on the agent profile header (21) and once at Go live (13). Remove the old inline name/color editing section from the profile. Recolor applies live everywhere.

## 5. Screen-by-screen

### Home (14) — restyle + restructure
Order: greeting header (Manrope 800 ~27px, "Good morning, {name}" + right-aligned quiet status line) → stories strip → **Needs you** panel → agent grid.
- Stories `.st-ring`: ring in agent color only when unreviewed sessions exist; reviewed agents gray ring `#E5E7EB`, 62px avatars.
- **Needs you**: white panel, coral uppercase label `NEEDS YOU · n` — **kill the red left border**. Each `.needs-row`: agent `.ag-av`, bold one-liner + quoted detail/timestamp, right-aligned blue button (`Answer` / `Review`). Sort: questions → approvals → lessons.
- Agent grid: 2-col `.ag-row` cards — avatar, name, one-line bio (ellipsis), `.pill-on` LIVE / `.pill-off` PAUSED.
- Optional flourish: greeting headline can state the brief ("Two things need you. Everything else ran clean.") with agent names tinted in their colors.

### Review overlay (15, 27) — restyle + proposal restructure
Overlay bg `rgba(18,26,47,.96)`. Progress segments: done white, current coral. Card 18px radius.
- **Proposal cards stop dumping raw model output.** Structure: colored dot + bold action title ("Send email — 'Weekly recap: W32'") + one-sentence what/why + `View full email ▾` disclosure that reveals To/Subject/body in an inset block (27).
- Buttons: `Approve & send` (blue, flexes full), `Edit` (secondary), `Skip/Reject` (link-gray). Edits open the payload inline; save each edit as a lesson. Reject prompts one line: why.
- Questions render as `.opt` answer chips (now pill-shaped), not text dumps.

### Stuck agent / blocked run (16)
When an escalation rule trips mid-run: run timeline with ✓ done steps, a coral-bordered card at the paused step — `STOPPED — YOUR RULE: "…"` label, the question, answer chips, note "Answering resumes the run instantly · your answer becomes a lesson". Remaining steps at 45% opacity. Surfaces: toast, dock badge, Needs-you row, coral story ring.

### Agent profile (21) — restructure into tabs
Header: 64px avatar, name + pencil (opens 19), LIVE pill, bio, "live since". Actions: `Message` (secondary) `Run now` (blue) `Pause` (icon).
Tabs: **Overview** (schedule card, permissions card with ALWAYS STAGED / READ ONLY chips, stat trio runs/approved-as-is/time-saved, danger card) · **Rules** (escalation: team rules + per-agent overrides, editable) · **Lessons** (shared-lesson confirm card, active lessons with Retire) · **History** (run log; `.log-row`).
Bio uses `.ag-bio.wrap` here.

### Messages (22) / DM (23)
List: white cards, avatar, name, timestamp, last-message preview (bold + coral dot when unread). Thread: header with green "● live · answers from its run history" + View profile; me = ink bubbles, agent = white bordered bubbles; suggestion chips (`.suggest`) above the composer ("What did you do today?"); pill composer + round blue send. Agents may reference each other by colored name.

### Groups (24, 17)
Groups list: stacked color tiles, name, "n agents · active {t}", coral dot if a member is blocked. Create modal: name + agent checklist. Group chat (17): `@mention` routes to one agent (render mention in light blue `#8FB0FF` inside ink bubbles); un-addressed questions get replies from each relevant member, each bubble with `.ag-name` in the agent's color; typing indicator per agent. Keep replies broad for general questions, deep when @mentioned.

### Sharing (25)
Two segmented dials: **You contribute** (Nothing / Lessons / Lessons + patterns) and **You consume** (Nothing / Ask me first / Auto-adopt). Copy: "lessons only — never your emails, data, or recordings." Per-agent override rows below. "Ask me first" feeds the confirm card on profiles.

### Settings (18, 26, 30)
Sub-nav: Account · Notifications · Privacy & data · Connected apps · Team · Appearance.
- Notifications: morning brief (one summary), blocked-agent pings, weekly digest — `.tgl` toggles.
- Connected apps (26): per-app card — name, which agents use it + staged/read-only note, last used, `Revoke`. Expired session = yellow border + "paused until you reconnect" + ink `Reconnect` button.
- Privacy & data: delete-all-capture-data danger card → type-`delete`-to-confirm modal (28).
- **Team (30):** members list (avatar, name, email, agents-live count, ADMIN/MEMBER pill, ⋯ menu), pending invites (dashed avatar, Resend/Cancel), `+ Invite` modal (email + Member/Admin segmented + explainer). Lesson-sharing scopes to the team.

### Setup wizard (06–13)
Full-screen, centered column, `.steps` chip row on every step (done=green ✓, active=blue, upcoming=faint blue).
1. **Record (06):** pulsing coral rec-dot + `RECORDING`, Manrope tabular timer, reassurance line, waveform, "Capturing: … edit apps", `Pause` / ink `Finish recording`.
2. **Find work (07):** animated logo (splash keyframes), "Reading your day…", found-workflow cards appear live with agent-color dot + STRONG/MAYBE confidence pill + dashed "still looking…".
3. **Review (08):** "Did I get these right?" — per-workflow card: dot, name, cadence, plain-language description, chips `✓ Looks right / Fix details / Not a workflow`. Continue shows confirm count.
4. **Questions (09):** ONE interview for all agents. Cap 3–4/agent (only behavior-changing questions) + an "Everyone" tab asked once. Per-agent progress pills; card = agent tag, Manrope question, textarea ("Type it like you'd explain to a new hire…"), quick-answer chips. Footer: "Answer the rest later — agents ask in the moment instead."
5. **Safety net (10):** escalation rules — "Anything outbound — always" (BUILT IN, locked), Someone new, Numbers look wrong (±20%), Low confidence (<90%) toggles + free-text "add your own". CTA `Save & build my agents`.
6. **Build (11):** pulsing agent avatar, checklist (done ✓ green / current spinner / pending hollow), "usually under 2 minutes", queue note.
7. **Connect (12):** per-app card: which agents need it + staged note, green `✓ Connected` / ink `Connect`. Continue disabled until required apps connect.
8. **Go live (13):** 5-color dot row, "Meet {name}", agent card with color picker (29 when exhausted), schedule line, `Run a test now` / blue `Go live`.

## 6. Motion inventory

| Where | Animation |
|---|---|
| Launch splash (02) & Find-work loader (07) | `palzaDraw`/`palzaPop`/`palzaRise`, 4.2s loop |
| Recording dot, WAITING-ON-YOU pill | `tracePulse` 1.4–2s |
| Spinners (build step, buttons) | `traceSpin` .9s |
| Typing indicator | `dmTyping` 1.2s staggered |
| Progress bars, toggles, hovers | CSS transitions already in the stylesheet |
| Dock icon | dot while running, coral badge when blocked |

All motion honors `prefers-reduced-motion`.

## 7. Content rules (model-generated strings are unbounded)

- Every one-liner truncates (`.ag-bio`), every full view wraps (`.ag-bio.wrap`, `overflow-wrap:anywhere`).
- Titles from models get a 2-line clamp in cards; full text lives behind the disclosure.
- Coral is reserved for actionable; FYI runs collapse to one quiet gray line with a `Details` link.
- Red left borders, mono uppercase labels, and orange-as-accent are all retired.

## 8. Suggested execution order

1. Swap `app.css` → `palza-app.css` content + fonts + brand strings (pure drop-in, ship same day).
2. Splash + dock icon + logo swap.
3. Shell/sidebar + routing (first-run vs returning).
4. Home restructure + proposal-card restructure (biggest UX wins).
5. Profile tabs + identity popover + color system.
6. Interview hub + safety-net step + stuck-run view.
7. Sharing, Team, Connected apps, modals.
