# FPMS Re-Architecture — Team Decisions Needed

Nov 8, 2026 · @Jake Snyder · Companion to [FPMS Re-Architecture Plan.md](FPMS%20Re-Architecture%20Plan.md)

These are the open questions from the plan plus ones that surfaced during Phase 0 (contracts), with what we've learned so far. Each has an owner group and a recommendation where we have one. Phase 1 (service + TUI) is underway and does **not** block on most of these; the ones that gate the panel default are marked.

## 1. wlanpi-core

**1.1 Reserve `did=wlanpi-menud` + root helper — approve? (blocks first release)**
- The plan needs core to reserve a service identity for the menu daemon, exactly as it already reserves `wlanpi-webui`, plus a `getjwt wlanpi-menud` path through a root helper (`/usr/libexec/wlanpi-menud/get-core-token`, sudoers-allowed for the `wlanpi` user only).
- Phase 0 verification (core `df7ac8a`): only `did=wlanpi-webui` is reserved (`auth.py:23`, `auth_api.py:313-319`); `sudo getjwt wlanpi-menud` already mints a token via root HMAC, but without a reservation core logs it as an unknown device and it can collide with a user-chosen id.
- Owner: core maintainers. Recommendation: approve — it's the same pattern, already proven in the WebUI.

**1.2 Owners and timing for `POST /system/mode/switch` (gates panel default)**
- Needed for panel parity (Classic/Hotspot/Bluetooth switching). core-worker-api.md Stream A; not built yet. Verified absent from core `main` (`df7ac8a`, OpenAPI 2.4.13) on Nov 8.
- Owner: core maintainers to assign.

**1.3 Owners and timing for `GET /utils/cloud-test/{vendor}` (gates panel default)**
- Needed for panel parity (Speedtest/Libre/Cloudflare test items). Stream C; verified absent from core `main` on Nov 8. **Filed as [wlanpi-core#481](https://github.com/WLAN-Pi/wlanpi-core/issues/481) (Nov 8); open point there: GET vs POST.**
- Owner: core maintainers to assign.

**1.4 Owners and timing for the capture-session API (Stream D) (gates panel default)**
- Scanner CSV and pcap file actions must move to it. Designed in wifi-capture.md; verified not implemented in core `main` on Nov 8. Note: core's own `docs/api-gap-matrix.csv` capture rows (59-62, 83-86) are stale vs the wifi-capture.md session design — worth reconciling when the issue is triaged.
- Owner: core maintainers to assign.

**1.5 Token introspection endpoint (later — remote JWT clients only)**
- For Phase 4: an endpoint accepting the `wlanpi-menud` service token and returning `valid`, `did`, `exp`, `jti`. Not needed for the first release.

**1.6 Event stream `WS /api/v1/events` — yes or no? (policy decision)**
- Would let the menu service drop its polling loop. It forwards from sources that already push (rtnetlink, inotify, systemd D-Bus, udev), so core keeps no session or job state — but it is a new responsibility and contradicts core-worker-api.md §3 ("the UI layer polls REST"). If declined, the service keeps one polling loop and publishes on change; surfaces are unaffected either way.
- Owner: core maintainers. Not needed until after the first release.

**1.7 `scope` claim in core tokens (before any limited-trust remote tokens)**
- Today roles in the menu are not a security boundary: any core JWT is full-power. Remote tokens may only be issued to people trusted with admin rights until core adds and enforces scopes.
- Owner: core maintainers. Timing: before Phase 4 issues tokens to anything less trusted.

## 2. Team — panel parity bar

**2.1 Which parity-list items are required before the panel default flips?**
- The tracker is `wlanpi-menud/docs/parity-list.md`: all 65 FPMS menu items, each with the core endpoint it needs and that endpoint's status — **40 work today, 13 need a rebind** (endpoint moved or response shape changed), **11 are missing in core** (mostly the mode-switch, cloud-test and capture-file groups), 1 drop-candidate (Rotate Display); System updates already dropped.
- Also found: the PoC's Bluetooth pair action fails with a 400 (`bt-timedpair` is not in core's service allowlist, `system_service.py:565-568`) — needs a decision to add it or rebind.
- Ask: mark each row **required / deferred / dropped**. The panel stays opt-in until every required item works on every supported model.
- Owner: team decision.

## 3. Hardware / image owners — two assumptions to confirm

**3.1 Is 512 MiB really the smallest supported board?**
- `docs/resource-budget.md` budgets the service + TUI against a 512 MiB BCM2837-class board. Confirm whether any such board is still supported for Trixie, or whether 1 GiB is the actual floor across M4/M4+/R4/R5/Pro/Go. The budget tightens or loosens accordingly.

**3.2 Is `/etc` per-slot across A/B upgrades?**
- `docs/upgrade-rollback.md` assumes `/etc` is replaced by the new slot's image (only `/home` persists, `/var` does not). Confirm (a) whether `/etc` or an overlay/copy list is carried across slots, and (b) whether the A/B tool replays third-party `.deb`s installed in the old slot. This decides where menu overrides live and how plugins survive upgrades.

## 4. WebUI

**4.1 Browser auth: nginx `auth_request` or one-time tickets?**
- Recommended in the plan: `auth_request` — nginx validates the session cookie before upgrading `/ui/ws`, then sets `X-WebUI-User` / `X-WebUI-Session` on the proxy socket; logout closes that session's menu sessions. Fallback: single-use tickets minted by the WebUI backend over `local.sock`.
- Owner: WebUI maintainers. Needed for Phase 2 (first release if time allows).

## 5. Mobile app (later)

**5.1 How does the app get its JWT, and what lifetime should app tokens have?**
- Needed before Phase 4. Tied to 1.7: until core enforces scopes, app tokens are full-device tokens.

## 6. Panel look and feel

**6.1 Which screen redesign direction — Quiet, Glance tiles, or Pixel crisp?**
- All three work with the same view blocks, so this does not affect the protocol. Best decided after seeing them on real hardware. Canvas: [WLAN Pi Panel Redesign](https://claude.ai/artifact/ArCo4fDqzfxCEAtmQrWMrm).
- Owner: team. Timing: before the panel opt-in (Phase 3).

**6.2 Renderer: stay on the improved PIL path (option A), or spike the kernel driver + LVGL (option C)?**
- The plan schedules a spike: ST7735 as a DRM device, buttons via `gpio-key` evdev, frame-rate/CPU/RAM vs option A, and the status of SSD1351 DRM support for the Pro (June 2026 patch series, merge unconfirmed).
- Owner: team after the spike. Timing: after Phase 3.

---

### What's already decided
- Names: `wlanpi-menud` (service), `wlanpi-menu-tui` (TUI). ✅
- System updates menu items: dropped. ✅
- JSON as the only manifest format. ✅
