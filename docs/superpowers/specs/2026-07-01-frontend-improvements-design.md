# Design: Frontend UX Improvements (chat list ordering, right column, hover glitch, empty chat)

> **Status: SPEC — planning only.** Four independent, mostly small frontend changes batched
> into one design because they all touch `services/frontend/src`. Each can ship on its own; the
> implementation plan sequences them from lowest to highest risk. No backend behavior changes
> are required except an optional belt-and-suspenders tweak for item #1 (see below).

All line references verified against the branch on 2026-07-01.

---

## Scope

Four asks, in the user's words:

1. **Chat list ordering** — selecting/viewing a chat should NOT move it to the top of the list.
   Only *using* a chat (sending a message → real activity) should make it "most recent."
2. **Right column hidden by default** — the right inspector column should start hidden, and
   remember the user's last open/closed choice (persisted, like the left sidebar already is).
3. **Hover glitch in the left column** — hovering a chat row briefly changes its size (~half a
   second) before settling. Eliminate the reflow.
4. **New-session welcome page** — an empty chat should render a distinct, centered welcome view
   (animated logo → greeting → mode-aware subtext → centered composer → mode-aware starter chips),
   modeled on Claude Desktop, replacing the one-line "Ask anything to get started." placeholder.
   The visual design is supplied as a high-fidelity prototype at `~/Downloads/new_session/`;
   **where it conflicts with live code, live code wins** and the design is adapted (see Item #4).

Non-goals: no backend session-store or protocol schema changes; no signed-in-user name in the
greeting until one is actually plumbed into client state (time-of-day only for now); no
centered-then-docking composer *animation* (the composer is reused as-is, repositioned by branch).

---

## Item #1 — Chat list ordering by real activity

### Current behavior (root cause)
- Opening a chat sends `session.open`; the ws_gateway handler replies with a `session.updated`
  frame ([`server.py:391`](../../../services/ws_gateway/server.py)) followed by `session.history`.
- The client reducer handles `session.updated` by **blindly unshifting** the session to the front
  of the list ([`useLabmateWS.ts:234-241`](../../../services/frontend/src/hooks/useLabmateWS.ts)):
  ```ts
  const rest = state.sessions.filter((s) => s.id !== frame.session.id);
  return { ...state, sessions: [frame.session, ...rest] };
  ```
- So merely *opening* a chat reorders the list to the top — the reported bug.

Crucially, the backend `updatedAt` is already an authoritative "last real activity" timestamp:
it is bumped on `create`, `rename`, and `add_turn` — **not** on `get`/open
([`sessions.py:34,59,86`](../../../services/ws_gateway/sessions.py);
[`mongo_session_store.py`](../../../services/ws_gateway/mongo_session_store.py) `create`/`rename`/`add_turn`).
The store's `list()` already sorts by `updatedAt` descending. Only the *client reducer* ignores
the timestamp and orders by frame-arrival instead.

### Approaches considered
- **A (recommended): client sorts by `updatedAt` descending.** On `session.updated`, replace the
  session in place (or insert if new) and re-sort the list by `updatedAt` desc. Because open
  returns the session with its *unchanged* `updatedAt`, re-sorting keeps it exactly where it was;
  a real turn bumps `updatedAt` on the backend, so the next `session.updated` naturally floats it
  to the top. This makes ordering derive from the single authoritative field and is robust no
  matter which event emits `session.updated`.
- **B: backend stops emitting `session.updated` on open.** Emit only `session.history` (+ a light
  "set active" signal). Smaller diff, but leaves the blind-unshift latent for any other caller and
  still relies on frame order rather than the timestamp. Rejected as the primary fix.
- **C: do both.** A as the real fix; optionally also stop emitting `session.updated` on open as
  belt-and-suspenders (avoids a redundant no-op re-render). Optional, low value — deferred.

### Design (Approach A)
Change the `session.updated` reducer branch to:
1. Upsert `frame.session` into `state.sessions` (replace matching id, else append).
2. Return the list sorted by `updatedAt` descending, with a stable tiebreak on `id` so equal
   timestamps don't jitter between renders.

`activeSessionId` handling is unchanged — selecting a chat still sets it active (the highlight and
history replay are driven separately by `session.history` / active id, not by list position).

### Test impact
The existing test **`useLabmateWS.test.ts:268`** ("session.updated moves an existing session to
the front") encodes the *old* blind-unshift contract and MUST be rewritten to assert
`updatedAt`-ordering instead: emitting `session.updated` for an already-present session with an
*unchanged* `updatedAt` keeps its position; only a newer `updatedAt` moves it up. Add a case that
mirrors the bug: open (same `updatedAt`) does not reorder; a turn (newer `updatedAt`) does.

---

## Item #2 — Right column hidden by default (persisted)

### Current behavior
- `rightView` initializes to `'skills'` ([`ChatScreen.tsx:1804`](../../../services/frontend/src/components/chat/ChatScreen.tsx)),
  and `showRight = debug || rightView !== null` (`:1896`), so the 380px right column renders on
  first paint.
- The left sidebar already models the desired pattern: `sidebarCollapsed` is lazy-initialized
  from `localStorage['lm.sidebarCollapsed']` and `toggleSidebar` writes it back
  (`:1813-1829`).

### Design
Mirror the sidebar pattern for the right column:
- Lazy-init `rightView` from `localStorage['lm.rightView']`. **Default (no stored value) = hidden
  (`null`).** Persist the last non-debug view choice (`'skills' | 'files' | null`) whenever the
  user opens/closes it via `onSkills` / `onFiles`.
- Keep `debug` orthogonal (debug still force-shows the column when on; it is session state, not
  persisted here).
- Extract a tiny persistence helper so the read/write is in one place instead of duplicating the
  sidebar's inline try/catch (small helper module — see plan).
- **Composes with Item #4:** the final gate becomes `showRight = (debug || rightView !== null) &&
  turns.length > 0`, so the right panel is also hidden on the welcome view. Item #4 adds the
  `&& turns.length > 0` clause; Item #2 owns the persistence/default.

### Test impact
Add a test: with no `localStorage` value the right column (`layout-right` / the 380px panel and
its Skills/Files content) is absent on first render; toggling Skills shows it and writes
`lm.rightView`; a fresh mount with that value restores the view. Guard the tests that assume the
Skills panel is visible by default (search `ChatScreen.test.tsx` for panel assertions and open the
panel explicitly first).

---

## Item #3 — Left-column hover size glitch

### Current behavior (root cause)
`SessionItem` swaps its content based on `isHovering`
([`ChatScreen.tsx:1012-1055`](../../../services/frontend/src/components/chat/ChatScreen.tsx)):
- **Hovering:** renders the rename/delete action buttons in the title row AND hides the meta line
  via `{!isHovering && meta && ...}` (`:1053`).
- **Not hovering:** hides the buttons, shows the meta line ("Chat · N turns").

Removing the meta line on hover changes the row's height (one text line shorter), so the row
shrinks while hovered and grows back on mouse-leave — the ~half-second size jump. (The action
buttons themselves fit within the existing title row and are not the cause; the meta-line toggle
is.)

### Approaches considered
- **A (recommended): keep the meta row always mounted; overlay the actions.** Always render the
  meta line so vertical size is constant. Show the rename/delete buttons on hover positioned so
  they don't displace layout — either absolutely positioned in the row's top-right, or occupying
  the same fixed-height slot whether or not they're visible (toggle `visibility`/opacity, not
  presence). Zero reflow.
- **B: reserve space for whichever element is hidden.** Give both the actions and the meta line a
  fixed min-height container and toggle visibility instead of mounting/unmounting. Equivalent
  result; slightly more markup.

Both remove the reflow; A is the cleanest. The row keeps: title + mode icon always; meta line
always; action buttons revealed on hover without moving anything.

### Design (Approach A)
- Always render the meta line (drop the `!isHovering` guard).
- Render the action buttons in a fixed-size container that is present in both states, toggling
  `opacity`/`visibility` (or `pointer-events`) on hover rather than conditionally mounting them,
  so the title row's height and the buttons' footprint never change.
- Preserve current behavior: buttons `stopPropagation`, rename switches to the inline input, delete
  confirms. Keyboard focus should also reveal the actions (accessibility bonus, low cost).

### Test impact
Add/adjust a `SessionItem` (or `Sidebar`) test asserting the meta line is present regardless of
hover state, and that actions are reachable on hover. A DOM-height assertion is brittle in jsdom;
prefer asserting the meta text stays in the document across hover/unhover.

---

## Item #4 — New-session welcome view (centered, Claude-desktop pattern)

The visual design is supplied as a high-fidelity HTML prototype + handoff at
`~/Downloads/new_session/` (`README.md` + `Labmate.dc.html`). It replaces the empty center pane
with a vertically-centered welcome: animated orbital logo → greeting → mode-aware subtext →
centered composer → a row of three mode-aware starter chips. The right panel is hidden while the
welcome shows.

### Guiding principle (per user directive)
**Live code is the source of truth. Where the prototype conflicts with the live frontend, the
prototype yields and the design is adapted.** The prototype is a look-and-behavior reference
(its `<x-dc>`/`{{ }}` template runtime is pseudocode), not code to copy. The reconciliations below
are deliberate deviations from the prototype in favor of live code.

### Current behavior
When `turns.length === 0`, the center pane renders a single centered line, "Ask anything to get
started." ([`ChatScreen.tsx:1950-1954`](../../../services/frontend/src/components/chat/ChatScreen.tsx)),
inside the same scroll container and above the same bottom `Composer`, `ContextStrip`, and (when
open) right panel used for populated chats.

### Prototype-vs-live reconciliations (design changes made to fit live code)

| Prototype approach | Live truth | Resolution (live wins) |
|---|---|---|
| Explicit `newChat: boolean` state, toggled true by `newSession` and false by send / starter-click / `setMode` / `pickSession`. | No such state. The center already keys its empty branch on `turns.length === 0`. Live actions: `newChat()` creates+selects a session; `openSession` selects one; `mode` is local UI state. | **Trigger the welcome on `turns.length === 0`** (the active session has no turns). New/empty session → welcome; first send creates a turn → thread; selecting a populated session → thread; selecting an empty one → welcome. Switching mode keeps the welcome and updates its copy **live** (the prototype only dismissed on mode-switch because its mock loaded a populated session — not real behavior). |
| A static composer *card* (placeholder text + mode chip + `thinking N` + send glyph). | The live `Composer` (`ChatScreen.tsx:1358`) is a full component: own text state, @-mention autocomplete, compact-command handling, node/budget status row. | **Reuse the live `<Composer>`**, centered in the welcome. The README itself says "visually identical to the docked composer" — reuse satisfies that and keeps one behavior. Accept the live Composer's own footer/status row rather than rebuilding the prototype's chip row. |
| Bespoke inline SVG badge (62×62 gradient tile, exact shadow, 38px orbital mark). | `LabmateMark` (`components/LabmateMark.tsx`) `variant="tile"` renders the same gradient tile (radius = size·0.27 ≈ 18px at 62; inner mark = size·0.64 ≈ 40px) and orbital SVG with `spin` animation. | **Reuse `<LabmateMark size={62} variant="tile" spin="slow" />`.** Accept its radius/shadow values; do not hand-inline the SVG the README already says is a shared component. |
| Greeting `Good evening, Jordan` (name hardcoded; README says derive name from signed-in user). | No signed-in display name exists anywhere in the WS state/events (verified). | **Time-of-day greeting only** (`Good morning/afternoon/evening`), derived from `new Date()`. The optional name is deferred until a user display name is actually plumbed into client state — do not invent one. |
| Starter chip "seeds the composer/opening message" then dismisses. | Live `Composer` owns its text internally; no external seed hook. | Add ONE **optional, backward-compatible `seed` prop** to the live `Composer` (the only change to existing live code) so a starter click prefills the text; the welcome then dismisses naturally when the user sends (a turn appears). This is an additive extension, not a conflict. |
| `showRight = (debug || rightView !== null) && !newChat`. | Live `showRight = debug || rightView !== null` (`:1896`). | **`showRight = (debug || rightView !== null) && turns.length > 0`** — hide the right panel while the welcome shows. Composes with Item #2. |
| Top-bar breadcrumb reads `New chat` while `newChat`. | Live `sessionTitle = activeSession?.title ?? 'New session'` (`:1901`). | `sessionTitle = turns.length === 0 ? 'New chat' : (activeSession?.title ?? 'New session')`. |
| Mode-aware subtext + starter labels (new copy not in the app). | `MODE_META` has `icon/label/noun/chip/budget` but no subtext/starters. | **Additive** per-mode content map (subtext + 3 starters), reusing `MODE_META` where it overlaps. No conflict. |

### Component structure (fits live patterns)
- New file `services/frontend/src/components/chat/NewSessionWelcome.tsx` — a **presentational**
  component: renders the `LabmateMark` badge, greeting, mode subtext, a **composer slot**
  (`composer: ReactNode`), and the starter chips. It knows nothing about turns/sessions/WS; it
  takes `mode`, a `greeting` string, an `onStarter(prompt)` callback, and the composer element.
- New file `services/frontend/src/components/chat/newSessionContent.ts` — **pure helpers**
  (unit-testable, matching the codebase's "test exported helpers" style):
  - `greetingFor(now: Date, name?: string): string` → `Good {morning|afternoon|evening}[, name]`.
  - `welcomeCopyFor(mode: Mode): { subtext: string; starters: { icon: string; label: string; prompt: string }[] }`
    — copy transcribed from the handoff (§State), with `prompt` = the seed text for the composer.
- `ChatScreen` builds the `Composer` element **once** (`const composerEl = <Composer … seed={seed} />`)
  and renders `turns.length === 0 ? <NewSessionWelcome composer={composerEl} …/> : <>{thread}{ContextStrip}{composerEl}</>`.
  Because it is the same element used in mutually-exclusive branches, **exactly one Composer ever
  mounts** — no double-composer, no prop-list duplication.
- Starter seeding: `ChatScreen` holds `const [seed, setSeed] = useState<{text:string; nonce:number}|null>(null)`;
  `onStarter={(prompt) => setSeed({ text: prompt, nonce: <increment> })}`; the `Composer`'s new
  `seed` prop applies the text on nonce change.

### Test impact
- `newSessionContent.test.ts`: `greetingFor` returns the right time-of-day word across boundary
  hours and appends the name only when given; `welcomeCopyFor` returns 3 starters and the right
  subtext for each mode.
- `NewSessionWelcome.test.tsx` (isolated render): badge (`orbit-mark` testid from `LabmateMark`),
  greeting text, subtext, exactly 3 starter chips present; clicking a chip calls `onStarter` with
  that starter's `prompt`; the passed-in composer slot renders. No `<ChatScreen>` harness needed.
- `Composer` seed: a focused test that setting the `seed` prop (new nonce) populates the textarea
  and that omitting it is a no-op (backward compatible).
- The `turns.length === 0 → welcome / else → thread` switch and the single-Composer invariant are
  covered structurally (the one-line conditional + the reused element) and verified manually in the
  running app (jsdom has no `<ChatScreen>` harness — see Cross-cutting).

---

## Cross-cutting notes

- **All four are client-only** except the optional #1-C backend tweak (deferred). No protocol,
  session-store, or orchestrator changes.
- **Persistence keys:** reuse the `lm.*` localStorage namespace (`lm.sidebarCollapsed` exists;
  add `lm.rightView`). Wrap all access in try/catch (jsdom / privacy-mode safe), matching the
  existing sidebar code.
- **Testing:** `vitest run` in `services/frontend`. The house pattern is **export the unit and
  test it in isolation** (pure helper, or a small presentational component with simple props) —
  there is NO `<ChatScreen>` render harness (it would need `electronAPI`/`useWorkspace` mocks),
  and this spec does not add one. Assert structure/behavior, not pixel geometry.
- **Independence & sequencing:** ship in ascending risk order — #3 (pure style), #2 (default +
  persistence), #1 (reducer + test rewrite), #4 (welcome view; depends on #2 for the `showRight`
  gate). Each is a self-contained commit.

## Acceptance criteria

1. Opening/viewing a chat does not change its position in the list; sending a message moves it to
   the top. Ordering follows `updatedAt` desc.
2. The right column is hidden on first run and restores the user's last open/closed choice across
   launches.
3. Hovering a chat row reveals rename/delete with no change in row height or position — no
   size jump.
4. A new/empty chat (`turns.length === 0`) renders the centered `NewSessionWelcome` view —
   reused `LabmateMark` badge, time-of-day greeting, mode-aware subtext, the reused centered
   `Composer`, and three mode-aware starter chips; clicking a chip prefills the composer. The
   top bar reads "New chat" and the right panel is hidden. Sending a message (or opening a
   populated session) shows the normal conversation. Exactly one `Composer` mounts at any time.
