// Browser-driven e2e for the redesigned /menu page.
//
// Real headless Chromium (Playwright) against the real page through nginx,
// Flask and menud, started by run_verification.py. This is the layer that
// catches what unit tests on a simulated DOM cannot (layout, focus, real
// collections, real sockets).
//
// Each assertion prints one line:  RESULT {"id","name","ok","detail"}
// Everything else printed is the transcript.
//
// Assertion ids follow wlanpi-menud docs/webui-menu-redesign.md section 5.5.
import { createRequire } from "node:module";

const require = createRequire(process.env.E2E_MODULES || "/opt/e2e/");
const { chromium } = require("playwright");
const axeModule = require("@axe-core/playwright");
const AxeBuilder = axeModule.AxeBuilder || axeModule.default;

const cfg = JSON.parse(process.env.E2E_CONFIG);
const BASE = cfg.base;
const T0 = Date.now();

function say(msg) {
  console.log(`[${((Date.now() - T0) / 1000).toFixed(1).padStart(6)}s] ${msg}`);
}

let failures = 0;
function check(id, name, ok, detail = "") {
  if (!ok) failures += 1;
  const text = (typeof detail === "string" ? detail : JSON.stringify(detail)).replace(/\s+/g, " ");
  say(`${ok ? "PASS" : "FAIL"} ${id} ${name}${text ? "  [" + text + "]" : ""}`);
  console.log("RESULT " + JSON.stringify({ id, name, ok: Boolean(ok), detail: text.slice(0, 400) }));
}

/** Run one numbered section; an exception fails that section, not the run. */
async function section(id, title, fn) {
  say(`--- ${id}: ${title}`);
  try {
    await fn();
  } catch (err) {
    check(id, `${title}: completed without an exception`, false, String(err && err.stack ? err.stack.split("\n").slice(0, 3).join(" | ") : err));
  }
}

async function ctl(method, path, body) {
  const res = await fetch(cfg.control + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const out = await res.json();
  if (!res.ok) throw new Error(`control ${method} ${path}: ${JSON.stringify(out)}`);
  return out;
}

/** Invoke lines in menud's audit log for a node, by this run's user. */
async function auditInvokes(node) {
  const { invokes } = await ctl("GET", "/invokes");
  return invokes.filter((i) => i.user === cfg.user && (!node || i.node === node)).length;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Optional: E2E_SHOTS=<writable dir> saves a screenshot at each named point.
const SHOTS = process.env.E2E_SHOTS || "";
async function shot(p, name) {
  if (!SHOTS) return;
  try {
    await p.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false });
  } catch (err) {
    say(`screenshot ${name} failed: ${err.message}`);
  }
}

async function until(fn, { timeout = 10000, every = 100, what = "condition" } = {}) {
  const end = Date.now() + timeout;
  let last;
  for (;;) {
    last = await fn();
    if (last) return last;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await sleep(every);
  }
}

// ---------------------------------------------------------------- browser

const browser = await chromium.launch({ args: ["--no-sandbox"] });
say(`Chromium ${browser.version()} (headless), Playwright driving ${BASE}/menu`);

async function newContext(viewport) {
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport });
  await context.addCookies([{
    name: cfg.cookie.name, value: cfg.cookie.value, domain: "localhost", path: "/", secure: true, httpOnly: true, sameSite: "Lax",
  }]);
  return context;
}

