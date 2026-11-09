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

---

# UX TODO — round 2 (November 9, 2026)

Second batch of user feedback after the round-1 deploy.

## 5. Menu: merge the redundant Network interface screens

**Feedback:** "WLAN interfaces and adapters seems somewhat redundant — simplify into a single pane, or have the adapters screen be a sub of the wlan interfaces."

- Confirmed redundant at the source: `network.interfaces` and `network.wlan_interfaces` are two nodes calling the **same** `GET /network/interfaces` (parity rows 1–2); the second is just a client-side `wlan*` filter (`wlanpi-menud/protocol/samples/manifests/builtin.json:11-30`, poller at `wlanpi_menud/poller.py:92`).

**Fix (recommended: single pane):**
- One **Network > Interfaces** screen: grouped table (Ethernet / WLAN sections — the core response is already `{group: [records]}`, see `blocks.py:144`), with adapter details (model/driver/firmware where present) as sub-rows of the wlan entries.
- Remove `network.wlan_interfaces` from the manifest (keep the node id reserved in the parity list with a "merged into network.interfaces" note so parity tracking stays honest).
- Alternative if the single table gets too tall on the 128x64 panel: keep Interfaces as the only entry and make the WLAN detail a `nav`-reachable sub-screen. Decide during implementation against the panel screen budget.

## 6. WebUI: table row styling — kill the zebra stripes

**Feedback:** "Alternating rows, white text on white background is not visible. Remove the alternating color, leave all rows the darker color, mouse-over just slightly lighter."

- Cause: `uk-table-striped` on both table renderers (`wlanpi_webui/static/js/view_blocks.js:99,137`) — the light stripe fights the dark theme's row text color.

**Fix:** drop `uk-table-striped`; uniform dark rows; hover = slightly lighter (keep `uk-table-hover`, tune the CSS var). Applies to all tables (results, kv fallback, interfaces).

## 7. WebUI: Results & Activity shows only the most recent

**Feedback:** "We have a running list of results — limit to the most recent."

- Cause: `menu.js:274/:535` append into `#rich-job-results`, growing unbounded.

**Fix:** replace the pane's content with the latest result/job snapshot (title, status, view blocks); completed jobs fold into a one-line "previous result" summary that expands on click, or drop entirely — pick the simpler during implementation. No unbounded DOM growth.

## 8. WebUI: rich mode + mirror simultaneously

**Feedback:** "We don't have the ability to do Rich and mirror the display."

- Today the three-way control is exclusive; mirror forces thin mode. The protocol already allows it: a session supports multiple attachments, so a browser can hold **two connections** — its rich private session AND a thin attach to the default session.

**Fix:** add a "Show panel mirror" side-pane toggle that is independent of the mode control. When on (in either mode), open a second thin/default connection and render its view blocks in a collapsible side pane; input in the pane sends to the default session (with the driven-by indicator). Closing the pane closes only that attachment. Logout revokes both connections (existing webui_session revoke covers it — same session id).

## 9. WebUI: rich-mode root screen shows device info like the thin home

**Feedback:** "The main menu page should display info similar to the home page of the thin display."

- The thin home/panel shows hostname, mode, IP, temp etc. from the `device.summary` topic (TUI footer does this at `app.py:795+`); the rich root currently shows only branch cards.

**Fix:** on the rich root (and behind each branch level), render a device-info strip from the subscribed `device.summary` event data — hostname, mode, eth0/wlan0 IPs, public IP, CPU temp — using the existing kv/status renderers. Subscribe to `device.summary` in rich mode (check the hello/subscribe frame already does it; extend if not).

## 10. TUI: navigation feels laggy vs the WebUI

**Feedback:** "If I press up several times, I can see the webui update before the TUI updates."

- Mechanism: every server `view` frame triggers a full synchronous re-render (`app.py:365-380` → `_refresh_ui` at `:444`, rebuilding all screen strings per frame). A burst of keypresses queues N view frames; the TUI repaints all N serially and lands on the final state late, while the browser coalesces to the last frame per animation tick.

**Fix:** coalesce renders in the TUI — on `view`/`event` frames set a dirty flag and repaint at most once per short interval (~50-80 ms, Textual `set_interval`/`call_later` pattern), always rendering the LATEST frame state. Input must still send immediately on keypress (verify `on_key` → `_send_frame` has no await-behind-render path). Add a test: burst of 5 input frames in quick succession results in ≤2 refreshes.

## Verification bar for round 2

- Menu: one Network interfaces entry; table shows grouped Ethernet/WLAN rows on TUI, panel and WebUI; parity list updated.
- WebUI: no light stripes anywhere; hover slightly lighter; results pane bounded (100 invokes → constant DOM size); rich + mirror pane works simultaneously; root shows device info.
- TUI: keypress burst settles in ≤1 render tick after the last frame; no regression in mirroring.
- Container e2e extended: grouped interfaces table assertion, rich+mirror dual-connection assertion, results-pane-bounds assertion.
- All suites green (menud, tui, webui, JS, e2e).

## Verification bar for the round

- TUI: tree hidden by default in a fresh launch; `t` toggles; breadcrumb unaffected; suites green.
- WebUI: click Network > Interfaces in rich mode → table renders without a second click; three-way mode switch reconnects correctly (no reconnect-loop regressions); mirror behavior unchanged.
- Container e2e extended: rich-mode click-to-view assertion + private-thin-session assertion.
- All existing suites stay green (menud 210+, webui, JS, e2e).
