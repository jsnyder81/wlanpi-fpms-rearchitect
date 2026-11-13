# FPMS Re-Architecture Plan

Nov 5, 2026 · @Jake Snyder

## Summary

FPMS becomes a headless **menu service** (`wlanpi-menud`, the layer wlanpi-core's docs call wlanpi-ui) that owns the menu, sessions and jobs, plus independent **surfaces** that connect to it, each over one authenticated WebSocket. Every device operation goes to wlanpi-core; nothing in the menu stack shells out, and surfaces never poll.

The first release ships the service and a TUI, plus the WebUI plugin if time allows. None of them touch the screen or GPIO, so they install next to today's FPMS with no regression risk and give people something real to test. The front panel moves to the new service only when an owner opts in. It becomes the default when the team agrees the parity bar is met: which current FPMS features must work first is a team decision, not a blanket 100%.

What changes from the wlanpi-fpms2 PoC:

1. **A default session that surfaces attach to** instead of one shared cursor: the service holds a persistent default session, so the panel screen, the WebUI and the TUI can show and drive one shared menu state; any client can also open an independent session with its own cursor.
2. **Render-neutral view blocks** instead of OLED text lines.
3. **JSON menu manifests and out-of-process providers**, so others can extend the menu without touching WLAN Pi repos.
4. **Real auth:** kernel peer credentials on a local socket, login first on a proxy socket, and a reserved `wlanpi-menud` core token for the service's own calls.
5. **The panel keeps FPMS's proven hardware and startup code** and swaps only its menu internals.

Remote clients, token scopes in core and a core event stream come after the first release.

## Current state

[wlanpi-fpms](https://github.com/WLAN-Pi/wlanpi-fpms) is one process that draws the screen, reads the buttons, holds the menu and runs the actions itself (`fpms/modules/network.py`, `system.py`, `apps/*`). It also carries years of hardware and startup work the new stack must keep: the boot splash unit, shutdown handling, `Type=notify` readiness, the Pi 5 GPIO chip lookup by driver label, and model detection.

[wlanpi-fpms2](https://github.com/jsnyder81/wlanpi-fpms2) (release 0.1.2) proves the service and client split. What carries over and what changes:

| Area | PoC today | Change |
| --- | --- | --- |
| Service | FastAPI; full `FpmsState` snapshot to every client on each change | Keep; add a reconnect epoch and bounded per-client queues |
| Navigation | Pure `navigator.handle_input()`; one global `NavLocation` | Keep the pure navigator; a default session holds the shared cursor, independent sessions on request |
| Menu | Code-built `menu_tree.py`, 61 leaf actions | JSON manifests |
| Actions | Async handlers returning `PageContent` text lines | View blocks; most become declarative core bindings |
| Core access | HMAC with the shared secret | The secret directory is root-only (0700), so the `wlanpi` user cannot read it. Use a reserved `did=wlanpi-menud` JWT from a root helper |
| Periodic work | Homepage refresh every 5 s, profiler check every 2 s | One loop in the service, publishing only on change |
| Clients | OLED + GPIO, Textual TUI, Cockpit plugin polling every 1.5 s | Keep the TUI; WebUI gains a new Menu panel; Cockpit stays as-is (removal out of scope — only the fpms2/menud prototype had a menu surface); panel evolves from wlanpi-fpms |
| Panel hardware | `Type=simple`, `Requires=`, hardcoded `/dev/gpiochip0`, checks `"WLANPi Pro"` where FPMS uses `"WLAN Pi Pro"`, no splash or shutdown handling | Keep FPMS's code for all of this |
| Auth | None on `/ws`, `/input`, `/navigate` | Auth on every connection |
| Extras | Complications API | Keep; becomes the provider `publish` call |
| Platform | Python 3.9+ | Python 3.13 on Trixie per WLAN Pi conventions |

wlanpi-core is reached through nginx at `https://127.0.0.1:31415`, as the WebUI does today. Its token manager issues revocable JWTs carrying a device id (`did`). The WebUI gets its token from a root helper (`/usr/libexec/wlanpi-webui/get-core-token`, allowed by sudoers, wrapping `getjwt`), which is the pattern this plan copies. `/streaming/capture` shows the WebSocket auth pattern reused here: first frame `{"command":"auth","token":...}` within 10 s, close code 4401 on failure, tokens refused in the URL. Core does not yet have mode switch or cloud test endpoints, and the capture-session API that scanner CSV and pcap actions will move to is designed but not built. FPMS uses all of these today.

## Target architecture

Three layers, each owning one thing: **wlanpi-core** owns the device, **wlanpi-menud** owns the menu and sessions, **surfaces** own pixels and input. A surface never talks to core for menu work, and wlanpi-menud never touches hardware.

&#91;embedded content: target architecture · 5 surfaces, 2 services\]

Arrows show who opens each connection. The upward event stream is a later phase; until then the service polls core in one loop, and surfaces still only receive pushes.

### Repos and packages

| Repo | Package | Runs as | Owns | When |
| --- | --- | --- | --- | --- |
| `wlanpi-menud` (new, seeded from wlanpi-fpms2) | `wlanpi-menud` | `wlanpi`, systemd | Manifests, sessions, navigator, jobs, auth, fan-out, protocol schema | First release |
| `wlanpi-menu-tui` (new) | `wlanpi-menu-tui` | The SSH user | Textual client | First release |
| `wlanpi-webui` (existing) | `wlanpi-webui` | Existing | Browser panel plugin, nginx location | First release if time allows |
| `wlanpi-fpms` (existing) | `wlanpi-fpms` | root | Existing hardware and startup code, plus an opt-in menu backend that renders wlanpi-menud views | After the first release |
| `wlanpi-core` (existing) | `wlanpi-core` | root | Worker API, tokens, reserved `wlanpi-menud` id, parity endpoints | Reserved id first; parity endpoints gate the panel default |
| Mobile app (existing) | n/a | Phone | Rich-mode client | Later |

The protocol schema lives in `wlanpi-menud/protocol/` and publishes Python models and generated TypeScript types, so every surface builds against one source.

### Runtime connections

| From | To | Transport | Auth |
| --- | --- | --- | --- |
| TUI, panel, local providers | wlanpi-menud | `/run/wlanpi-menud/local.sock` | Kernel `SO_PEERCRED` uid and gid |
| nginx (WebUI browsers, later the mobile app) | wlanpi-menud | `/run/wlanpi-menud/proxy.sock`, connectable only by nginx's user | Login required on every connection; peer credentials ignored |
| wlanpi-menud | wlanpi-core | HTTPS to `127.0.0.1:31415` | Reserved `wlanpi-menud` JWT, or the remote user's own JWT |

There are two sockets because every connection that arrives through nginx shows up as nginx's user. Trusting kernel credentials on that socket would trust every remote client. The service has no TCP listener.

## Menu model and protocol

The menu is data, actions are jobs, and every surface speaks one versioned JSON protocol over a single WebSocket.

### Menu manifests

The code-built tree in `menu_tree.py` becomes JSON manifests loaded at startup: the built-in set in `/usr/share/wlanpi-menud/menu.d/`, plugin sets in `/usr/share/wlanpi-menud/plugins.d/<name>/`, local overrides in `/etc/wlanpi-menud/menu.d/`. A JSON Schema, `wlanpi-menud/protocol/menu.schema.json`, is the contract; every file is validated before any node is registered, so a bad plugin is rejected with an error naming the file and field instead of crashing the service. JSON is the only format because the protocol is JSON and one format is simpler to support.

Each node has a stable dotted id (the PoC's ids, such as `network.lldp`, carry over), a label, a parent, an order, visibility rules (device mode, hardware capability, role), optional parameters, and either children or an action. Most actions call one core endpoint and show the result:

```json
{
  "schema": 1,
  "nodes": [
    {
      "id": "network.publicip6",
      "label": "Public IPv6",
      "parent": "network",
      "order": 70,
      "action": {
        "core": "GET /network/info/publicip6",
        "view": "kv",
        "shareable": true,
        "fresh_sec": 60
      }
    }
  ]
}
```

Actions that need logic (multi-step flows, the composed adapter summary from core-worker-api.md section 2.5) are Python handlers inside the wlanpi-menud tree, referenced by name: `"action": {"handler": "network.adapter_summary"}`. Third parties use providers instead (see Extensibility).

**Keep the format small.** A manifest holds ids, labels, parents, order, visibility, parameters and an action that is either a core binding or a handler reference. It gets no conditionals, loops, variables or chained calls; once an entry needs those, it is code.

### Two client modes

| Mode | Used by | Server holds | Client sends |
| --- | --- | --- | --- |
| Thin | Panel, TUI, WebUI front-panel mirror pane (thin-private survives only as a `?panel=private` dev flag since the Nov 10 redesign) | The nav cursor; renders the current view | `input` (up, down, left, right, center, key1 to key3) |
| Rich | WebUI, mobile app | Nothing per screen | `invoke` on a node id; navigates the tree locally, `nav` to pull the server-rendered view for a node |

Both modes share the same jobs, prompts and results.

### Sessions

The service holds one persistent **default session**: its cursor and current view live in the service, not in any connection. The panel attaches to it, so what is on the screen is what any other attached surface sees and can drive. `hello` carries `session: "default"` to attach, or `session: "new"` for an **independent session** with its own cursor — for a second SSH user browsing privately, the WebUI's own tree view, or any application that wants its own state. An independent session ends when its last connection detaches; the default session never ends — it is what the panel wakes up to.

- Input from any surface attached to the default session moves the one cursor, and the resulting `view` fans out to all of them. Concurrent input is last-write-wins; each `view` names the principal that produced it so surfaces may show, for example, "driven from the WebUI".
- A `prompt` on the default session goes to every attached surface; the first `answer` wins and the others see it resolved. A confirmation stays bound to the invoking principal: attached surfaces can see it, but only the invoker can answer it.
- Rich-mode clients navigate locally by definition; since the Nov 10 panel redesign the WebUI is **one surface** — always a rich private session — with the panel mirror as an optional front-pane pane (a second, thin connection). The three-way mode control was dropped; thin-private remains reachable only as a dev flag.
- The default session's state survives surfaces connecting and disconnecting; a reconnecting surface just receives the current `view`.

### Messages

Every frame is `{"v":1, "type":"...", "id":"<client msg id>", ...}`. The server answers requests with the same `id`.

| Direction | Type | Purpose |
| --- | --- | --- |
| C to S | `auth` | First frame on `proxy.sock`: core JWT (WebUI sessions arrive pre-authenticated by nginx) |
| C to S | `hello` | Mode, `session` (`default` to attach or `new` for an independent session), capabilities (screen size, colour), protocol versions, last `epoch` and sequence numbers |
| C to S | `subscribe` / `unsubscribe` | Topics such as `device.summary`, `complications`, `alerts`, `jobs` |
| C to S | `input` | Thin mode button event |
| C to S | `nav` | Move the session cursor directly to `node` (ancestors auto-expanded); service pushes the resulting `view`. Added in the Phase 2 UX polish round (Nov 9) so rich-mode browsers can show server-rendered landing views; additive to protocol v1 |
| C to S | `invoke` | Run a node's action with `params`; returns a `job_id` |
| C to S | `answer` | Reply to a `prompt` (selection, confirmation, parameter form) |
| C to S | `page` | Next page of a large result, by cursor |
| C to S | `cancel` | Detach from a job |
| S to C | `welcome` | `session_id`, session kind (`default` or `new`), `epoch`, principal, roles, negotiated version |
| S to C | `menu` | Full tree on connect and whenever it changes (mode switch, plugin install) |
| S to C | `view` | Thin mode: the session's current screen as view blocks |
| S to C | `prompt` | Ask the session to pick or confirm, for example a scan adapter when core returns `needsSelection` |
| S to C | `job` | `queued`, `progress`, `result` (first page plus cursor), `error`, `cancelled` |
| S to C | `event` | Topic data with a sequence number |
| S to C | `error` | Typed code (`AUTH_FAILED`, `FORBIDDEN`, `CONFLICT`, `VERSION`) and message |

### Parameters, prompts and paging

- A node declares parameters in its manifest (enum, integer, string, adapter). Rich clients render a form; thin clients get one `prompt` per parameter.
- The server can also prompt mid-job. A scan that comes back with `needsSelection` turns into a `prompt` listing core's `candidates`, then re-runs with the chosen interface and namespace.
- Large results (scan, DHCP leases, journal) return a first page and a cursor. Thin clients page with up and down; the server fetches the next page on demand.

### Reconnect and backpressure

- The service picks a new `epoch` each time it starts. A reconnecting client with the same epoch and an unbroken sequence just continues. Otherwise it gets a full snapshot of the menu, its subscribed topics and its own jobs. There is no replay buffer.
- Each client has a bounded outbound queue. When it fills, queued topic updates for that client are dropped and the client gets a fresh snapshot once it drains. A slow client never holds up the others.

### View blocks

Results are a list of typed blocks: `text`, `kv`, `table`, `list`, `qr`, `status`, `progress`, `confirm`. Each surface renders the types it supports and falls back to `text`, which every block must provide.

### Jobs, sharing and conflicts

- Only reads marked `shareable` in their manifest may be shared between sessions or cached under `fresh_sec`. Anything that returns a secret (SSID passphrase, PSKs) is never shareable.
- Mutations are never shared, deduplicated or cached.
- A confirmation is bound to the principal that invoked the job. On the default session every attached surface sees the prompt, but only the invoker can answer it; an independent session's prompts stay private to that session.
- `cancel` detaches the caller. The job stops only when no session is still attached and the action is cancellable.
- The service may show "busy" as a courtesy, but core is the authority: a core 409 is passed through as `CONFLICT` with core's message.

## Extensibility

Third parties extend the system with files and processes, never by adding code to WLAN Pi's virtualenvs.

| Extension point | Mechanism | Who |
| --- | --- | --- |
| New surface | Implement the protocol against the published schema | Anyone |
| Menu entries bound to core endpoints | JSON manifest in `/usr/share/wlanpi-menud/plugins.d/<name>/`, shipped in any `.deb` | Anyone |
| Menu entries with logic, homepage complications | Provider process on `local.sock` with role `provider` | Anyone |
| Python action handlers | In the wlanpi-menud source tree | WLAN Pi maintainers |
| Display and input drivers | In the wlanpi-fpms source tree, behind a small driver interface | Contributed upstream |

wlanpi-menud and wlanpi-fpms ship as dh\_virtualenv packages with private virtualenvs under `/opt`. A third-party `.deb` cannot safely register Python entry points there, and the next upgrade would break it, so there is no in-process plugin path for third parties.

**Providers** connect like any other client, register a menu subtree, answer `invoke` for their nodes and publish complications. A crashing provider loses its own subtree, not the panel; it can run as its own user; and it can be written in any language. The PoC's complications API becomes the provider `publish` call and keeps its TTL behaviour. Plugin and provider node ids are namespaced (`plugin.<name>.*`), and ids that collide with built-ins are refused.

**Complications integration spec (Nov 13, 2026):** the full design for the above now lives in `wlanpi-menud/docs/complications-spec.md` — creation via `complication.publish/remove` frames on `local.sock` (+ `wlanpi-complication` CLI + `plugins.d` catalog declarations), namespaces `wlanpi.*` / `plugin.<name>.*` / `local.<user>.*`, new topics `complications.live/.catalog/.config` (legacy topic and `device.summary` frozen), server-side per-surface display profiles, built-in tiles derived from the poller. Approved with a decision log (§8); **implementation on hold** by user decision.

**Touch screens** have two routes. A large touch display with its own UI (a kiosk browser or a native app) connects as a rich-mode surface and needs no panel code. A small touch display bolted to the panel needs an input driver in wlanpi-fpms that maps taps to on-screen regions; that comes after the panel opt-in ships.

## Panel rendering

Recommendation: ship the panel opt-in on an improved Python renderer, and run a spike on a kernel display driver plus LVGL to decide the renderer for a later panel release. The redesign below fits either path.

### How the screen is drawn today

Every model has a 128x128 colour display: an SSD1351 OLED on the Pro (through luma.oled) and an ST7735 LCD on the others (through FPMS's own driver). The code also has a 240x240 ST7789 path. FPMS draws each frame with PIL `ImageDraw` in DejaVu Sans Mono at 7 to 14 px, plus an icon font.

- **Push cost.** The ST7735 driver converts each frame to RGB565 in a nested Python loop over all 16,384 pixels before writing to spidev at 40 MHz. At that clock a full 32 KiB frame is about 6.6 ms on the wire, so the Python loop, not the bus, is the bottleneck.
- **Full frames only.** Every change redraws and resends the whole screen, which rules out smooth scrolling or transitions.
- **Jagged shapes.** PIL anti-aliases text but not shapes, so rounded corners, circles and icons have stair-stepped edges.
- **Monospace everywhere.** Fixed-width type wastes width on a 128 px screen; at 7 px it is hard to read.

### Options

| Option | What changes | Gains | Costs |
| --- | --- | --- | --- |
| A. Faster PIL path | numpy RGB565 conversion, dirty-rectangle updates, shapes drawn at 2x and downsampled, a proportional UI font and a pixel font for small text | Removes the per-pixel loop, enables partial redraws and smooth shapes, stays in Python inside wlanpi-fpms | Still hand-placed drawing code; no widget toolkit |
| B. Vector renderer | pycairo (Debian `python3-cairo`) for drawing, same push path as A | Anti-aliased shapes, gradients and clipping natively | One more native dependency; text layout still manual |
| C. Kernel driver plus LVGL | Display as a DRM device (`st7735r` or `panel-mipi-dbi` for the ST7735; SSD1351 support in the `ssd130x` DRM driver is a June 2026 patch series, not yet confirmed merged) and buttons as an input device via the `gpio-key` overlay; UI in LVGL | Widgets, themes, animation, partial redraw; the panel no longer needs root for SPI and GPIO | C codebase (LVGL's Python bindings are not a first-class CPython option); needs the WLAN Pi kernel to carry the drivers; the Pro is blocked until SSD1351 DRM support lands; splash and shutdown screens move to the kernel display path |

Option A is the low-risk path that keeps FPMS's hardware and startup code, as the plan already requires. Option C is the bigger step change in feel, but it touches the kernel, the boot path and the language of the panel, so it should earn its place through a spike, not by default. Moving buttons to `gpio-key` input events is worth doing on its own, since it helps every option.

### Spike for option C

- [ ] ST7735 model: bring up the display as a DRM device on the WLAN Pi kernel and run an LVGL demo at 128x128.
- [ ] Buttons through the `gpio-key` overlay as evdev input.
- [ ] Measure frame rate, CPU and RAM against option A on the same board.
- [ ] Check the status of SSD1351 support in `ssd130x` for the Pro.
- [ ] Decide: stay on A, or plan a panel v2 on C.

### Screen redesign

Three visual directions are drafted on the [WLAN Pi Panel Redesign](https://claude.ai/artifact/ArCo4fDqzfxCEAtmQrWMrm) canvas. Each shows the same home, menu, result, selection prompt and confirm screens, laid out at 128x128 and shown at 3x, next to today's look.

| Direction | Idea | Best on |
| --- | --- | --- |
| A. Quiet | Near-black, one accent, proportional type; hierarchy from size and grey levels | Options A or C, with a hinted UI font |
| B. Glance tiles | Home as status tiles, top menu as an icon grid, results as small cards | Option C; needs anti-aliased shapes to look right |
| C. Pixel crisp | Bitmap type, 1-pixel rules, inverted focus bar, readout-style rows | Option A today; sharpest on the real screen |

All three use view blocks unchanged, so the choice of look does not affect the protocol. Picking one is a team decision, ideally after seeing them on a real device.

## Authentication and authorization

wlanpi-core stays the only issuer and verifier of tokens. wlanpi-menud never reads core's HMAC secret; it holds its own reserved token, obtained the same way the WebUI gets one.

### Service identity

- Core reserves `did=wlanpi-menud`, as it already reserves `wlanpi-webui`.
- The wlanpi-menud package ships `/usr/libexec/wlanpi-menud/get-core-token`, which runs `getjwt wlanpi-menud`, and a sudoers entry that lets only the `wlanpi` user run it.
- The service fetches its token at start, keeps it in memory only, refreshes it before `exp`, and fetches a new one after any 401.

### Who connects

| Caller | Socket | Proof | Acts in core as | Session | When |
| --- | --- | --- | --- | --- | --- |
| TUI | `local.sock` | `SO_PEERCRED`, uid in group `wlanpi-menud` | `wlanpi-menud` | Attaches to the default session, or `new` for its own | First release |
| WebUI browser | `proxy.sock` | WebUI session, checked by nginx | `wlanpi-menud`; the WebUI user is recorded in the service's audit log | Mirror attach or its own | First release if time allows |
| Panel | `local.sock` | `SO_PEERCRED`, uid root | `wlanpi-menud` | The default session's screen | Panel opt-in |
| Local provider | `local.sock` | `SO_PEERCRED`, uid in the provider allowlist | `wlanpi-menud` | No session (provider role) | Later |
| Mobile app, third-party | `proxy.sock` | Core JWT in the first frame | Its own JWT | Its own | Later |

### WebUI browsers

Recommended: nginx `auth_request`. Before upgrading `/ui/ws`, nginx asks a WebUI endpoint whether the session cookie is valid. If it is, nginx sets `X-WebUI-User` and `X-WebUI-Session` (overwriting any values the client sent) and proxies to `proxy.sock`.

- wlanpi-menud trusts those headers only on `proxy.sock`, which is mode 0660 and owned by nginx's group, so nothing else can connect to it.
- wlanpi-menud checks that `Origin` matches the device's own host, which blocks cross-site WebSocket hijacking with the cookie.
- On logout, the WebUI backend tells wlanpi-menud over `local.sock` to close every session carrying that WebUI session id.

The fallback, if `auth_request` proves awkward, is a one-time ticket that the WebUI backend mints over `local.sock`, bound to the WebUI session and revoked on logout.

### Remote JWT clients (later)

1. The client opens `wss://<host>/ui/ws`. A first frame with no WebUI headers must be `{"v":1, "type":"auth", "token":"<core JWT>"}` within 10 s. `?token=` in the URL is refused, matching `/streaming/capture`.
2. wlanpi-menud validates the token with a new core introspection endpoint that accepts the `wlanpi-menud` service token. It returns `valid`, `did`, `exp` and `jti`.
3. On failure: `error AUTH_FAILED`, close code 4401. The result is cached for at most 60 s or until `exp`, then checked again.
4. Jobs for that session forward the user's own JWT to core, so core logs the real `did` and a revoked token stops working at core too.

### Roles are not a security boundary

wlanpi-menud roles (`viewer`, `operator`, `admin`; the panel is `admin`) decide which menu entries a session sees and may run. They do not protect the device. Core tokens carry no scope, so anyone holding a remote JWT can call core directly with full power, whatever the menu allows.

Before any remote client gets less than full trust, core must add a `scope` claim and enforce it on its own endpoints. Until then, issue remote tokens only to people who would be trusted with admin rights on the device.

## wlanpi-core integration

Every action runs through core's REST API. Core work splits into one small item the first release needs, the endpoints that gate the panel default, and later work.

### The PoC against current core

The PoC was written before core's auth and API hardening, so its core client is a starting point, not something to carry over unchanged. Checked against wlanpi-core `main` (5 Nov 2026):

| PoC assumes | Core now | Plan |
| --- | --- | --- |
| `http://localhost:31415/api/v1` | 31415 is HTTPS only (nginx, self-signed device cert); gunicorn listens on `/run/wlanpi_core.sock` | Call `https://127.0.0.1:31415` and pin the device certificate |
| HMAC with `shared_secret.bin` | Secret directory is root-only (0700) | Reserved `wlanpi-menud` JWT from a root helper |
| `POST /profiler/purge/files` and `/profiler/purge/reports` | One `POST /profiler/purge` | Rebind |
| `/system/updates`, `/system/updates/install` | Not present | Dropped from the new menu |
| `/system/mode/switch`, `/utils/cloud-test/{vendor}` | Not present | Core parity asks (table below) |
| Any token works whenever it is presented | A token whose `iat` is ahead of the device clock is refused with `AUTH_CLOCK_NOT_SET`; boot-relative expiry (`bexp`) is checked | Service re-mints its token on that error; test on a board that boots offline with a stale clock |
| Hotspot, server and Bluetooth actions always offered | Transmit units (hostapd, wpa\_supplicant, Bluetooth) are refused on the Go and on unknown models | Hide those nodes with a model-capability visibility rule |
| PAM endpoints usable by any local caller | Only the `wlanpi-webui` token or localhost HMAC | wlanpi-menud never calls PAM |
| Capture WebSocket open | First-frame JWT auth, close code 4401, no token in the URL | Same pattern for wlanpi-menud remote clients |

The other 30 core paths the PoC calls still exist. Response shapes have moved too (for example, scan now returns `networks[]`, `selectedAdapter` and `needsSelection`), so the service's core client should be generated from core's `docs/openapi.json`. CI should run contract tests against a pinned core version so the next core change fails a build instead of a menu item.

### Core changes

| Change | Needed for | Notes |
| --- | --- | --- |
| Reserve `did=wlanpi-menud` and allow `getjwt wlanpi-menud` through the root helper | First release | Same pattern as `wlanpi-webui` |
| `POST /system/mode/switch` | Panel parity | core-worker-api.md Stream A; not built yet |
| `GET /utils/cloud-test/{vendor}` | Panel parity | Stream C; not built yet |
| Scanner CSV and pcap file actions | Panel parity | Port to core's capture-session API (core-worker-api.md Stream D: capture sessions and their files). Gap: designed in wifi-capture.md, not implemented yet |
| Token introspection, accepting the `wlanpi-menud` token | Remote JWT clients | Returns `valid`, `did`, `exp`, `jti` |
| `scope` claim, enforced by core | Giving remote clients less than full trust | See Roles above |
| Event stream `WS /api/v1/events` | Removing the service's polling loop | Needs a policy decision (below) |

### Parity list

The tracker for the panel default is a parity list built from current FPMS's menu: one row per menu item, with the core endpoint it needs and that endpoint's status. The core-worker-api.md gap matrix (`docs/api-gap-matrix.csv`) feeds it. The team reviews the list and marks each item required, deferred or dropped; the panel stays opt-in until the required items work on every supported model. System updates is already dropped.

The service and TUI do not wait for parity. A node whose endpoint is missing is hidden with `requires_core = "<path>"`, which is acceptable for a new tool people choose to try. For the panel, the team's parity decision says which hidden items are acceptable.

### Polling until core can push

Until the event stream exists, the service runs one loop for homepage and status data and publishes to subscribers only when a value changes. Surfaces never poll.

The later event stream would forward changes from sources that already push, so core needs no polling loops either: rtnetlink for links, addresses and routes; inotify on `/etc/wlanpi-state`; systemd D-Bus for allowed services; udev for USB adapters and battery. core-worker-api.md section 3 says core holds no session or job state and the UI layer polls REST. A stream that only forwards events keeps core stateless, but it is still a new responsibility and needs the core maintainers' agreement. If they decline, the service keeps its polling loop.

## Development phases

The February release ships the service and the TUI (Phases 0 and 1), plus the WebUI plugin if time allows. None of it touches the panel, so today's FPMS keeps running unchanged while people test. The panel follows as an opt-in, and becomes the default when the team agrees the parity bar is met.

&#91;embedded content: roadmap · 7 phases, 1 release cut\]

### Phase 0: Contracts

Repos: `wlanpi-menud`, `wlanpi-core` (issues only). Keep this short.

- Protocol and manifest JSON Schemas in `wlanpi-menud/protocol/`.
- Core compatibility audit: finish the PoC-against-core table and generate the core client from `docs/openapi.json`.
- Parity list from current FPMS's menu: each item, the core endpoint it needs, and that endpoint's status, for the team to mark required, deferred or dropped.
- Resource budget: a RAM and CPU ceiling for the service plus TUI on the smallest supported board.
- Upgrade and rollback plan, including the A/B image layout (`/home` persists, `/var` does not).
- Core issues filed: reserved `wlanpi-menud` id, parity endpoints.

Exit: schemas published, parity list and core issues filed.

### Phase 1: Service and TUI (February)

Repos: `wlanpi-menud` (seeded from the PoC's `state/`, `nav/`, `actions/`), `wlanpi-menu-tui`, `wlanpi-core` (reserved id).

- Default session plus independent sessions (shared cursor, attach semantics); JSON manifests; first-party handlers; generated core client over HTTPS.
- `local.sock` with `SO_PEERCRED`; service token from the root helper, re-minted on 401 or `AUTH_CLOCK_NOT_SET`.
- Jobs with the sharing, confirm and cancel rules; prompts, parameters, paging; epoch and bounded queues.
- One polling loop for status data.
- TUI in its own repo, built on the generated models.
- Python 3.13, `pyproject.toml`, dh\_virtualenv under `/opt/wlanpi-menud`, systemd unit as `wlanpi`. Installs next to wlanpi-fpms and touches no hardware.

Exit: the TUI runs every menu item whose core endpoint exists; two independent TUI sessions navigate separately, and two attaching TUI sessions mirror and drive one default session; usage stays inside the resource budget; contract tests pass against the pinned core; FPMS is unaffected.

### Phase 2: WebUI plugin (February if time allows)

Repos: `wlanpi-webui`, `wlanpi-menud`.

- `proxy.sock`, nginx `/ui/ws` location with `auth_request`, Origin check, logout closes sessions.
- Browser rendering of view blocks. Cockpit is **not** retired in this project (decision Nov 9: only the fpms2/menud prototype had a menu surface; the existing Cockpit plugin stays untouched).

Exit: the TUI and two browser tabs run concurrently; WebUI logout ends that user's browser sessions.

#### Phase 2 UX polish round (November 9, 2026)

Post-rollout user feedback, batched in *FPMS Re-Architecture - UX TODO.md* (repo root) and implemented across `wlanpi-menu-tui`, `wlanpi-menud` and `wlanpi-webui`:

1. **TUI menu tree hidden by default** — the full-hierarchy tree panel was a Phase 1 dev/parity aid ("reference only"); it is now off, toggled with `t`. The client still holds the tree for breadcrumbs.
2. **`nav` frame added to the protocol** (additive v1, see Messages) — lets a rich-mode client fetch the server-rendered view for any node.
3. **WebUI rich mode: click shows results** — no-param view/status actions (e.g. Network > Interfaces) now invoke on a single click and render in the Results column; branch cards use `nav` for landing views. Fixes the "No actions run yet" dead-end.
4. **WebUI three-way mode control** — Rich (private) | Thin (private, `mode=thin session=new`) | Thin (mirror panel). Previously thin existed only as the panel mirror.
5. **Container e2e extended** — click-to-view, private-thin-session and `nav` assertions pin all of the above.

#### Phase 2 UX polish round 2 (November 9, 2026)

Further feedback, batched as items 5–10 in *FPMS Re-Architecture - UX TODO.md*:

1. **Menu simplification** — `network.wlan_interfaces` merged into a single grouped **Network > Interfaces** screen (core's `/network/interfaces` already returns `{group: [records]}`; the second node was the same endpoint re-filtered). Parity rows updated.
2. **Table styling** — zebra stripes removed (white-on-white rows), uniform dark rows with a slightly-lighter hover.
3. **Results pane** — shows only the most recent result (previous folds to a one-line summary); no unbounded list.
4. **Rich + mirror simultaneously** — a "Show panel mirror" side pane independent of the mode control, implemented as a **second connection** (thin attach to the default session) rather than a mode change; logout revoke covers both.
5. **Rich root** — device-info strip (hostname, mode, IPs, temp) from the `device.summary` topic, mirroring the thin home screen.
6. **TUI render coalescing** — the TUI repainted once per arriving frame, so keypress bursts lagged the browser; now a dirty-flag repaint at ~60 ms renders only the latest state, input still sends immediately.

Review round initially returned REQUEST-CHANGES (a real-browser blocker: `HTMLCollection.indexOf` misuse hidden by array-based test mocks; plus the leftover Adapters entry). Fixes landed with **HTMLCollection-faithful mocks** so the bug class is dead in tests; re-review **APPROVED** with mutation proofs. One item required an **additive v1 schema change** (optional `eth0`/`wlan0` on `device.summary`, since the payload was `additionalProperties: false`). Two non-blocking observations logged for later: no menud unit test for the poller's eth0/wlan0 fields; e2e 5f asserts Eth0 only.

#### WebUI panel redesign (November 10, 2026)

User released the panel from backward compatibility ("no need to maintain the existing menu plugin"); Claude reviewed, designed (`wlanpi-menud/docs/webui-menu-redesign.md`) and implemented on `wlanpi-webui` branch `redesign/webui-menu-panel`; Antigravity verified — APPROVE, zero findings.

- **One surface**: `/menu` is always the rich private session; the panel mirror is an optional front-panel pane (second thin connection); thin-private survives only as a `?panel=private` dev flag. The mode control is gone.
- **Every node is a URL-hash page**; results render on the action's own page — no Results column.
- **Navigation cannot change the device**: read-only no-param views run on arrival; mutating/paramful/confirm actions require an explicit Run (user accepted two-click over panel one-click). Verified by network-level e2e capture.
- Always-visible device status bar + Home device card; connection loss keeps content under a banner with backoff countdown and `hello.resume`.
- Protocol and schemas **unchanged** (presentation-only); Playwright/headless-Chromium replaced mock-based browser e2e (147 assertions; verify image ~1.6 GB).
- Deployed to the M4+ test device Nov 10: 9/9 live checks through real nginx, including two-click mutation safety, mirror-pane tracking, and disconnect/resume. Rollback deb preserved on device; evidence in `/tmp/opencode/device-deploy-redesign.md`.

### Phase 3: Panel opt-in

Repos: `wlanpi-fpms`, `wlanpi-core` (parity endpoints).

- Keep FPMS's hardware and startup code: splash unit, shutdown handling, `Type=notify`, the Pi 5 GPIO chip lookup, model detection.
- New setting in `/etc/wlanpi-fpms.conf`: `menu_backend = legacy` (default) or `wlanpi-menud`. In `wlanpi-menud` mode the panel attaches to the default session over `local.sock`, renders its views and sends button input; a TUI or browser attaching to the same session mirrors the screen live.
- `Wants=wlanpi-menud.service`, not `Requires=`. If the service is down, the panel shows a local "service unavailable" screen with the IP address and keeps reconnecting.
- Rollback is flipping the setting back.
- No new features go into the legacy menu while both exist.

Exit: every item the team marked required works on every supported model; a later release flips the default.

#### Phase 3 first cut: end-state panel client (November 11, 2026)

`wlanpi-fpms` branch **`phase3/menud-client`** (4 commits from `87b96cc`, published to jsnyder81/wlanpi-fpms): skips the opt-in mechanism — the legacy menu, pages and action modules are **deleted**, and `modules/menud_client/` makes the panel a pure thin-mode client of the service (default session, all 8 block types, prompts, paging, job cancel, epoch/seq resume with backoff, driven-by indicator, menu-unavailable screen with device IPs, `Wants=` not `Requires=`). Reviewed by Antigravity — APPROVE, zero defects (212-frame protocol transcripts, mutation checks, lintian-clean deb). **Deployed to the M4+ test device Nov 11** (fpms package swap only; rollback deb saved on-device at `/home/wlanpi/rollback/`): remote verification passed — thin client connects, mirrors TUI/WebUI navigation frame-for-frame, clean backoff + `hello.resume` across a menud restart, 0.40% idle CPU. Physical button presses and OLED rendering pending user confirmation.

Open policy calls before this shape could ship: kv secret values render unmasked on the panel; `Recommends:` vs `Depends:` on wlanpi-menud; and the plan's rollback-by-setting does not exist on this branch — reverting means reinstalling the old package.

### Later phases

| Phase | Delivers | Depends on |
| --- | --- | --- |
| 4 Remote clients | JWT handshake on `proxy.sock`, mobile app rich mode | Core introspection; core scopes before any limited-trust tokens |
| 5 Push from core | Core event stream; the service drops its polling loop | Core maintainers agreeing to the policy change |
| 6 Extensions | Provider docs and an example provider; a touch input driver for the panel | Phase 3 |

## Risks and open questions

### Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Core parity endpoints slip | Panel stays opt-in longer | Nothing else waits on them; the service, TUI and WebUI still ship |
| Core keeps changing under the service | Menu items break silently | Generated client, contract tests against a pinned core in CI |
| Memory and CPU on smaller boards | Another Python service on a constrained board | Budget set in Phase 0, measured at Phase 1 exit |
| Device boots offline with a stale clock | Tokens refused with `AUTH_CLOCK_NOT_SET` | Service re-mints its token; test this case explicitly |
| Two menus during the opt-in period | Fixes made twice | Legacy menu frozen; new items go to manifests only |
| Remote tokens are full-power until core has scopes | A leaked token can reboot the device | Issue remote tokens only to people trusted with admin rights |
| Service down while the panel is opted in | Blank screen | `Wants=`, a local "service unavailable" screen, flip the setting back |
| Event stream declined by core maintainers | Service keeps polling core | One loop, publish on change; surfaces unaffected |

### Decisions needed

- [x] Name: `wlanpi-menud` for the service and `wlanpi-menu-tui` for the TUI (decided).
- [x] Sessions: one persistent default session that surfaces attach to (panel, mirroring TUI/WebUI), plus independent sessions on request (decided).
- [ ] Core: approve reserving `did=wlanpi-menud` and the root helper.
- [ ] Core: owners and timing for mode switch and cloud tests.
- [ ] Core: owners and timing for the capture-session API (Stream D), which scanner CSV and pcap actions will use.
- [ ] Team: which parity-list items are required before the panel default flips?
- [ ] WebUI: nginx `auth_request` or one-time tickets?
- [ ] How does the mobile app get its JWT, and what lifetime should app tokens have?
- [ ] Event stream in core: yes or no?

Sources: [wlanpi-fpms](https://github.com/WLAN-Pi/wlanpi-fpms), [wlanpi-fpms2](https://github.com/jsnyder81/wlanpi-fpms2), [wlanpi-core](https://github.com/WLAN-Pi/wlanpi-core) (`core/auth.py`, `core/token.py`, `core/security.py`, `core/transmit_policy.py`, `api/api_v1/endpoints/streaming_api.py`, `install/etc/wlanpi-core/nginx/wlanpi_core.conf`, `docs/core-worker-api.md`), [wlanpi-webui](https://github.com/WLAN-Pi/wlanpi-webui) (`install/etc/sudoers.d/wlanpi-webui`).