/** A page whose WebSocket traffic is recorded, frame by frame. */
async function newPage(context) {
  const page = await context.newPage();
  const sockets = [];
  // A reload or a new URL is a new document; its predecessor's sockets are
  // gone with it, whether or not a close event was reported for them.
  let doc = 0;
  page.on("framenavigated", (frame) => {
    if (frame !== page.mainFrame()) return;
    const url = new URL(frame.url());
    const key = url.origin + url.pathname + url.search;
    if (page.__docKey !== undefined && page.__docKey === key && !page.__reloading) return; // a hash change
    page.__docKey = key;
    page.__reloading = false;
    doc += 1;
  });
  page.on("websocket", (ws) => {
    const rec = { url: ws.url(), sent: [], received: [], closed: false, doc };
    sockets.push(rec);
    ws.on("framesent", (f) => { try { rec.sent.push(JSON.parse(f.payload)); } catch { /* not JSON */ } });
    ws.on("framereceived", (f) => { try { rec.received.push(JSON.parse(f.payload)); } catch { /* not JSON */ } });
    ws.on("close", () => { rec.closed = true; });
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error" && !/WebSocket|Failed to load resource/.test(m.text())) errors.push(m.text()); });
  const helloOf = (rec) => rec.sent.find((f) => f.type === "hello") || {};
  return {
    page, sockets, errors,
    /** Sockets of the current document that are still open. */
    open: () => sockets.filter((s) => !s.closed && s.doc === doc),
    reload: async () => { page.__reloading = true; await page.reload(); },
    // The menu connection says hello as the page's own client; the pane's is the only one with keys.
    menuSockets: () => sockets.filter((s) => helloOf(s).mode === "rich"),
    paneSockets: () => sockets.filter((s) => helloOf(s).mode === "thin"),
    sent: (type, pick = () => true) => sockets.flatMap((s) => s.sent).filter((f) => f.type === type && pick(f)),
    invokes: (node) => sockets.flatMap((s) => s.sent).filter((f) => f.type === "invoke" && (!node || f.node === node)),
  };
}

const hash = (page) => page.evaluate(() => location.hash);
const title = (page) => page.locator("#menu-page-title").innerText();
const resultRows = (page) => page.locator("#menu-result tbody tr:not(.table-detail-row)").count();

async function waitForPage(page, id) {
  await page.waitForFunction((want) => {
    const el = document.querySelector("#menu-page");
    return el && el.getAttribute("data-node") === want && el.getAttribute("data-page") !== "loading";
  }, id);
}

async function waitForResult(page) {
  await page.waitForFunction(() => {
    const el = document.querySelector("#menu-result");
    return el && el.getAttribute("data-status") === "result" && el.getAttribute("aria-busy") === "false";
  });
}

async function clickRow(page, id) {
  await page.locator(`#menu-page a[data-node="${id}"]`).click();
  await waitForPage(page, id);
}

async function goHome(page) {
  await page.locator("#menu-statusbar").click();
  await waitForPage(page, "");
}

// ================================================================== desktop

const desktop = await newContext({ width: 1280, height: 900 });
const d = await newPage(desktop);
const page = d.page;

await section("1", "/menu loads as one surface with device info", async () => {
  await page.goto(`${BASE}/menu`);
  await page.waitForSelector("#menu-page .menu-card");
  const text = await page.locator("#menu-panel").innerText();
  await shot(page, "01-home-desktop");
  check("1a", "no mode control on /menu", (await page.locator("#menu-panel select, #menu-mode-select, #menu-panel input[type=checkbox]").count()) === 0);
  check("1b", "no control or text names a protocol mode", !/\b(rich|thin|mirror|session)\b/i.test(text), text.match(/\b(rich|thin|mirror|session)\b/i)?.[0] || "");
  const bar = await page.locator("#menu-statusbar").innerText();
  check("1c", "status bar shows hostname and IP from the stub core", bar.includes("wlanpi-test") && bar.includes("192.0.2.10"), bar.replace(/\s+/g, " "));
  const card = await page.locator(".menu-device").innerText();
  check("1d", "Home device card shows hostname, IP, model and mode", /wlanpi-test/.test(card) && /192\.0\.2\.10/.test(card) && /Model/.test(card) && /Classic/.test(card), card.replace(/\s+/g, " ").slice(0, 160));
  check("1e", "one connection, and it is the private menu", d.sockets.length === 1 && d.menuSockets().length === 1 && d.menuSockets()[0].sent[0].session === "new",
    `sockets=${d.sockets.length}`);
  check("1f", "Home runs nothing", d.invokes().length === 0 && (await auditInvokes()) === 0);
  check("1g", "the menu connection subscribes to device.summary, alerts, complications and jobs",
    JSON.stringify(d.sent("subscribe")[0]?.topics) === JSON.stringify(["device.summary", "alerts", "complications", "jobs"]));
});

await section("2", "drill to Network > Interfaces: grouped table on arrival", async () => {
  await page.locator('.menu-card[data-node="network"]').click();
  await waitForPage(page, "network");
  check("2a", "branch page lists children; opening it sends no invoke", (await title(page)) === "Network" && d.invokes().length === 0,
    `rows=${await page.locator("#menu-page .menu-row").count()}`);
  await shot(page, "02-branch-desktop");
  await clickRow(page, "network.interfaces");
  await waitForResult(page);
  await shot(page, "03-action-readonly");
  const h = await hash(page);
  check("2b", "the URL hash is the node id", h === "#network.interfaces", h);
  const table = await page.locator("#menu-result table").innerText();
  check("2c", "grouped interfaces table appears with no further click", /Ethernet/.test(table) && /WLAN/.test(table) && /eth0/.test(table) && /wlan0/.test(table),
    table.replace(/\s+/g, " ").slice(0, 160));
  check("2d", "read-only view ran on arrival: exactly one invoke frame, one audit line",
    d.invokes("network.interfaces").length === 1 && (await auditInvokes("network.interfaces")) === 1,
    `frames=${d.invokes("network.interfaces").length} audit=${await auditInvokes("network.interfaces")}`);
  const inv = d.invokes("network.interfaces")[0];
  check("2e", "the invoke carries no params and no confirmed", inv && !("params" in inv) && !("confirmed" in inv), JSON.stringify(inv));
  check("2f", "the page holds one result region, with a timestamp and Refresh",
    (await page.locator("#menu-page #menu-result").count()) === 1 && /^Updated \d\d:\d\d:\d\d$/.test(await page.locator(".menu-updated").innerText()) &&
    (await page.locator('[data-menu-action="refresh"]').isEnabled()));
  check("2g", "one table: all rows share one background", await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll("#menu-result tbody tr"));
    const bgs = new Set(rows.map((r) => getComputedStyle(r).backgroundColor));
    return document.querySelectorAll("#menu-result table").length === 1 && rows.length >= 2 && bgs.size === 1;
  }));
  check("2h", "focus moved to the page title on navigation", await page.evaluate(() => document.activeElement && document.activeElement.id === "menu-page-title"));
  check("2i", "the sidebar marks the current page", (await page.locator('#menu-sidebar [aria-current="page"]').getAttribute("href")) === "#network.interfaces");
  check("2j", "the menu connection sent no nav and no input frames", d.sent("nav").length === 0 && d.sent("input").length === 0);
});

await section("3", "reload restores the page and its result; Back returns to Network", async () => {
  await d.reload();
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  check("3a", "reload on the URL restores the page and its result", (await hash(page)) === "#network.interfaces" && (await resultRows(page)) >= 2,
    `rows=${await resultRows(page)}`);
  await page.goBack();
  await waitForPage(page, "network");
  check("3b", "Back returns to Network without leaving /menu", (await title(page)) === "Network" && new URL(page.url()).pathname === "/menu", page.url());
  await page.goForward();
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  check("3c", "Forward returns to the result page", (await resultRows(page)) >= 2);
});

