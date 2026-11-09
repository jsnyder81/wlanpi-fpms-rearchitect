# FPMS Re-Architecture — UX TODO (batched fixes)

Collected from user feedback after the Phase 2 device rollout (Nov 9, 2026).
Implement as one coordinated round across the TUI and WebUI panel; each item lists the evidence in code so the fix can be verified against it.

## 1. TUI: hide the menu-tree panel by default

**Feedback:** "Why does the menu structure get visually displayed?"

- The TUI renders the entire menu hierarchy (every node id, `[confirm]/[params]/[provider]` badges) below the active screen — a Phase 1 dev/parity aid, marked "reference only" in code (`wlanpi-menu-tui/wlanpi_menu_tui/app.py:513-516`, renderer at `:781-786`, mounted at `:214`).
- It duplicates the current screen's content, scales with the whole menu (not the current branch), and leaks internal node ids to operators.

**Fix:**
- Do not render the tree section by default.
- Keep the data path (the header breadcrumb needs the tree — `app.py:533-535`, `protocol.py:263`).
- Add a toggle key (`t`) that shows/hides the tree for debugging/parity work; show a dim hint in the footer keybar when hidden.
- Update tests + README key list.

## 2. WebUI rich mode: status/view nodes must show results on click

**Feedback:** "Network > Interfaces shows 'No actions run yet. Select an action from the menu to execute.' But there are no mechanisms to execute an action here. Clicking on the interfaces menu should show results."

- Root cause: branch clicks are purely client-side tree walks (`wlanpi-webui/static/js/menu.js:369-376` `handleNodeClick` → `renderRichMenuTree`); no frame reaches the service, so no `view` frame is ever produced, and the "Results & Activity" column keeps its placeholder (`templates/partials/menu.html:48-51`).
- `network.interfaces` is a **view action** (`view: "table"`, no params — `wlanpi-menud/protocol/samples/manifests/builtin.json:11-20`); the TUI renders it immediately on selection, the panel instead routes it through `selectActionNode`'s detail pane + separate Run button (`menu.js:377-381`).

**Fix:**
- Clicking a **no-param view/status action** invokes it immediately and renders the returned view blocks in the Results column (parity with TUI semantics).
- Clicking a **branch** that has a server-side landing view should fetch/render that view too (see item 4 for the nav-frame question); branches without one keep client-side drill-down, but the Results column placeholder should say what to expect, not "select an action to execute".
- Keep the detail pane only for actions with params or confirm.

## 3. WebUI: add a real rich ↔ thin switch (thin ≠ mirror only)

**Feedback:** "There is also no way to change from Rich to Thin. The only option is to mirror panel."

- The panel has exactly two states: rich/private and mirror (thin attach to the **default** session) — `menu.js:5-8`, toggle at `:707-711`, badge logic at `:231-233`.
- The protocol already supports the missing combination: `hello` with `mode: "thin"` + `session: "new"` = a **private thin session** (server-driven screens, own cursor, no panel mirroring).

**Fix:**
- Replace the single checkbox with a three-way mode control: **Rich (private)** | **Thin (private)** | **Thin (mirror panel)**.
- Thin (private) renders server-pushed view blocks + input mapping (the existing renderer code path) against its own session.
- Mirror stays as-is; session badge reflects all three states.

## 4. Protocol question (blocks part of item 2): node-targeted navigation frame

- Today the service's cursor moves only via `input` frames (up/down/left/right/center/back); a rich-mode browser that wants the server-rendered view for an arbitrary node has no frame to ask for it.
- Options: (a) additive `nav` frame (`{"type":"nav","node":"network"}` → service moves cursor + pushes view) — clean, needs `frames.schema.json` addition + PROTOCOL.md + webui vendored-schema re-sync; (b) client sends an input key-sequence — brittle, rejected; (c) treat item 2 as client-side-only by having rich mode invoke view actions directly (works for leaf views, not branch landing screens).
- Decide during implementation; (a) is the likely pick. Keep the change additive to protocol v1 and re-run the TS typegen + vendor-sync (item covered by the existing CI regen check).

## Out of scope here

- Panel (physical) renderer track, Phase 3 opt-in, and the deferred core endpoints (mode switch, cloud-test #481, capture sessions) — unchanged by this list.

## Verification bar for the round

- TUI: tree hidden by default in a fresh launch; `t` toggles; breadcrumb unaffected; suites green.
- WebUI: click Network > Interfaces in rich mode → table renders without a second click; three-way mode switch reconnects correctly (no reconnect-loop regressions); mirror behavior unchanged.
- Container e2e extended: rich-mode click-to-view assertion + private-thin-session assertion.
- All existing suites stay green (menud 210+, webui, JS, e2e).
