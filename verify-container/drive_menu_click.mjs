#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const cookieHdr = process.argv[2];
if (!cookieHdr) {
  console.error("Usage: node drive_menu_click.mjs <cookie_hdr>");
  process.exit(1);
}

// 1. Fetch real /menu page through Flask/nginx
const pageResp = await fetch("https://localhost:443/menu", {
  headers: {
    Host: "localhost",
    Cookie: cookieHdr,
  },
});
if (!pageResp.ok) {
  console.error("Failed to fetch /menu:", pageResp.status);
  process.exit(1);
}
const pageHtml = await pageResp.text();
if (!pageHtml.includes('id="menu-panel"')) {
  console.error("menu-panel element not found in /menu HTML");
  process.exit(1);
}

// 2. Set up DOM and harness environment for menu.js
const webuiDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../wlanpi-webui");
const viewBlocksCode = fs.readFileSync(path.join(webuiDir, "wlanpi_webui/static/js/view_blocks.js"), "utf8");
const menuCode = fs.readFileSync(path.join(webuiDir, "wlanpi_webui/static/js/menu.js"), "utf8");

const elements = {};

class MockHTMLCollection {
  constructor(owner) {
    this._owner = owner;
    return new Proxy(this, {
      get(target, prop) {
        const arr = target._owner._children;
        if (prop === "length") return arr.length;
        if (prop === "item") return (idx) => arr[idx] ?? null;
        if (prop === "namedItem") return (name) => arr.find((el) => el.id === name || (el.getAttribute && el.getAttribute("name") === name)) ?? null;
        if (prop === Symbol.iterator) return arr[Symbol.iterator].bind(arr);
        if (typeof prop === "string" && !isNaN(prop)) {
          const idx = Number(prop);
          return arr[idx];
        }
        // HTMLCollection has no Array prototype methods like indexOf, slice, etc.
        return undefined;
      },
      has(target, prop) {
        const arr = target._owner._children;
        if (prop === "length" || prop === "item" || prop === "namedItem" || prop === Symbol.iterator) return true;
        if (typeof prop === "string" && !isNaN(prop)) return Number(prop) < arr.length;
        return false;
      },
    });
  }
}

class MockElement {
  constructor(id) {
    this._id = id;
    if (id) elements[id] = this;
    this.dataset = {};
    this.style = {};
    this._innerHTML = "";
    this.textContent = "";
    this.className = "";
    this.listeners = {};
    this.value = "";
    this.checked = false;
    this.parentElement = null;
    this.parentNode = null;
    this._children = [];
    this.children = new MockHTMLCollection(this);
  }
  set innerHTML(val) {
    this._innerHTML = val;
    if (val === "") {
      for (const c of this._children) {
        c.parentElement = null;
        c.parentNode = null;
      }
      this._children = [];
    }
  }
  get innerHTML() {
    return this._innerHTML ?? "";
  }
  set id(val) {
    this._id = val;
    if (val) elements[val] = this;
  }
  get id() {
    return this._id;
  }
  addEventListener(evt, fn) {
    this.listeners[evt] = this.listeners[evt] || [];
    this.listeners[evt].push(fn);
  }
  removeEventListener(evt, fn) {
    if (this.listeners[evt]) {
      this.listeners[evt] = this.listeners[evt].filter((f) => f !== fn);
    }
  }
  querySelector(sel) {
    if (sel === ".rich-prev-body") {
      for (const c of this._children) {
        if (c.className && c.className.includes("rich-prev-body")) return c;
      }
      const b = new MockElement("rich-prev-body");
      b.className = "rich-prev-body";
      this.appendChild(b);
      return b;
    }
    return null;
  }
  querySelectorAll() {
    return [];
  }
  get firstChild() {
    return this._children[0] || null;
  }
  appendChild(child) {
    if (child.parentElement) {
      child.parentElement.removeChild(child);
    }
    this._children.push(child);
    child.parentElement = this;
    child.parentNode = this;
    return child;
  }
  insertBefore(newChild, refChild) {
    if (newChild.parentElement) {
      newChild.parentElement.removeChild(newChild);
    }
    const idx = this._children.indexOf(refChild);
    if (idx !== -1) {
      this._children.splice(idx, 0, newChild);
    } else {
      this._children.unshift(newChild);
    }
    newChild.parentElement = this;
    newChild.parentNode = this;
    return newChild;
  }
  removeChild(child) {
    const idx = this._children.indexOf(child);
    if (idx !== -1) {
      this._children.splice(idx, 1);
    }
    child.parentElement = null;
    child.parentNode = null;
    return child;
  }
  scrollIntoView() {}
  dispatchEvent(event) {
    const fns = this.listeners[event.type] || [];
    for (const fn of fns) fn(event);
    const onHandler = this["on" + event.type];
    if (typeof onHandler === "function") onHandler(event);
  }
  closest(sel) {
    if (sel === ".menu-node-card") return this;
    if (sel === "#menu-mode-select") return this;
    return null;
  }
  getAttribute(name) {
    return this[name] || this.dataset[name] || null;
  }
  setAttribute(name, val) {
    this[name] = val;
    this.dataset[name] = val;
  }
}