await section("4", "a mutating node never fires without Run", async () => {
  const before = await ctl("GET", "/core");
  await goHome(page);
  await page.locator('.menu-card[data-node="system"]').click();
  await waitForPage(page, "system");
  const cue = await page.locator('.menu-row[data-node="system.reboot"] .menu-row-mutates').innerText();
  check("4a", "the row is marked 'changes device' (icon plus text)", /changes device/.test(cue) &&
    (await page.locator('.menu-row[data-node="system.reboot"] .menu-row-mutates [uk-icon]').count()) === 1, cue);
  await clickRow(page, "system.reboot");
  await sleep(700);
  const zero = async () => ({
    frames: d.invokes("system.reboot").length, audit: await auditInvokes("system.reboot"),
    reboots: (await ctl("GET", "/core")).reboots - before.reboots,
  });
  let z = await zero();
  check("4b", "opening a mutating node sends zero invoke frames (browser frames and menud audit log)", z.frames === 0 && z.audit === 0 && z.reboots === 0, z);
  check("4c", "the page says it changes the device and waits for its button",
    /This changes the device\./.test(await page.locator(".menu-mutates").innerText()) && (await page.locator('[data-menu-action="run"]').innerText()) === "Reboot");

  await d.reload();
  await waitForPage(page, "system.reboot");
  await sleep(700);
  z = await zero();
  check("4d", "reload on a mutating page does not run it", z.frames === 0 && z.audit === 0 && z.reboots === 0, z);
  await page.goBack();
  await waitForPage(page, "system");
  await page.goForward();
  await waitForPage(page, "system.reboot");
  await page.goBack();
  await page.goForward();
  await waitForPage(page, "system.reboot");
  await sleep(700);
  z = await zero();
  check("4e", "Back and Forward over a mutating page do not run it", z.frames === 0 && z.audit === 0 && z.reboots === 0, z);

  await shot(page, "04-mutating-idle");
  await page.locator('[data-menu-action="run"]').click();
  await page.waitForSelector('[data-menu-action="confirm-yes"]');
  await shot(page, "05-mutating-confirm");
  z = await zero();
  check("4f", "pressing Run only opens the inline confirmation: still zero", z.frames === 0 && z.audit === 0 && z.reboots === 0, z);
  check("4g", "confirm row shows the confirm text and focus is on Cancel",
    /Reboot the WLAN Pi now\?/.test(await page.locator("#menu-confirm-text").innerText()) &&
    (await page.evaluate(() => document.activeElement.getAttribute("data-menu-action"))) === "confirm-no");
  await page.locator('[data-menu-action="confirm-no"]').click();
  await page.waitForSelector('[data-menu-action="run"]');
  z = await zero();
  check("4h", "Cancel in the confirm row sends nothing", z.frames === 0 && z.audit === 0 && z.reboots === 0, z);

  await page.locator('[data-menu-action="run"]').click();
  await page.locator('[data-menu-action="confirm-yes"]').click();
  await waitForResult(page);
  z = await zero();
  const inv = d.invokes("system.reboot")[0];
  check("4i", "Run then confirm sends exactly one invoke, with confirmed: true", z.frames === 1 && z.audit === 1 && z.reboots === 1 && inv.confirmed === true,
    { ...z, confirmed: inv && inv.confirmed });
  check("4j", "no server-side confirm prompt was needed", d.sockets.flatMap((s) => s.received).filter((f) => f.type === "prompt").length === 0);
  check("4k", "the result is on the action's own page", /Rebooting/.test(await page.locator("#menu-result").innerText()), (await page.locator("#menu-result").innerText()).replace(/\s+/g, " "));

  await goHome(page);
  await page.goBack();
  await waitForPage(page, "system.reboot");
  await sleep(500);
  z = await zero();
  check("4l", "returning to the page shows the last result and does not run it again",
    z.frames === 1 && z.audit === 1 && z.reboots === 1 && /Rebooting/.test(await page.locator("#menu-result").innerText()), z);

  // A mutation without a confirm: two clicks (open, Run), never one.
  const plain = await page.evaluate(() => {
    const s = window.wlanpiMenu.getState();
    const n = Object.values(s.menu.byId).find((x) => x.kind === "action" && x.mutates === true && !x.confirm && !(x.params && x.params.length));
    return n ? { id: n.id, label: n.label } : null;
  });
  if (plain) {
    await page.evaluate((id) => { location.hash = "#" + id; }, plain.id);
    await waitForPage(page, plain.id);
    await sleep(600);
    const f0 = d.invokes(plain.id).length;
    const a0 = await auditInvokes(plain.id);
    await page.locator('[data-menu-action="run"]').click();
    await page.waitForFunction(() => ["result", "error"].includes(document.querySelector("#menu-result").getAttribute("data-status")));
    check("4m", `a mutation without a confirm (${plain.id}) waits for its button, then runs once`,
      f0 === 0 && a0 === 0 && d.invokes(plain.id).length === 1 && (await auditInvokes(plain.id)) === 1,
      `before=${f0}/${a0} after=${d.invokes(plain.id).length}/${await auditInvokes(plain.id)}`);
  } else {
    check("4m", "a mutation without a confirm waits for its button, then runs once", false, "no such node in the menu");
  }
});

await section("5", "an action with parameters: form, Run, table, Load more", async () => {
  await ctl("POST", "/core", { scan_networks: 213, scan_candidates: null });
  await goHome(page);
  await page.locator('.menu-card[data-node="apps"]').click();
  await waitForPage(page, "apps");
  await clickRow(page, "apps.scanner");
  check("5a", "a row with parameters ends in an ellipsis", /…$/.test(await page.locator('.menu-row[data-node="apps.scanner.scan"] .menu-row-name').innerText()));
  await clickRow(page, "apps.scanner.scan");
  await sleep(500);
  check("5b", "the form is shown and nothing runs on arrival",
    (await page.locator("#menu-action-form select[data-param]").count()) === 1 && d.invokes("apps.scanner.scan").length === 0 && (await auditInvokes("apps.scanner.scan")) === 0);
  const options = await page.locator("#menu-action-form select[data-param] option").allInnerTexts();
  check("5c", "the adapter control offers the device's adapters", options.includes("wlan1"), options.join(","));
  await page.locator("#menu-action-form select[data-param]").selectOption("wlan1");
  await page.locator('[data-menu-action="run"]').click();
  await waitForResult(page);
  const inv = d.invokes("apps.scanner.scan")[0];
  check("5d", "Run sends one invoke with the chosen parameter", d.invokes("apps.scanner.scan").length === 1 && inv.params && inv.params.adapter.iface === "wlan1", JSON.stringify(inv));
  const first = await resultRows(page);
  const count1 = await page.locator(".menu-paging-count").innerText();
  check("5e", "the first page of the table is shown with the total", first === 20 && count1 === "Showing 20 of 213", `rows=${first} "${count1}"`);
  await page.locator('[data-menu-action="load-more"]').click();
  await page.waitForFunction(() => document.querySelectorAll("#menu-result tbody tr").length === 40);
  await shot(page, "06-form-result-paging");
  const count2 = await page.locator(".menu-paging-count").innerText();
  const pages = d.sent("page");
  check("5f", "Load more sends one page frame and appends rows to the same table; the count changes",
    pages.length === 1 && typeof pages[0].cursor === "string" && (await page.locator("#menu-result table").count()) === 1 && count2 === "Showing 40 of 213",
    `pages=${pages.length} "${count2}"`);
  check("5g", "paging did not re-run the action", d.invokes("apps.scanner.scan").length === 1 && (await auditInvokes("apps.scanner.scan")) === 1);
});

