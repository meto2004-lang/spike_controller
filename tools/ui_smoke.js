/* Headless smoke test for web/js/app.js using jsdom and a CodeMirror stub.
 *
 * The real editor bundle needs layout, so this replaces CodeMirror with a tiny
 * stand-in and checks the application shell: tabs, console events, i18n, the
 * run button, drawers and dialogs. It catches wiring mistakes without a browser.
 *
 * Usage:
 *   npm install jsdom      # once, outside the repository
 *   node tools/ui_smoke.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "..", "web");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8")
  .replace(/<link[^>]+>/g, "")
  .replace(/<script src="[^"]+"><\/script>/g, "");

const { VirtualConsole } = require("jsdom");
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => console.log("JSDOM ERROR:", e.message, e.detail && e.detail.stack));
virtualConsole.on("error", (...a) => console.log("CONSOLE ERROR:", ...a));
const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8123/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;
const errors = [];
window.addEventListener("error", (e) => errors.push("window error: " + (e.error && e.error.stack || e.message)));
window.addEventListener("unhandledrejection", (e) => errors.push("unhandled rejection: " + (e.reason && e.reason.stack || e.reason)));

// ---- polyfills -----------------------------------------------------------
if (!window.HTMLElement.prototype.showModal) {
  window.HTMLElement.prototype.showModal = function () {
    this.open = true;
    this.setAttribute("open", "");
  };
  window.HTMLElement.prototype.close = function (value) {
    this.open = false;
    this.removeAttribute("open");
    this.returnValue = value === undefined ? "" : value;
    this.dispatchEvent(new window.Event("close"));
  };
}

const bootstrap = {
  version: "2.1.0",
  simulation: true,
  pybricksdev: false,
  transport: { simulation: true, forced_simulation: true, reason: "simulate_flag",
               reason_detail: null, pybricksdev: null, bleak: null,
               python: "3.11.2", platform: "linux" },
  settings: { lang: "ar", theme: "light", font_size: 14, retries: 6, scan_timeout: 20,
              autoconnect: false, hub_name: "", timestamps: true, auto_scroll: true, last_file: null },
  state: { connected: true, busy: false, running: false, label: "SPIKE Prime (simulation)",
           firmware: "4.1.0-sim", mode: "simulation", battery: 87, can_run_stored: true,
           operation: null, connected: true, training: { lines: 4, available: true, lost: false } },
  workspace: { dir: "/tmp/programs", files: [{ name: "main.py", size: 12, mtime: 1, modified: "2026-01-01 10:00" }] },
  starter: "# starter\nprint('hi')\n",
  examples: [{ id: "hello", name: { ar: "مرحبا", en: "Hello" },
               description: { ar: "وصف", en: "desc" }, code: "print(1)\n" }],
  logs: [{ t: "console", level: "info", text: "backlog line", ts: "10:00:00", seq: 1 }],
  log_seq: 1,
  paths: { data: "/tmp", logs: "/tmp/logs" },
  time: "2026-01-01 10:00:00",
};

const calls = [];
window.fetch = async (url, options) => {
  calls.push({ url, options });
  if (url === "/api/bootstrap") return jsonResponse(bootstrap);
  if (url === "/api/lint") return jsonResponse({ ok: true, diagnostics: [] });
  return jsonResponse({ ok: true, files: bootstrap.workspace.files, settings: bootstrap.settings });
};

function jsonResponse(data) {
  return { ok: true, status: 200, json: async () => data };
}

let sourceInstance = null;
window.EventSource = class {
  constructor(url) { this.url = url; sourceInstance = this; }
  close() {}
};
window.EventSource.prototype.emit = function (data) {};
window.__emit = (data) => sourceInstance.onmessage({ data: JSON.stringify(data) });

// ---- CodeMirror stub ------------------------------------------------------
function Doc(text, mode) { this._text = text || ""; this.mode = mode; this.cursor = { line: 0, ch: 0 }; }
Doc.prototype.getValue = function () { return this._text; };
Doc.prototype.setValue = function (v) { this._text = v; };
function FakeCM(host, options) {
  this.host = host;
  this.options = Object.assign({}, options);
  this._doc = new Doc(this.options.value || "");
  this._events = {};
  this.state = {};
  this._lintCalls = 0;
}
FakeCM.prototype.getWrapperElement = function () { const d = window.document.createElement("div"); this.host.appendChild(d); return d; };
FakeCM.prototype.on = function (name, fn) { (this._events[name] = this._events[name] || []).push(fn); };
FakeCM.prototype.getValue = function () { return this._doc._text; };
FakeCM.prototype.setValue = function (text) { this._doc._text = text; };
FakeCM.prototype.getDoc = function () { return this._doc; };
FakeCM.prototype.swapDoc = function (doc) { this._doc = doc; };
FakeCM.prototype.getCursor = function () { return this._doc.cursor; };
FakeCM.prototype.setCursor = function (pos) { this._doc.cursor = pos; };
FakeCM.prototype.getLine = function (n) { return (this._doc._text.split("\n")[n] || ""); };
FakeCM.prototype.lineCount = function () { return this._doc._text.split("\n").length; };
FakeCM.prototype.setOption = function (k, v) { this.options[k] = v; };
FakeCM.prototype.getOption = function (k) { return this.options[k]; };
FakeCM.prototype.performLint = function () { this._lintCalls++; };
FakeCM.prototype.focus = function () {};
FakeCM.prototype.refresh = function () {};
FakeCM.prototype.getScrollInfo = function () { return { top: 0 }; };
FakeCM.prototype.scrollTo = function () {};
FakeCM.prototype.scrollIntoView = function () {};
FakeCM.prototype.execCommand = function () {};
FakeCM.prototype.somethingSelected = function () { return false; };
FakeCM.prototype.replaceSelection = function () {};
FakeCM.prototype.indentSelection = function () {};
FakeCM.prototype.showHint = function () {};
FakeCM.prototype.getSelection = function () { return ""; };
window.__cms = [];
window.CodeMirror = function (host, options) {
  const cm = new FakeCM(host, options);
  window.__cms.push({ host, cm });
  return cm;
};
window.CodeMirror.Doc = function (text, mode) { return new Doc(text, mode); };
window.CodeMirror.Pos = function (line, ch) { return { line, ch }; };
window.CodeMirror.registerHelper = function () {};
window.CodeMirror.defineMode = function () {};

// ---- load the app ---------------------------------------------------------
function load(file) {
  const code = fs.readFileSync(path.join(ROOT, file), "utf8");
  window.eval(code);
}
load("js/i18n.js");
load("js/editor.js");
load("js/blocks.js");
load("js/app.js");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const assert = (condition, message) => {
  if (!condition) { errors.push("ASSERT: " + message); }
};

(async () => {
  await wait(400);

  assert(window.document.documentElement.dir === "rtl", "starts RTL");
  assert(window.document.querySelectorAll(".tab").length === 1, "one tab exists");
  assert(window.document.querySelector("#hub-name").textContent.includes("SPIKE"), "hub name shown");
  assert(window.document.querySelector("#console-log").textContent.includes("backlog line"), "backlog rendered");
  assert(window.document.querySelector("#status-training").textContent === "4", "training lines");

  // live events
  window.__emit({ t: "console", level: "out", text: "hub says hi", ts: "10:00:01", seq: 2 });
  window.__emit({ t: "state", state: Object.assign({}, bootstrap.state, { busy: true, running: true, operation: "run" }) });
  window.__emit({ t: "progress", phase: "upload", percent: 42, sent: 42, total: 100 });
  await wait(30);
  assert(window.document.querySelector("#console-log").textContent.includes("hub says hi"), "console event rendered");
  assert(window.document.querySelector("#progress-fill").style.width === "42%", "progress bar");
  assert(window.document.querySelector("#btn-run").disabled === true, "run disabled while busy");
  assert(window.document.querySelector("#status-program").textContent.includes("جارٍ"), "status busy");

  window.__emit({ t: "state", state: Object.assign({}, bootstrap.state, { busy: false, running: false }) });
  await wait(20);
  assert(window.document.querySelector("#btn-run").disabled === false, "run enabled when idle");

  // language switch
  window.document.querySelector("#btn-lang").click();
  await wait(60);
  assert(window.document.documentElement.dir === "ltr", "switched to LTR");
  assert(window.document.documentElement.lang === "en", "switched to English");
  assert(window.document.querySelector("#btn-lang").textContent === "ع", "toggle label");
  assert(window.document.querySelector("#console-log").textContent.includes("hub says hi"), "console survives language switch");

  // run action
  window.document.querySelector("#btn-run").click();
  await wait(40);
  const runCall = calls.filter((c) => c.url === "/api/program/run").pop();
  assert(!!runCall, "run posts to the server");
  if (runCall) {
    const body = JSON.parse(runCall.options.body);
    assert(body.mode = "run", "run mode");
    assert(body.name.endsWith(".py"), "program name");
  }

  // new tab + dirty marker
  window.document.querySelector("#tab-add").click();
  await wait(40);
  assert(window.document.querySelectorAll(".tab").length === 2, "new tab opened");

  // drawer + examples
  window.document.querySelector('.rail button[data-panel="examples"]').click();
  await wait(40);
  assert(!window.document.querySelector("#drawer").hidden, "drawer opened");
  assert(window.document.querySelectorAll(".example-card").length === 1, "example rendered");

  // settings dialog opens
  window.document.querySelector("#btn-settings").click();
  await wait(30);
  assert(window.document.querySelector("#dlg-settings").open === true, "settings dialog");
  assert(window.document.querySelector("#set-workspace").value === "/tmp/programs", "workspace path shown");

  // connect dialog + scan rendering
  window.document.querySelector("#dlg-settings").close();
  window.document.querySelector("#hub-chip").click();
  await wait(20);
  assert(window.document.querySelector("#dlg-connect").open === true, "connect dialog");
  assert(window.document.querySelector("#connect-status").dataset.tone === "warn",
         "connect dialog explains why Bluetooth is off");
  assert(window.document.querySelector("#connect-status-title").textContent.trim().length > 0,
         "connect dialog status has a title");
  assert(window.document.querySelector("#connect-status-text").textContent.includes("محاكاة") ||
         window.document.querySelector("#connect-status-text").textContent.toLowerCase().includes("simulat"),
         "connect dialog explains simulation mode");
  window.__emit({ t: "scan", done: true, hubs: [{ name: "Hub A", address: "AA:BB", rssi: -50, simulated: true }] });
  await wait(20);
  assert(window.document.querySelectorAll(".scan-row").length === 1, "scan result rendered");
  assert(!!window.document.querySelector(".scan-badge"), "simulated hub is labelled");

  // race generation
  window.document.querySelector("#dlg-connect").close();
  window.document.querySelector("#btn-race-generate").click();
  await wait(60);
  assert(window.document.querySelector("#dlg-race").open === true, "race dialog opened from drawer/menu");

  // ---- block programming mode ---------------------------------------------
  window.document.querySelector("#dlg-race").close();
  const tabsBefore = window.document.querySelectorAll(".tab").length;
  window.document.querySelector("#menu-new-blocks").click();
  await wait(50);
  assert(window.document.querySelectorAll(".tab").length === tabsBefore + 1, "block tab added");
  assert(!!window.document.querySelector(".tab.tab-blocks"), "block tab is marked");
  assert(window.document.querySelector("#mode-switch").hidden === false, "mode switch shown");
  assert(window.document.querySelector("#blocks-host").hidden === false, "block canvas shown");
  assert(window.document.querySelector("#btn-convert").hidden === false, "convert button shown");
  assert(window.document.querySelectorAll(".blk-palette-item").length === 15,
         "palette lists every block type");
  assert(!!window.document.querySelector(".blk-hat"), "set up hat block rendered");
  assert(window.document.querySelector("#blocks-host").textContent.includes("Program"),
         "program hat rendered");

  // add the first palette block (keyboard path: jsdom has no pointer events)
  window.document.querySelectorAll(".blk-palette-item")[0]
    .dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await wait(30);
  assert(window.document.querySelectorAll(".blk-stack .blk").length === 1,
         "palette block added to the program");
  const textInput = window.document.querySelector(".blk-stack .blk input.blk-input");
  assert(!!textInput, "print block exposes an editable field");
  if (textInput) {
    textInput.value = "Hello from blocks";
    textInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  }
  await wait(30);
  assert(!!window.document.querySelector(".tab.tab-blocks.dirty"), "editing a block marks the tab dirty");

  // generated python preview
  window.document.querySelector('#mode-switch button[data-mode="python"]').click();
  await wait(30);
  assert(window.document.querySelector("#blocks-host").hidden === true, "blocks hidden in python view");
  assert(window.document.querySelector("#code-preview").hidden === false, "python preview shown");
  const preview = window.__cms.filter((entry) => entry.host.id === "code-preview").pop();
  assert(!!preview, "python preview editor created");
  if (preview) {
    const generated = preview.cm.getValue();
    assert(generated.includes("from pybricks.hubs import PrimeHub"), "setup code generated");
    assert(generated.includes("robot = DriveBase("), "drive base configured");
    assert(generated.includes('print("Hello from blocks")'), "edited field reaches the python");
  }

  // back to blocks, then convert into a python tab
  window.document.querySelector('#mode-switch button[data-mode="blocks"]').click();
  await wait(30);
  assert(window.document.querySelector("#blocks-host").hidden === false, "blocks come back");

  const tabsBeforeConvert = window.document.querySelectorAll(".tab").length;
  window.document.querySelector("#btn-convert").click();
  await wait(80);
  assert(window.document.querySelectorAll(".tab").length === tabsBeforeConvert + 1,
         "converted python tab opened");
  assert(!window.document.querySelector(".tab.active.tab-blocks"), "python tab becomes active");
  assert(window.document.querySelector("#mode-switch").hidden === true, "mode switch hidden for python");
  assert(window.document.querySelector("#blocks-host").hidden === true, "canvas hidden for python");

  console.log(errors.length ? "FAILURES:\n" + errors.join("\n") : "UI SMOKE OK");
  console.log("tabs:", window.document.querySelectorAll(".tab").length,
              "| console lines:", window.document.querySelectorAll("#console-log .line").length,
              "| fetch calls:", calls.length);
  process.exit(errors.length ? 1 : 0);
})().catch((err) => {
  console.error("SMOKE CRASH", err);
  process.exit(2);
});