// Instantiate standard menu DOM elements
elements["menu-panel"] = new MockElement("menu-panel");
elements["menu-mode-select"] = new MockElement("menu-mode-select");
elements["toggle-mirror-mode"] = new MockElement("toggle-mirror-mode");
elements["toggle-side-mirror"] = new MockElement("toggle-side-mirror");
elements["close-side-mirror"] = new MockElement("close-side-mirror");
elements["side-mirror-container"] = new MockElement("side-mirror-container");
elements["side-mirror-status"] = new MockElement("side-mirror-status");
elements["side-mirror-title"] = new MockElement("side-mirror-title");
elements["side-mirror-breadcrumb"] = new MockElement("side-mirror-breadcrumb");
elements["side-mirror-driven-by"] = new MockElement("side-mirror-driven-by");
elements["side-mirror-screen-blocks"] = new MockElement("side-mirror-screen-blocks");
elements["side-mirror-hints"] = new MockElement("side-mirror-hints");
elements["rich-mode-container"] = new MockElement("rich-mode-container");
elements["mirror-mode-container"] = new MockElement("mirror-mode-container");
elements["menu-connection-status"] = new MockElement("menu-connection-status");
elements["menu-session-kind"] = new MockElement("menu-session-kind");
elements["rich-device-summary"] = new MockElement("rich-device-summary");
elements["rich-menu-items"] = new MockElement("rich-menu-items");
elements["rich-action-detail"] = new MockElement("rich-action-detail");
elements["rich-job-results"] = new MockElement("rich-job-results");
elements["mirror-screen-blocks"] = new MockElement("mirror-screen-blocks");

let liveSocket = null;
const framesReceived = [];
const intermediatePrompts = [];
let menuFramePromiseResolve;
const menuFrameReceived = new Promise((res) => {
  menuFramePromiseResolve = res;
});
const jobResultQueue = [];
let nextJobResolve = null;

function waitForJobResult(timeoutMs = 5000) {
  if (jobResultQueue.length > 0) {
    return Promise.resolve(jobResultQueue.shift());
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Timeout waiting for job result")), timeoutMs);
    nextJobResolve = (frame) => {
      clearTimeout(timer);
      resolve(frame);
    };
  });
}

class LiveWebSocket extends globalThis.WebSocket {
  constructor(url) {
    super(url, {
      headers: {
        Host: "localhost",
        Origin: "https://localhost",
        Cookie: cookieHdr,
      },
    });
    liveSocket = this;

    // Intercept messages to record frames
    const origAddEventListener = this.addEventListener.bind(this);
    origAddEventListener("message", (evt) => {
      try {
        const frame = JSON.parse(evt.data);
        framesReceived.push(frame);
        if (frame.type === "menu" && menuFramePromiseResolve) {
          menuFramePromiseResolve(frame);
          menuFramePromiseResolve = null;
        }
        if (frame.type === "prompt") {
          intermediatePrompts.push(frame);
        }
        if (frame.type === "job" && frame.state === "result") {
          if (nextJobResolve) {
            const r = nextJobResolve;
            nextJobResolve = null;
            r(frame);
          } else {
            jobResultQueue.push(frame);
          }
        }
      } catch (e) {}
    });
  }
}