await section("6", "a job that needs a selection opens the dialog", async () => {
  await ctl("POST", "/core", {
    scan_networks: 3,
    scan_candidates: [{ iface: "wlan0", namespace: "root", label: "wlan0" }, { iface: "wlan1", namespace: "root", label: "wlan1" }],
  });
  await page.goBack();
  await waitForPage(page, "apps.scanner");
  await clickRow(page, "apps.scanner.scan_nohidden");
  await page.locator('[data-menu-action="run"]').click();
  const dialog = page.locator('#menu-dialog [role="dialog"]');
  await dialog.waitFor();
  await shot(page, "07-prompt-dialog");
  const heading = await page.locator("#menu-dialog-title").innerText();
  check("6a", "the dialog names the node that asked", /Scanner › Scan \(no hidden\)/.test(heading), heading);
  check("6b", "it is a modal dialog with a radio per candidate and focus inside it",
    (await dialog.getAttribute("aria-modal")) === "true" && (await dialog.locator('input[type="radio"]').count()) === 2 &&
    (await page.evaluate(() => document.querySelector('#menu-dialog [role="dialog"]').contains(document.activeElement))));
  check("6c", "it shows the time left before the prompt expires", /^Expires in \d:\d\d$/.test(await dialog.locator(".menu-dialog-expiry").innerText()));
  // Tab cannot leave the dialog.
  for (let i = 0; i < 8; i++) await page.keyboard.press("Tab");
  check("6d", "Tab stays inside the dialog", await page.evaluate(() => document.querySelector('#menu-dialog [role="dialog"]').contains(document.activeElement)));
  await dialog.locator('input[type="radio"]').nth(1).check();
  await dialog.locator("[data-prompt-ok]").click();
  await waitForResult(page);
  const answers = d.sent("answer");
  const kinds = answers[0] ? ["value", "values", "confirmed", "cancel"].filter((k) => k in answers[0]) : [];
  check("6e", "choosing an option answers with exactly one of value/values/confirmed/cancel", answers.length === 1 && kinds.join() === "value" && answers[0].value.iface === "wlan1",
    JSON.stringify(answers[0]));
  check("6f", "the job completes and its result is on the action's page", (await page.locator('#menu-dialog [role="dialog"]').count()) === 0 && (await resultRows(page)) === 3,
    `rows=${await resultRows(page)}`);

  // Esc cancels.
  await page.locator('[data-menu-action="run"]').click();
  await page.locator('#menu-dialog [role="dialog"]').waitFor();
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelector("#menu-result").getAttribute("data-status") === "cancelled");
  const cancel = d.sent("answer").pop();
  check("6g", "Esc answers cancel: true and the action page shows Cancelled", cancel.cancel === true && /^Cancelled at \d\d:\d\d:\d\d\./.test(await page.locator("#menu-result").innerText()),
    JSON.stringify(cancel));
  await ctl("POST", "/core", { scan_candidates: null });
});

await section("7", "job progress, Activity, and leaving a running job", async () => {
  await ctl("POST", "/core", { speedtest_delay: 4 });
  await goHome(page);
  await page.locator('.menu-card[data-node="utils"]').click();
  await waitForPage(page, "utils");
  await clickRow(page, "utils.speedtest");
  await clickRow(page, "utils.speedtest.run");
  await page.waitForSelector("#menu-result .menu-running progress");
  check("7a", "running state: progress bar, message, Cancel on a cancellable node, aria-busy",
    (await page.locator("#menu-result").getAttribute("aria-busy")) === "true" && (await page.locator('#menu-result [data-menu-action="cancel-job"]').count()) === 1 &&
    /Running/.test(await page.locator("#menu-result .menu-running-text").innerText()));
  check("7b", "Refresh is disabled while the job is live", await page.locator('[data-menu-action="refresh"]').isDisabled());
  const chip = page.locator(".menu-activity-chip");
  await chip.waitFor();
  check("7c", "the Activity chip appears while something runs", /1 running/.test(await chip.innerText()), await chip.innerText());
  await chip.click();
  await shot(page, "08-running-activity");
  const item = await page.locator(".menu-activity-item").first().innerText();
  check("7d", "the popover lists the job by its menu path", /Speedtest › Run Test/.test(item) && (await chip.getAttribute("aria-expanded")) === "true", item.replace(/\s+/g, " "));
  await page.keyboard.press("Escape");

  await goHome(page);
  check("7e", "leaving does not cancel the job", d.sent("cancel").length === 0);
  const notice = page.locator(".menu-notice");
  await notice.waitFor({ timeout: 15000 });
  await shot(page, "09-notice-view-result");
  check("7f", "when it finishes elsewhere a notice offers View result", /Run Test finished\./.test(await notice.innerText()) &&
    (await notice.locator("a").getAttribute("href")) === "#utils.speedtest.run", (await notice.innerText()).replace(/\s+/g, " "));
  check("7g", "the chip goes when nothing is running", (await page.locator(".menu-activity-chip").count()) === 0);
  await notice.locator("a").click();
  await waitForPage(page, "utils.speedtest.run");
  await page.waitForFunction(() => /500 Mbps/.test(document.querySelector("#menu-result").innerText));
  check("7h", "the result was stored against its node", true);
  await ctl("POST", "/core", { speedtest_delay: 0 });
});

await section("12", "100 consecutive Refresh presses keep the result region the same size", async () => {
  await page.evaluate(() => { location.hash = "#network.interfaces"; });
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  const count = () => page.evaluate(() => document.querySelector("#menu-result").querySelectorAll("*").length);
  const before = await count();
  const sentBefore = d.invokes("network.interfaces").length;
  for (let i = 0; i < 100; i++) {
    await page.locator('[data-menu-action="refresh"]:not([disabled])').click();
    await until(() => d.invokes("network.interfaces").length === sentBefore + i + 1, { every: 10, what: `invoke ${i + 1}` });
    await page.waitForFunction(() => {
      const s = window.wlanpiMenu.getState();
      const r = s.results["network.interfaces"];
      return r && r.status === "result" && !Object.values(s.pending).some((p) => p.kind === "invoke");
    });
  }
  await waitForResult(page);
  const after = await count();
  const st = await page.evaluate(() => {
    const s = window.wlanpiMenu.getState();
    return { results: Object.keys(s.results).length, jobs: Object.keys(s.jobs).length, pending: Object.keys(s.pending).length };
  });
  check("12a", "result region node count unchanged after 100 refreshes", after === before && d.invokes("network.interfaces").length - sentBefore === 100,
    `before=${before} after=${after} invokes=${d.invokes("network.interfaces").length - sentBefore}`);
  check("12b", "client state stays bounded", st.jobs === 0 && st.pending <= 4 && st.results <= 20, st);
});

