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
class MockElement {
  constructor(id) {
    this._id = id;
    if (id) elements[id] = this;
    this.dataset = {};
    this.style = {};
    this.innerHTML = "";
    this.textContent = "";
    this.className = "";
    this.listeners = {};
    this.value = "";
    this.children = [];
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
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  get firstChild() {
    return this.children[0] || null;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  insertBefore(newChild, refChild) {
    const idx = this.children.indexOf(refChild);
    if (idx !== -1) {
      this.children.splice(idx, 0, newChild);
    } else {
      this.children.unshift(newChild);
    }
    return newChild;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    return child;
  }
  scrollIntoView() {}
  dispatchEvent(event) {
    const fns = this.listeners[event.type] || [];
    for (const fn of fns) fn(event);
  }
  closest(sel) {
    if (sel === ".menu-node-card") return this;
    if (sel === "#menu-mode-select") return this;
    return null;
  }
  getAttribute(name) {
    return this[name] || this.dataset[name] || null;
  }
}

// Instantiate standard menu DOM elements
elements["menu-panel"] = new MockElement("menu-panel");
elements["menu-mode-select"] = new MockElement("menu-mode-select");
elements["toggle-mirror-mode"] = new MockElement("toggle-mirror-mode");
elements["rich-mode-container"] = new MockElement("rich-mode-container");
elements["mirror-mode-container"] = new MockElement("mirror-mode-container");
elements["menu-connection-status"] = new MockElement("menu-connection-status");
elements["menu-session-kind"] = new MockElement("menu-session-kind");
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
let jobResultPromiseResolve;
const jobResultReceived = new Promise((res) => {
  jobResultPromiseResolve = res;
});

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
        if (frame.type === "menu") {
          menuFramePromiseResolve(frame);
        }
        if (frame.type === "prompt") {
          intermediatePrompts.push(frame);
        }
        if (frame.type === "job" && frame.state === "result") {
          jobResultPromiseResolve(frame);
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

// 4. Drive card click via REAL browser event path
const card = new MockElement("card");
card["data-node-id"] = "network.interfaces";
card["data-is-branch"] = "0";

elements["menu-panel"].dispatchEvent({
  type: "click",
  target: card,
  preventDefault: () => {},
});

// 5. Wait for job result from menud
const resultFrame = await Promise.race([
  jobResultReceived,
  new Promise((_, rej) => setTimeout(() => rej(new Error("Timeout waiting for job result (auto-invoke failed)")), 5000)),
]);

// 6. Verify result
const blocks = (resultFrame && resultFrame.result && resultFrame.result.blocks) || [];
const hasTable = blocks.some((b) => b.type === "table");
const promptsCount = intermediatePrompts.length;
const detailDisplay = elements["rich-action-detail"].style.display;

// Detail pane must remain hidden (single click direct invoke)
const noSecondClick = (detailDisplay === "none" || !detailDisplay) && promptsCount === 0 && hasTable;

if (liveSocket) {
  try {
    liveSocket.close(1000, "test done");
  } catch (e) {}
}

const summary = {
  ok: noSecondClick,
  table: hasTable,
  prompts: promptsCount,
  detailDisplay: detailDisplay,
};

console.log(JSON.stringify(summary));
process.exit(noSecondClick ? 0 : 1);