const windowListeners = {};
const documentListeners = {};
const ctx = {
  console: {
    log: (...args) => console.error("[client log]", ...args),
    warn: (...args) => console.error("[client warn]", ...args),
    error: (...args) => console.error("[client error]", ...args),
  },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  location: {
    protocol: "https:",
    host: "localhost",
  },
  document: {
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => new MockElement(tag),
    addEventListener: (evt, fn) => {
      documentListeners[evt] = fn;
    },
    activeElement: null,
  },
  window: {
    location: {
      protocol: "https:",
      host: "localhost",
    },
    addEventListener: (evt, fn) => {
      windowListeners[evt] = fn;
    },
    ViewBlocks: null,
    WebSocket: LiveWebSocket,
  },
  WebSocket: LiveWebSocket,
};

vm.createContext(ctx);
// Load ViewBlocks
vm.runInContext(viewBlocksCode, ctx);
ctx.window.ViewBlocks = ctx.ViewBlocks;

// Load menu.js
vm.runInContext(menuCode, ctx);

// Trigger init via window load
if (windowListeners.load) {
  windowListeners.load();
} else if (ctx.window.wlanpiMenu) {
  ctx.window.wlanpiMenu.init();
}

// 3. Wait for live menu frame from menud
const menuFrame = await Promise.race([
  menuFrameReceived,
  new Promise((_, rej) => setTimeout(() => rej(new Error("Timeout waiting for menu frame")), 5000)),
]);

// 4. Drive first card click (network.interfaces) via REAL browser event path
const card = new MockElement("card");
card["data-node-id"] = "network.interfaces";
card["data-is-branch"] = "0";

elements["menu-panel"].dispatchEvent({
  type: "click",
  target: card,
  preventDefault: () => {},
});

// 5. Wait for first job result from menud
const resultFrame = await waitForJobResult(5000);

// 6. Verify first result (interfaces grouped table)
const blocks = (resultFrame && resultFrame.result && resultFrame.result.blocks) || [];
const ifaceTable = blocks.find((b) => b.type === "table");
const hasTable = Boolean(ifaceTable);
const rows = (ifaceTable && ifaceTable.rows) || [];
const hasEthRow = rows.some((r) => Array.isArray(r) && r[0] === "Ethernet");
const hasWlanRow = rows.some((r) => Array.isArray(r) && r[0] === "WLAN");
const hasAdapterSubRow = rows.some(
  (r) =>
    Array.isArray(r) &&
    r[0] === "" &&
    (String(r[1]).includes("MAC:") ||
      String(r[1]).includes("Model:") ||
      String(r[1]).includes("Type:") ||
      String(r[1]).includes("Driver:"))
);
const groupedTableOk = Boolean(hasEthRow && hasWlanRow && hasAdapterSubRow);

const promptsCount = intermediatePrompts.length;
const detailDisplay = elements["rich-action-detail"].style.display;
const noSecondClick = (detailDisplay === "none" || !detailDisplay) && promptsCount === 0 && hasTable;

// 7. Drive second card click (network.lldp) to test bounded results pane
const card2 = new MockElement("card2");
card2["data-node-id"] = "network.lldp";
card2["data-is-branch"] = "0";

elements["menu-panel"].dispatchEvent({
  type: "click",
  target: card2,
  preventDefault: () => {},
});

const resultFrame2 = await waitForJobResult(5000);

const resultsContainer = elements["rich-job-results"];
const prevDetails = elements["rich-previous-result"];
const boundsOk =
  resultsContainer &&
  resultsContainer.children.length <= 2 &&
  prevDetails !== null &&
  prevDetails !== undefined;

const deviceStripHtml = elements["rich-device-summary"] ? elements["rich-device-summary"].innerHTML : "";
const hasEth0InStrip = deviceStripHtml.includes("Eth0 IP") && deviceStripHtml.includes("192.0.2.10");
const stripOk = Boolean(hasEth0InStrip);

if (liveSocket) {
  try {
    liveSocket.close(1000, "test done");
  } catch (e) {}
}

const allOk = noSecondClick && groupedTableOk && boundsOk && stripOk;

const summary = {
  ok: allOk,
  table: hasTable,
  prompts: promptsCount,
  detailDisplay: detailDisplay,
  groupedTable: groupedTableOk,
  boundsOk: boundsOk,
  stripOk: stripOk,
  childrenCount: resultsContainer ? resultsContainer.children.length : -1,
};

console.log(JSON.stringify(summary));
process.exit(allOk ? 0 : 1);