await section("7p", "Front panel pane: a second connection beside the menu", async () => {
  const toggle = page.locator("#menu-panel-toggle");
  check("7p-a", "the pane is off by default", (await toggle.getAttribute("aria-pressed")) === "false" && !(await page.locator("#menu-front-panel").isVisible()) && d.open().length === 1,
    `open sockets=${d.open().length}`);
  await toggle.click();
  await page.waitForSelector("#menu-front-panel .menu-fp-screen .view-block");
  await until(() => d.paneSockets().length === 1, { what: "pane hello" });
  const hello = d.paneSockets()[0].sent[0];
  check("7p-b", "toggling opens a second socket: thin attach to the default screen", d.open().length === 2 && hello.mode === "thin" && hello.session === "default",
    `open=${d.open().length} mode=${hello.mode} attach=${hello.session}`);
  check("7p-c", "the pane docks beside the menu at 1280 px and the menu stays usable", await page.evaluate(() => {
    const pane = document.querySelector("#menu-front-panel").getBoundingClientRect();
    const content = document.querySelector("#menu-page").getBoundingClientRect();
    return getComputedStyle(document.querySelector("#menu-front-panel")).position === "sticky" && pane.left >= content.right - 1 && content.width > 300;
  }));
  check("7p-d", "first open explains that the screen is shared", /Anyone looking at the device sees the same thing/.test(await page.locator(".menu-fp-notice").innerText()));

  // A TUI-style client on the default session drives; the pane follows and says who.
  const tuiView = (await ctl("POST", "/tui/input", { key: "down" })).view;
  await page.waitForFunction(() => /^Driven from wlanpi-menu-tui/.test(document.querySelector('[data-region="driven"]').textContent));
  const driven = await page.locator('[data-region="driven"]').innerText();
  check("7p-e", "TUI input updates the pane with a 'Driven from' line", /^Driven from wlanpi-menu-tui \(.+\)$/.test(driven) && tuiView && tuiView.type === "view", driven);
  const screenBefore = await page.locator(".menu-fp-screen").innerText();

  // The pane drives; the TUI client receives the view and sees who drove it.
  const menuHash = await hash(page);
  const menuTitle = await title(page);
  const key = (await page.locator('[data-fp-key="down"]').isEnabled()) ? "down" : "up";
  await page.locator(`[data-fp-key="${key}"]`).click();
  await page.waitForFunction(() => document.querySelector('[data-region="driven"]').textContent === "You are driving");
  const seen = (await ctl("GET", "/tui/view?wait=1.0")).view;
  check("7p-f", "a key press in the pane changes the view the TUI client receives, driven by this surface",
    seen && seen.driven_by && seen.driven_by.self === false && seen.driven_by.surface === "wlanpi-webui-front-panel" && seen.driven_by.principal.name === cfg.user,
    JSON.stringify(seen && seen.driven_by));
  await shot(page, "10-front-panel-docked");
  const screenAfter = await page.locator(".menu-fp-screen").innerText();
  check("7p-g", "the pane's screen follows the shared cursor", screenAfter !== screenBefore || /›/.test(screenAfter), screenAfter.replace(/\s+/g, " ").slice(0, 100));
  check("7p-h", "the private menu is unaffected by the pane", (await hash(page)) === menuHash && (await title(page)) === menuTitle &&
    d.menuSockets().every((s) => s.sent.every((f) => f.type !== "input")));
  check("7p-i", "keys take their captions from the view's hints", await page.evaluate(() => {
    const cap = document.querySelector('[data-fp-key="center"] .menu-fp-key-caption');
    return Boolean(cap && cap.textContent.length > 0);
  }));

  // Keyboard: captured only while focus is inside the pane.
  const inputs = () => d.paneSockets().flatMap((s) => s.sent).filter((f) => f.type === "input").length;
  await page.locator("#menu-page-title").focus();
  let n = inputs();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowUp");
  await sleep(300);
  check("7p-j", "arrow keys outside the pane are the page's own", inputs() === n);
  await page.locator(".menu-fp-screen").focus();
  n = inputs();
  await page.keyboard.press("ArrowDown");
  await until(() => inputs() === n + 1, { what: "input from the keyboard" });
  check("7p-k", "arrow keys with focus in the pane send input", inputs() === n + 1);

  // Rich and pane at the same time.
  const rows0 = d.invokes("network.interfaces").length;
  await page.locator('[data-menu-action="refresh"]:not([disabled])').click();
  await ctl("POST", "/tui/input", { key: "down" });
  await page.waitForFunction(() => /^Driven from wlanpi-menu-tui/.test(document.querySelector('[data-region="driven"]').textContent));
  await waitForResult(page);
  check("7p-l", "the menu and the pane work at the same time, each on its own socket",
    d.invokes("network.interfaces").length === rows0 + 1 && d.open().length === 2 && (await resultRows(page)) >= 2);

  // Close detaches only the pane.
  await page.locator('[data-fp-action="close"]').click();
  await until(() => d.open().length === 1, { what: "pane socket closed" });
  check("7p-m", "closing the pane detaches only its connection", d.open().length === 1 && d.menuSockets().some((s) => !s.closed) &&
    (await page.evaluate(() => window.wlanpiMenu.connectionState().phase)) === "live" && (await ctl("GET", `/menud/attachments?user=${cfg.user}`)).count === 1);
  await page.locator('[data-menu-action="refresh"]:not([disabled])').click();
  await waitForResult(page);
  check("7p-n", "the menu still works after the pane closed", d.invokes("network.interfaces").length === rows0 + 2);
  await toggle.click();
  await page.waitForSelector("#menu-front-panel .menu-fp-screen .view-block");
  check("7p-o", "the choice is remembered for this browser", (await page.evaluate(() => localStorage.getItem("wlanpi-menu-front-panel"))) === "1");
});

await section("11", "accessibility: axe-core and a Tab-only walk", async () => {
  const scan = async (label) => {
    const out = [];
    for (const theme of ["light", "dark"]) {
      await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
      await sleep(150);
      await shot(page, `11-${label.replace(/[^a-z]+/gi, "-")}-${theme}`);
      const res = await new AxeBuilder({ page }).include("#menu-panel").analyze();
      const bad = res.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      const all = res.violations.map((v) => `${v.id}(${v.impact}: ${v.nodes.slice(0, 2).map((x) => x.target.join(" ")).join(" ; ")})`);
      say(`axe ${label} [${theme}]: ${res.passes.length} rules passed, violations: ${all.join(", ") || "none"}`);
      for (const v of bad) {
        out.push(`${theme}: ${v.id} x${v.nodes.length} ${v.nodes.slice(0, 2).map((x) => x.target.join(" ")).join(" ; ")}`);
      }
    }
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
    return out;
  };
  // The pane is open from the previous section.
  let bad = await scan("result page + open pane");
  check("11a", "axe: no serious or critical violations on a result page with the pane open, both themes", bad.length === 0, bad.join(" | "));
  await page.locator('[data-fp-action="close"]').click();
  await goHome(page);
  bad = await scan("Home");
  check("11b", "axe: no serious or critical violations on Home, both themes", bad.length === 0, bad.join(" | "));
  await page.evaluate(() => { location.hash = "#apps.scanner.scan"; });
  await waitForPage(page, "apps.scanner.scan");
  bad = await scan("form page");
  check("11c", "axe: no serious or critical violations on a form page, both themes", bad.length === 0, bad.join(" | "));
  await page.evaluate(() => { location.hash = "#system.reboot"; });
  await waitForPage(page, "system.reboot");
  await page.locator('[data-menu-action="run"]').click();
  await page.waitForSelector('[data-menu-action="confirm-yes"]');
  bad = await scan("mutating page, confirm open");
  check("11d", "axe: no serious or critical violations on a mutating page with the confirm row, both themes", bad.length === 0, bad.join(" | "));
  await page.locator('[data-menu-action="confirm-no"]').click();

  // Keyboard only: from Home to a result with Tab and Enter.
  await goHome(page);
  await page.locator("#menu-statusbar").focus();
  const tabTo = async (selector) => {
    for (let i = 0; i < 80; i++) {
      await page.keyboard.press("Tab");
      if (await page.evaluate((s) => Boolean(document.activeElement && document.activeElement.matches(s)), selector)) return true;
    }
    return false;
  };
  const before = d.invokes("network.interfaces").length;
  const a = await tabTo('#menu-page a[data-node="network"]');
  await page.keyboard.press("Enter");
  await waitForPage(page, "network");
  const b = await tabTo('#menu-page a[data-node="network.interfaces"]');
  await page.keyboard.press("Enter");
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  check("11e", "a Tab-only walk reaches a result from Home", a && b && d.invokes("network.interfaces").length === before + 1 && (await resultRows(page)) >= 2);
  await page.keyboard.press("/");
  check("11f", "'/' focuses the filter", (await page.evaluate(() => document.activeElement.id)) === "menu-filter");
  await page.keyboard.type("lldp");
  const hits = await page.locator("#menu-sidebar .menu-tree a").allInnerTexts();
  check("11g", "the filter narrows the tree by label", hits.length === 1 && /LLDP/.test(hits[0]), hits.join(","));
  await page.locator("#menu-filter").fill("");
  check("11h", "no script errors on the page", d.errors.length === 0, d.errors.slice(0, 3).join(" | "));
});

await section("8", "restarting menud mid-session: banner, countdown, resume without a reload", async () => {
  await page.evaluate(() => { location.hash = "#network.interfaces"; });
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  await page.locator("#menu-panel-toggle").click();
  await page.waitForSelector("#menu-front-panel .menu-fp-screen .view-block");
  await page.evaluate(() => { window.__e2eMarker = "same-document"; });
  const rowsBefore = await resultRows(page);
  const invBefore = d.invokes("network.interfaces").length;
  const auditBefore = await auditInvokes("network.interfaces");
  const epochBefore = d.menuSockets().flatMap((s) => s.received).filter((f) => f.type === "welcome").pop().epoch;

  // Record every banner text in the page itself, so a short-lived state is not missed.
  await page.evaluate(() => {
    window.__bannerTexts = [];
    const banner = document.querySelector("#menu-banner");
    const note = () => {
      const t = banner.hidden ? "" : banner.innerText.replace(/\s+/g, " ").trim();
      const log = window.__bannerTexts;
      if (t && log[log.length - 1] !== t) log.push(t);
    };
    window.__bannerObserver = new MutationObserver(note);
    window.__bannerObserver.observe(banner, { childList: true, subtree: true, characterData: true, attributes: true });
  });
  const restart = ctl("POST", "/menud/restart", { down_ms: 3500 });
  await page.waitForSelector("#menu-banner:not([hidden])", { timeout: 10000 });
  await shot(page, "12-connection-lost");
  const during = await page.evaluate(() => ({
    rows: document.querySelectorAll("#menu-result tbody tr").length,
    stale: document.querySelector("#menu-panel").classList.contains("is-stale"),
    refreshDisabled: document.querySelector('[data-menu-action="refresh"]').disabled,
    asOf: (document.querySelector(".menu-sb-asof") || {}).textContent || "",
    paneKeysOff: Array.from(document.querySelectorAll("[data-fp-key]")).every((b) => b.disabled),
    paneConn: (document.querySelector(".menu-fp-conn") || {}).innerText || "",
    marker: window.__e2eMarker,
  }));
  await restart;
  await page.waitForSelector("#menu-banner", { state: "hidden", timeout: 40000 });
  const texts = await page.evaluate(() => { window.__bannerObserver.disconnect(); return window.__bannerTexts; });
  say("banner transcript: " + texts.map((t) => `"${t}"`).join(" -> "));
  const countdowns = [...new Set(texts.map((t) => (/Retrying in (\d+) s/.exec(t) || [])[1]).filter(Boolean))];
  check("8a", "a banner appears when the service goes away", texts.length > 0 && /restarting|Connection lost/.test(texts[0]), texts[0]);
  check("8b", "the retry countdown is visible and counts down", countdowns.length >= 2, `values seen: ${countdowns.join(",")}`);
  check("8c", "the banner shows Reconnecting when a retry fires", texts.some((t) => /Reconnecting…/.test(t)));
  check("8d", "content stays on screen, marked stale, with controls disabled", during.rows === rowsBefore && during.stale && during.refreshDisabled && /as of \d\d:\d\d:\d\d/.test(during.asOf), during);
  check("8e", "the pane shows its own connection state and disables its keys", during.paneKeysOff && /Retrying|restarting|Connection lost/.test(during.paneConn), during.paneConn.replace(/\s+/g, " "));

  const welcomes = d.menuSockets().flatMap((s) => s.received).filter((f) => f.type === "welcome");
  const lastHello = d.menuSockets().filter((s) => s.sent.length).pop().sent[0];
  check("8f", "the reconnect sent hello.resume with the old epoch", lastHello.resume && lastHello.resume.epoch === epochBefore && typeof lastHello.resume.session_id === "string" &&
    Object.keys(lastHello.resume.seq || {}).length >= 1, JSON.stringify(lastHello.resume));
  const w = welcomes[welcomes.length - 1];
  check("8g", "the service came back with a new epoch and did not resume", w.epoch !== epochBefore && w.resumed === false, `resumed=${w.resumed}`);
  await waitForResult(page);
  await sleep(1200);
  check("8h", "the page recovered without a reload", (await page.evaluate(() => window.__e2eMarker)) === "same-document" &&
    (await page.evaluate(() => window.wlanpiMenu.connectionState().phase)) === "live" && !(await page.evaluate(() => document.querySelector("#menu-panel").classList.contains("is-stale"))));
  const invAfter = d.invokes("network.interfaces").length;
  check("8i", "the auto-run page refreshed exactly once", invAfter === invBefore + 1 && (await auditInvokes("network.interfaces")) === auditBefore + 1,
    `frames +${invAfter - invBefore}, audit +${(await auditInvokes("network.interfaces")) - auditBefore}`);
  check("8j", "the result is current again", (await resultRows(page)) === rowsBefore && (await page.locator('[data-menu-action="refresh"]').isEnabled()));
  await page.waitForFunction(() => /live/.test(document.querySelector(".menu-fp-indicator").textContent), null, { timeout: 40000 });
  check("8k", "the pane reconnected on its own", (await page.locator(".menu-fp-screen .view-block").count()) > 0 && d.open().length === 2, `open=${d.open().length}`);

  // The same restart on a Run page must not run it.
  await page.evaluate(() => { location.hash = "#system.reboot"; });
  await waitForPage(page, "system.reboot");
  const reboots = (await ctl("GET", "/core")).reboots;
  const rebootFrames = d.invokes("system.reboot").length;
  const restart2 = ctl("POST", "/menud/restart", { down_ms: 1500 });
  await page.waitForSelector("#menu-banner:not([hidden])", { timeout: 10000 });
  const runOff = await page.locator('[data-menu-action="run"]').isDisabled();
  await restart2;
  await page.waitForSelector("#menu-banner", { state: "hidden", timeout: 40000 });
  await sleep(1500);
  check("8l", "a Run page is never re-run by a reconnect", runOff && d.invokes("system.reboot").length === rebootFrames && (await ctl("GET", "/core")).reboots === reboots &&
    (await page.locator('[data-menu-action="run"]').isEnabled()), `reboots=${(await ctl("GET", "/core")).reboots - reboots}`);
});

await section("H", "leaving /menu inside the app and coming back", async () => {
  await page.evaluate(() => { location.hash = "#network.interfaces"; });
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  if (!(await page.locator("#menu-front-panel").isVisible())) await page.locator("#menu-panel-toggle").click();
  await page.waitForFunction(() => /live/.test((document.querySelector(".menu-fp-indicator") || {}).textContent || ""));
  await page.evaluate(() => { window.__e2eMarker = "same-document-2"; });
  const before = d.sockets.length;
  const sessionBefore = d.menuSockets().flatMap((s) => s.received).filter((f) => f.type === "welcome").pop().session_id;
  await page.locator('.uk-navbar-nav a[data-nav="apps"]').first().click();
  await page.waitForFunction(() => !document.getElementById("menu-panel"));
  await until(async () => (await ctl("GET", `/menud/attachments?user=${cfg.user}`)).count === 0, { what: "both connections closed" });
  check("Ha", "an in-app page change closes both connections", d.open().length === 0, `open=${d.open().length}`);
  await page.locator('.uk-navbar-nav a[data-nav="menu"]').first().click();
  await page.waitForSelector("#menu-page .menu-card, #menu-page #menu-result");
  await until(() => d.sockets.length > before && d.menuSockets().pop().received.some((f) => f.type === "welcome"), { what: "menu reconnected" });
  const sock = d.menuSockets().pop();
  const hello = sock.sent[0];
  const welcome = sock.received.find((f) => f.type === "welcome");
  check("Hb", "returning within the grace resumes the same private menu", (await page.evaluate(() => window.__e2eMarker)) === "same-document-2" &&
    hello.resume && hello.resume.session_id === sessionBefore && welcome.session_id === sessionBefore,
    `resumed=${welcome.resumed} same=${welcome.session_id === sessionBefore}`);
  await page.waitForFunction(() => /live/.test((document.querySelector(".menu-fp-indicator") || {}).textContent || ""));
  check("Hc", "the page works again and the pane reopens as it was left", (await page.locator(".menu-device").count()) === 1 && d.open().length === 2 &&
    (await ctl("GET", `/menud/attachments?user=${cfg.user}`)).count === 2);
});

// ================================================================== preview flag

await section("P", "/menu?panel=private binds the pane to its own screen", async () => {
  const p = await newPage(desktop);
  await p.page.goto(`${BASE}/menu?panel=private`);
  await p.page.waitForSelector("#menu-front-panel .menu-fp-screen .view-block");
  await until(() => p.paneSockets().length === 1, { what: "preview hello" });
  const hello = p.paneSockets()[0].sent[0];
  const welcome = p.paneSockets()[0].received.find((f) => f.type === "welcome");
  check("Pa", "the preview pane is a private thin attach", hello.mode === "thin" && hello.session === "new" && welcome.session === "new", `hello=${hello.session} welcome=${welcome.session}`);
  check("Pb", "it is titled Panel preview, without the driving line or the shared-screen notice",
    /^Panel preview/.test(await p.page.locator("#menu-fp-title").innerText()) && (await p.page.locator(".menu-fp-notice").count()) === 0 &&
    !(await p.page.locator('[data-region="driven"]').isVisible()));
  const before = await p.page.locator(".menu-fp-screen").innerText();
  await ctl("POST", "/tui/input", { key: "down" });
  await ctl("POST", "/tui/input", { key: "down" });
  await sleep(600);
  check("Pc", "input on the device's default screen does not move the preview", (await p.page.locator(".menu-fp-screen").innerText()) === before);
  await p.page.locator('[data-fp-key="down"]').click();
  await p.page.waitForFunction((b) => document.querySelector(".menu-fp-screen").innerText !== b, before);
  check("Pd", "the preview has its own cursor", true);
  await p.page.close();
});

// ================================================================== phone

await section("10", "390 px viewport: drill navigation, sheet, no sideways scroll", async () => {
  const phone = await newContext({ width: 390, height: 844 });
  const m = await newPage(phone);
  const mp = m.page;
  await mp.goto(`${BASE}/menu`);
  await mp.waitForSelector("#menu-page .menu-card");
  await shot(mp, "13-phone-home");
  const noScroll = () => mp.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  check("10a", "no sidebar on a phone; the filter is on Home", !(await mp.locator("#menu-sidebar").isVisible()) && (await mp.locator("#menu-filter-home").isVisible()));
  const bar = (await mp.locator("#menu-statusbar").innerText()).replace(/\s+/g, " ");
  check("10b", "the status bar keeps host and IP", /wlanpi-test/.test(bar) && /192\.0\.2\.10/.test(bar) && !/Classic/.test(bar), bar);
  check("10c", "no horizontal page scroll on Home", await noScroll());
  await mp.locator('.menu-card[data-node="network"]').click();
  await waitForPage(mp, "network");
  await shot(mp, "14-phone-branch");
  const rowBox = await mp.locator('.menu-row[data-node="network.interfaces"]').boundingBox();
  check("10d", "rows are full-width links at least 44 px high", rowBox.height >= 44 && rowBox.width >= 330, `${Math.round(rowBox.width)}x${Math.round(rowBox.height)}`);
  check("10e", "the breadcrumb collapses to a Back link", (await mp.locator(".menu-back").isVisible()) && !(await mp.locator(".menu-crumbs-full").isVisible()));
  await clickRow(mp, "network.interfaces");
  await waitForResult(mp);
  await shot(mp, "15-phone-result");
  check("10f", "drill navigation reaches the result", (await hash(mp)) === "#network.interfaces" && (await resultRows(mp)) >= 2);
  check("10g", "no horizontal page scroll on a result page", await noScroll());
  await mp.locator(".menu-back").click();
  await waitForPage(mp, "network");
  await clickRow(mp, "network.interfaces");
  await waitForResult(mp);

  await mp.locator("#menu-panel-toggle").click();
  await mp.waitForSelector("#menu-front-panel .menu-fp-screen .view-block");
  await shot(mp, "16-phone-sheet");
  const sheet = await mp.evaluate(() => {
    const el = document.querySelector("#menu-front-panel");
    const r = el.getBoundingClientRect();
    return { position: getComputedStyle(el).position, left: r.left, width: r.width, bottom: r.bottom, top: r.top, vh: innerHeight, vw: innerWidth };
  });
  check("10h", "the pane opens as a full-width bottom sheet covering at most the lower half",
    sheet.position === "fixed" && Math.round(sheet.left) === 0 && Math.round(sheet.width) === sheet.vw && Math.round(sheet.bottom) === sheet.vh && sheet.top >= sheet.vh / 2 - 1, sheet);
  check("10i", "no horizontal page scroll with the sheet open", await noScroll());
  await mp.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const covered = await mp.evaluate(() => {
    const last = Array.from(document.querySelectorAll("#menu-result tbody tr")).pop().getBoundingClientRect();
    return last.bottom > document.querySelector("#menu-front-panel").getBoundingClientRect().top;
  });
  check("10j", "the sheet never covers the last row", !covered);
  await mp.locator('[data-fp-action="collapse"]').click();
  await shot(mp, "17-phone-sheet-collapsed");
  const bar2 = await mp.evaluate(() => {
    const r = document.querySelector("#menu-front-panel").getBoundingClientRect();
    return { height: Math.round(r.height), title: document.querySelector("#menu-fp-title").innerText, keys: document.querySelector(".menu-fp-keys").offsetParent !== null };
  });
  check("10k", "collapsed, the sheet is a title bar showing the screen title", bar2.height <= 48 && bar2.height >= 44 && /^Front panel · \S/.test(bar2.title) && !bar2.keys, bar2);
  check("10l", "two connections on the phone too", m.open().length === 2);
  await phone.close();
});

// ================================================================== logout

await section("9", "logout closes both connections", async () => {
  await page.evaluate(() => { location.hash = "#network.interfaces"; });
  await waitForPage(page, "network.interfaces");
  await waitForResult(page);
  if (!(await page.locator("#menu-front-panel").isVisible())) await page.locator("#menu-panel-toggle").click();
  await page.waitForFunction(() => /live/.test((document.querySelector(".menu-fp-indicator") || {}).textContent || ""));
  const attached = (await ctl("GET", `/menud/attachments?user=${cfg.user}`)).attachments;
  check("9a", "before logout: the menu and the pane are both attached, at the same time",
    d.open().length === 2 && attached.length === 2 && attached.some((a) => a.kind === "new" && a.mode === "rich") && attached.some((a) => a.kind === "default" && a.mode === "thin"),
    JSON.stringify(attached));
  const rowsBefore = await resultRows(page);

  // The real logout: the Settings page's form, in another tab of the same browser.
  const other = await desktop.newPage();
  await other.goto(`${BASE}/settings`);
  let how = "Settings > Log out";
  if (await other.locator("button.logout-button").count()) {
    await Promise.all([other.waitForURL(/\/login/), other.locator("button.logout-button").click()]);
  } else {
    how = "POST /logout";
    await desktop.request.post(`${BASE}/logout`, { form: { csrf_token: cfg.csrf }, headers: { Origin: BASE, "X-CSRF-Token": cfg.csrf }, maxRedirects: 0 });
  }
  say(`logged out via ${how}`);
  await other.close();

  await until(() => d.open().length === 0, { timeout: 10000, what: "both sockets closed" });
  check("9b", "logout closes both sockets", d.open().length === 0, `open=${d.open().length}`);
  const fatal = d.sockets.filter((s) => s.received.some((f) => f.type === "error" && f.code === "AUTH_FAILED"));
  check("9c", "both were revoked by the service (AUTH_FAILED before the close)", fatal.length >= 2 && fatal.some((s) => s.sent[0].mode === "rich") && fatal.some((s) => s.sent[0].mode === "thin"),
    `revoked sockets=${fatal.length}`);
  await page.waitForSelector("#menu-banner:not([hidden])");
  const banner = (await page.locator("#menu-banner").innerText()).replace(/\s+/g, " ");
  await shot(page, "18-signed-out");
  check("9d", "the page shows the signed-out state with Sign in", /^You have been signed out\. ?Sign in$/.test(banner) && (await page.locator("#menu-banner a").getAttribute("href")) === "/login", banner);
  check("9e", "the pane shows it too", /You have been signed out\./.test(await page.locator(".menu-fp-conn").innerText()));
  check("9f", "content is kept, dimmed, with controls disabled", (await resultRows(page)) === rowsBefore && (await page.locator('[data-menu-action="refresh"]').isDisabled()) &&
    (await page.evaluate(() => document.querySelector("#menu-panel").classList.contains("is-stale"))));
  const n = d.sockets.length;
  await sleep(3500);
  check("9g", "neither connection retries after sign-out", d.sockets.length === n && (await page.evaluate(() => window.wlanpiMenu.connectionState().phase)) === "terminal", `sockets=${d.sockets.length}/${n}`);
  check("9h", "the service holds no connection for the user", (await ctl("GET", `/menud/attachments?user=${cfg.user}`)).count === 0);
});

await browser.close();
say(`done: ${failures} failed`);
process.exit(failures ? 1 : 0);
