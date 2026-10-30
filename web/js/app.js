/* Hyena Control Web — application shell.
   Talks to the local Python server (same origin) over JSON + Server-Sent Events. */
(function () {
  "use strict";

  var t = function (key, params, fallback) {
    return window.I18N.t(key, params, fallback);
  };
  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  };

  var state = {
    boot: null,
    settings: {},
    hub: { connected: false, busy: false, running: false, training: {} },
    training: { lines: 0, revision: 0, lost: false, available: false },
    files: [],
    examples: [],
    events: [],
    seen: 0,
    scan: [],
    scanSelected: null,
    scanning: false,
    scanTimer: null,
    editor: null,
    autoScroll: true,
    renderTimer: null
  };

  var MAX_CONSOLE_LINES = 2500;
  var MAX_EVENTS = 4000;

  // ------------------------------------------------------------------ utils
  function el(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        if (key === "class") node.className = props[key];
        else if (key === "text") node.textContent = props[key];
        else if (key === "html") node.innerHTML = props[key];
        else if (key === "dataset") Object.assign(node.dataset, props[key]);
        else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), props[key]);
        else if (props[key] !== null && props[key] !== undefined) node.setAttribute(key, props[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function svgIcon(name, size) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (size) { svg.setAttribute("style", "width:" + size + "px;height:" + size + "px"); }
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function formatBytes(size) {
    if (size < 1024) return size + " B";
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
    return (size / 1024 / 1024).toFixed(1) + " MB";
  }

  function ApiError(message, status, kind) {
    this.name = "ApiError";
    this.message = message || "request failed";
    this.status = status;
    this.kind = kind || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  async function api(path, body) {
    var options = { headers: { "X-Hyena-Client": "web" } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    var response = await fetch(path, options);
    var data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (!response.ok || (data && data.ok === false)) {
      var message = (data && data.error) || response.statusText || "request failed";
      throw new ApiError(message, response.status, data && data.kind);
    }
    return data || {};
  }

  function humanizeError(err) {
    var message = (err && err.message) || String(err);
    var map = [
      [/not connected to a hub/i, "errors.notConnected"],
      [/another hub operation/i, "errors.busy"],
      [/the program is empty/i, "errors.emptyProgram"],
      [/no successful upload/i, "errors.storedUnavailable"],
      [/file not found/i, "errors.notFound"],
      [/already exists/i, "errors.exists"],
      [/path escapes|invalid file name/i, "errors.invalidName"],
      [/only \.py/i, "errors.extension"],
      [/too large/i, "errors.tooLarge"],
      [/no training data/i, "race.noData"],
      [/incomplete session/i, "race.incomplete"],
      [/no robot\./i, "race.noCommands"]
    ];
    for (var i = 0; i < map.length; i += 1) {
      if (map[i][0].test(message)) return t(map[i][1]);
    }
    return message;
  }

  // ------------------------------------------------------------------ toasts
  function toast(level, message) {
    if (!message) return;
    var icons = { success: "check", error: "alert", warn: "alert", info: "info" };
    var node = el("div", { class: "toast " + (level || "info") }, [
      svgIcon(icons[level] || "info", 16),
      el("span", { text: message })
    ]);
    $("#toasts").appendChild(node);
    setTimeout(function () {
      node.classList.add("leaving");
      setTimeout(function () { node.remove(); }, 220);
    }, level === "error" ? 6000 : 3800);
  }

  // ------------------------------------------------------------------ theme
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
    $("#btn-theme").firstElementChild.firstElementChild.setAttribute(
      "href", theme === "dark" ? "#i-sun" : "#i-moon");
    if (state.editor) state.editor.setTheme(theme === "dark");
  }

  function applyFontSize(size) {
    document.documentElement.style.setProperty("--editor-font-size", (size || 14) + "px");
    if (state.editor) state.editor.refresh();
  }

  function applyLanguage(lang, persist) {
    window.I18N.setLang(lang);
    $("#btn-lang").textContent = lang === "ar" ? "EN" : "ع";
    $("#btn-lang").setAttribute("title", lang === "ar" ? "English" : "العربية");
    renderTabs();
    renderShortcuts();
    renderDrawer();
    renderHub();
    renderTraining();
    rerenderConsole();
    if (persist) saveSettings({ lang: lang });
  }

  async function saveSettings(changes) {
    Object.assign(state.settings, changes);
    try {
      var res = await api("/api/settings", changes);
      state.settings = res.settings;
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  // ------------------------------------------------------------------ console
  function localized(ev) {
    if (ev.key) {
      var text = t(ev.key, ev.params, null);
      if (text !== null) return text;
    }
    return ev.text;
  }

  function shouldTimestamp() { return state.settings.timestamps !== false; }

  function lineNode(ev) {
    var text = localized(ev);
    var children = [];
    if (shouldTimestamp() && ev.ts) children.push(el("span", { class: "ts", text: ev.ts }));
    var body = el("span", { class: "txt", text: text });
    body.setAttribute("dir", "auto");
    children.push(body);
    return el("div", { class: "line " + (ev.level || "info") }, children);
  }

  function nearBottom(node) {
    return node.scrollHeight - node.scrollTop - node.clientHeight < 60;
  }

  function appendEvent(ev) {
    if (!ev || !ev.t) return;
    if (ev.t === "console") {
      if (ev.seq && ev.seq <= state.seen) return;
      if (ev.seq) state.seen = Math.max(state.seen, ev.seq);
      state.events.push(ev);
      if (state.events.length > MAX_EVENTS) state.events.shift();
      var log = $("#console-log");
      var stick = state.autoScroll && nearBottom(log);
      var empty = log.querySelector(".console-empty");
      if (empty) empty.remove();
      log.appendChild(lineNode(ev));
      while (log.childElementCount > MAX_CONSOLE_LINES) log.firstElementChild.remove();
      if (stick) log.scrollTop = log.scrollHeight;
      renderConsoleMeta();
      return;
    }
    switch (ev.t) {
      case "state":
        applyHubState(ev.state);
        break;
      case "progress":
        applyProgress(ev);
        break;
      case "scan":
        applyScan(ev);
        break;
      case "training":
        state.training = ev;
        renderTraining();
        break;
      case "toast":
        toast(ev.level, localized(ev));
        break;
      case "server":
        if (ev.closing) toast("warn", t("errors.serverClosed"));
        break;
      default:
        break;
    }
  }

  function rerenderConsole() {
    var log = $("#console-log");
    log.innerHTML = "";
    if (!state.events.length) {
      log.appendChild(el("div", { class: "console-empty", text: t("console.empty") }));
    } else {
      var fragment = document.createDocumentFragment();
      state.events.forEach(function (ev) { fragment.appendChild(lineNode(ev)); });
      log.appendChild(fragment);
      log.scrollTop = log.scrollHeight;
    }
    renderConsoleMeta();
  }

  function renderConsoleMeta() {
    var meta = $("#console-meta");
    if (!state.hub.busy && !state.progressPercent) {
      meta.textContent = t("console.lines", { count: state.events.length });
    }
  }

  function applyProgress(ev) {
    var fill = $("#progress-fill");
    var meta = $("#console-meta");
    if (ev.phase === "idle") {
      fill.style.width = "0%";
      fill.className = "progress-fill";
      state.progressPercent = 0;
      renderConsoleMeta();
      return;
    }
    if (ev.phase === "scan") {
      meta.textContent = t("connect.scanning");
      return;
    }
    if (typeof ev.percent === "number") {
      state.progressPercent = ev.percent;
      fill.style.width = Math.max(0, Math.min(100, ev.percent)) + "%";
      fill.className = "progress-fill" + (ev.percent >= 100 ? " done" : "");
      if (ev.total) {
        meta.textContent = ev.percent + "% · " + formatBytes(ev.sent) + " / " + formatBytes(ev.total);
      } else {
        meta.textContent = ev.percent + "%";
      }
    }
  }

  async function copyConsole() {
    var selection = window.getSelection();
    var text = "";
    var log = $("#console-log");
    if (selection && selection.rangeCount && !selection.isCollapsed &&
        log.contains(selection.anchorNode)) {
      var pre = document.createElement("div");
      pre.appendChild(selection.getRangeAt(0).cloneContents());
      text = pre.innerText;
    } else {
      text = state.events.map(function (ev) {
        return shouldTimestamp() && ev.ts ? "[" + ev.ts + "] " + localized(ev) : localized(ev);
      }).join("\n");
    }
    if (!text) return toast("warn", t("console.nothing"));
    try {
      await navigator.clipboard.writeText(text);
      toast("success", t("console.copied"));
    } catch (err) {
      var area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("success", t("console.copied"));
    }
  }

  function clearConsole() {
    state.events = [];
    $("#console-log").innerHTML = "";
    $("#console-log").appendChild(el("div", { class: "console-empty", text: t("console.empty") }));
    renderConsoleMeta();
  }

  function exportConsole() {
    window.location.href = "/api/logs/download";
    toast("success", t("toast.exported"));
  }

  // ------------------------------------------------------------------ tabs
  function renderTabs() {
    var container = $("#tabs");
    if (!state.editor) return;
    container.innerHTML = "";
    state.editor.docs.forEach(function (doc) {
      var dirty = state.editor.isDirty(doc);
      var close = el("button", { class: "tab-close", title: t("tabs.close"), "aria-label": t("tabs.close") },
        [svgIcon("x")]);
      close.addEventListener("click", function (event) {
        event.stopPropagation();
        closeDocument(doc);
      });
      var tab = el("div", {
        class: "tab" + (doc.id === state.editor.activeId ? " active" : "") + (dirty ? " dirty" : ""),
        role: "tab",
        tabindex: "0",
        title: doc.name + (doc.path ? "\n" + doc.path : "")
      }, [
        el("span", { class: "tab-dot" }),
        el("span", { class: "tab-name", text: doc.name }),
        close
      ]);
      tab.addEventListener("click", function () { state.editor.activate(doc.id); });
      tab.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          state.editor.activate(doc.id);
        }
      });
      container.appendChild(tab);
    });
    var active = state.editor.active();
    document.title = active
      ? (state.editor.isDirty(active) ? "• " : "") + active.name + " — Hyena Control"
      : "Hyena Control";
  }

  function newDocument(content, name) {
    var existing = state.editor.docs.length;
    var doc = state.editor.addDocument({
      name: name || t("tabs.untitled", { n: existing + 1 }) + ".py",
      content: content === undefined ? "" : content
    });
    state.editor.activate(doc.id);
    renderTabs();
    return doc;
  }

  async function closeDocument(doc) {
    if (state.editor.isDirty(doc)) {
      var choice = await askUnsaved(doc);
      if (choice === "cancel") return;
      if (choice === "save") {
        var saved = await saveDocument(doc, false);
        if (!saved) return;
      }
    }
    state.editor.removeDocument(doc.id);
    if (!state.editor.docs.length) {
      state.editor.addDocument({ name: "main.py", content: "" });
    }
    renderTabs();
  }

  // ------------------------------------------------------------------ saving
  async function saveDocument(doc, saveAs, silent) {
    var target = doc || state.editor.active();
    if (!target) return false;
    var name = target.name;
    if (!target.path || saveAs) {
      var suggestion = /\.py$/i.test(name) ? name : name + ".py";
      name = await askPrompt({
        title: t("prompt.saveAs"),
        label: t("files.nameLabel"),
        value: suggestion
      });
      if (!name) return false;
      if (!/\.py$/i.test(name)) name += ".py";
    }
    try {
      var res = await api("/api/files/save", { name: name, content: target.cmDoc.getValue() });
      state.editor.markSaved(target, { name: res.name, path: res.path });
      setFiles(res.files);
      renderTabs();
      if (!silent) toast("success", t("toast.saved", { name: res.name }));
      return true;
    } catch (err) {
      toast("error", humanizeError(err));
      return false;
    }
  }

  // ------------------------------------------------------------------ dialogs
  function rebuildFooter(dialog, buttons) {
    var foot = dialog.querySelector(".modal-foot");
    foot.innerHTML = "";
    foot.appendChild(el("span", { class: "spacer" }));
    buttons.forEach(function (spec) {
      // Only the primary action submits: pressing Enter in a field must never
      // pick "cancel" just because it is the first button in the DOM.
      var isCancel = spec.value === "cancel" || spec.value === "discard";
      var button = el("button", {
        type: isCancel ? "button" : "submit",
        class: "btn " + (spec.class || "btn-ghost"),
        value: spec.value,
        text: spec.label
      });
      if (isCancel) {
        button.addEventListener("click", function () { dialog.close(spec.value); });
      }
      foot.appendChild(button);
    });
  }

  function askChoice(dialogId, title, text, options) {
    return new Promise(function (resolve) {
      var dialog = $("#" + dialogId);
      $("#confirm-title").textContent = title;
      $("#confirm-text").textContent = text;
      rebuildFooter(dialog, options);
      var done = function () {
        dialog.removeEventListener("close", done);
        var value = dialog.returnValue;
        resolve(value || "cancel");
      };
      dialog.addEventListener("close", done);
      if (!dialog.open) dialog.showModal();
    });
  }

  function askConfirm(title, text, okLabel) {
    return askChoice("dlg-confirm", title, text, [
      { value: "cancel", label: t("action.cancel") },
      { value: "ok", label: okLabel || t("action.confirm"), class: "btn-primary" }
    ]).then(function (value) { return value === "ok"; });
  }

  function askUnsaved(doc) {
    return askChoice("dlg-confirm", t("confirm.unsavedTitle"),
      t("confirm.unsavedText", { name: doc.name }), [
        { value: "cancel", label: t("confirm.cancel") },
        { value: "discard", label: t("confirm.discard") },
        { value: "save", label: t("confirm.save"), class: "btn-primary" }
      ]);
  }

  function askPrompt(options) {
    return new Promise(function (resolve) {
      var dialog = $("#dlg-prompt");
      var input = $("#prompt-input");
      $("#prompt-title").textContent = options.title || t("prompt.fileName");
      $("#prompt-label").textContent = options.label || t("files.nameLabel");
      input.value = options.value || "";
      dialog.returnValue = "";
      var done = function () {
        dialog.removeEventListener("close", done);
        var value = dialog.returnValue === "ok" ? input.value.trim() : "";
        resolve(value || null);
      };
      dialog.addEventListener("close", done);
      if (!dialog.open) dialog.showModal();
      setTimeout(function () { input.focus(); input.select(); }, 30);
    });
  }

  // ------------------------------------------------------------------ hub view
  function applyHubState(hub) {
    if (!hub) return;
    state.hub = hub;
    if (hub.training) state.training = hub.training;
    renderHub();
    renderTraining();
  }

  function renderHub() {
    var hub = state.hub || {};
    var chip = $("#hub-chip");
    var name = $("#hub-name");
    var sub = $("#hub-sub");
    var connected = !!hub.connected;
    chip.classList.toggle("is-connected", connected && !hub.busy);
    chip.classList.toggle("is-busy", !!hub.busy);

    if (connected) {
      name.textContent = hub.label || t("hub.connected");
      var parts = [];
      if (hub.firmware) parts.push(t("hub.firmware", { version: hub.firmware }));
      if (hub.mode === "simulation") parts.push(t("status.simulation"));
      sub.textContent = parts.join(" · ") || t("hub.connected");
    } else {
      name.textContent = t("hub.none");
      sub.textContent = hub.busy ? t("hub.busy") : t("hub.hintConnect");
    }

    var stop = $("#btn-stop");
    var run = $("#btn-run");
    var runMenu = $("#btn-run-menu");
    stop.disabled = !connected;
    run.disabled = !connected || !!hub.busy;
    runMenu.disabled = !connected || !!hub.busy;
    $$("#run-popover .menu-item").forEach(function (item) {
      item.disabled = !connected || !!hub.busy ||
        (item.dataset.run === "stored" && !hub.can_run_stored);
    });

    var dot = $("#status-dot");
    dot.className = "dot" + (connected ? (hub.busy ? " busy" : (hub.running ? " ok" : "")) : " err");
    $("#status-connection").textContent = connected
      ? (hub.busy ? t("status.busy") : t("status.connected"))
      : t("status.disconnected");
    $("#status-program").textContent = hub.busy && hub.operation
      ? t("status.busy")
      : (hub.running ? t("status.running") : t("status.idle"));

    var battery = $("#status-battery-wrap");
    if (typeof hub.battery === "number") {
      battery.hidden = false;
      $("#status-battery").textContent = hub.battery + "%";
    } else {
      battery.hidden = true;
    }

    $("#status-mode").textContent = hub.mode === "simulation"
      ? t("status.simulation") : t("status.hardware");
    $("#sim-badge").hidden = hub.mode !== "simulation";

    var dialogOpen = $("#dlg-connect").open;
    if (!dialogOpen) $("#connect-target").value = state.settings.hub_name || "";
    var disconnect = $("#btn-disconnect");
    if (disconnect) disconnect.hidden = !connected;
    var scanButton = $("#btn-scan");
    if (scanButton) scanButton.disabled = !!hub.busy;
    var connectButton = $("#btn-connect");
    if (connectButton) connectButton.disabled = !!hub.busy;
  }

  function renderTraining() {
    var training = state.training || {};
    $("#status-training").textContent = training.lines || 0;
  }

  // ------------------------------------------------------------------ run
  async function runProgram(mode, wait) {
    var doc = state.editor.active();
    if (!doc) return;
    if (!state.hub.connected) {
      toast("warn", t("errors.notConnected"));
      openConnect();
      return;
    }
    if (state.hub.busy) {
      toast("warn", t("toast.busy"));
      return;
    }
    var code = doc.cmDoc.getValue();
    if (mode !== "stored") {
      if (!code.trim()) {
        toast("warn", t("errors.emptyProgram"));
        return;
      }
      if (doc.path) {
        var saved = await saveDocument(doc, false, true);
        if (!saved) return;
      }
    }
    try {
      await api("/api/program/run", {
        code: mode === "stored" ? "" : code,
        name: doc.name,
        mode: mode,
        wait: !!wait
      });
      if (mode === "download") toast("success", t("log.download_ok"));
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  async function stopProgram() {
    if (!state.hub.connected) return;
    try {
      await api("/api/hub/stop", {});
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  // ------------------------------------------------------------------ connect
  function openConnect() {
    var dialog = $("#dlg-connect");
    $("#connect-target").value = state.settings.hub_name || "";
    $("#connect-error").style.display = "none";
    renderScanList();
    if (!dialog.open) dialog.showModal();
    setTimeout(function () { $("#connect-target").focus(); }, 30);
  }

  function renderScanList() {
    var list = $("#scan-list");
    list.innerHTML = "";
    if (state.scanning) {
      list.appendChild(el("div", { class: "scan-empty" }, [
        el("span", { class: "spinner" }),
        el("span", { text: t("connect.scanning") })
      ]));
      return;
    }
    if (!state.scan.length) {
      list.appendChild(el("div", { class: "scan-empty", text: t("connect.scanHint") }));
      return;
    }
    state.scan.forEach(function (hub) {
      var selected = state.scanSelected === hub.address;
      var row = el("div", { class: "scan-row" + (selected ? " selected" : ""), tabindex: "0" }, [
        el("span", { class: "hub-ico" }, [svgIcon("bluetooth", 18)]),
        el("span", { class: "scan-name" }, [
          el("b", { text: hub.name || "(unnamed)" }),
          el("span", { text: hub.address || "" })
        ]),
        el("span", { class: "rssi", text: typeof hub.rssi === "number" ? hub.rssi + " dBm" : "" })
      ]);
      var choose = function () {
        state.scanSelected = hub.address;
        $("#connect-target").value = hub.address || hub.name || "";
        renderScanList();
      };
      row.addEventListener("click", choose);
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
      });
      list.appendChild(row);
    });
  }

  function applyScan(ev) {
    if (ev.done) {
      state.scanning = false;
      clearTimeout(state.scanTimer);
      state.scan = ev.hubs || [];
      if (!state.scan.length) {
        $("#scan-list").innerHTML = "";
        $("#scan-list").appendChild(el("div", { class: "scan-empty", text: t("connect.noneFound") }));
        return;
      }
      renderScanList();
      toast("info", t("connect.found", { count: state.scan.length }));
    } else {
      (ev.hubs || []).forEach(function (hub) {
        var known = state.scan.some(function (item) { return item.address === hub.address; });
        if (!known) state.scan.push(hub);
      });
      if (!state.scanning) {
        state.scanning = true;
      }
      renderScanList();
    }
  }

  async function startScan() {
    if (state.scanning) return;
    state.scan = [];
    state.scanSelected = null;
    state.scanning = true;
    renderScanList();
    try {
      await api("/api/hub/scan", { timeout: state.settings.scan_timeout || 8 });
      clearTimeout(state.scanTimer);
      state.scanTimer = setTimeout(function () {
        if (state.scanning) {
          state.scanning = false;
          renderScanList();
        }
      }, (state.settings.scan_timeout || 8) * 1000 + 5000);
    } catch (err) {
      state.scanning = false;
      renderScanList();
      showConnectError(humanizeError(err));
    }
  }

  function showConnectError(message) {
    $("#connect-error").style.display = "flex";
    $("#connect-error-text").textContent = message;
  }

  async function doDisconnect() {
    try {
      await api("/api/hub/disconnect", {});
      toast("info", t("toast.disconnected"));
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  async function doConnect() {
    var target = $("#connect-target").value.trim();
    if (!target) {
      var proceed = await askConfirm(t("confirm.title"), t("connect.emptyConfirm"),
        t("action.connect"));
      if (!proceed) return;
    }
    saveSettings({ hub_name: target });
    try {
      await api("/api/hub/connect", { target: target || null });
      $("#dlg-connect").close();
    } catch (err) {
      showConnectError(humanizeError(err));
    }
  }

  // ------------------------------------------------------------------ files
  function setFiles(files) {
    if (files) state.files = files;
    if ($("#drawer").dataset.panel === "files") renderDrawer();
  }

  async function refreshFiles() {
    try {
      var res = await api("/api/files");
      setFiles(res.files);
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  async function openFileByName(name) {
    try {
      var existing = state.editor.docs.filter(function (doc) { return doc.path && doc.name === name; })[0];
      if (existing) {
        state.editor.activate(existing.id);
        return;
      }
      var res = await api("/api/files/open", { name: name });
      var doc = state.editor.addDocument({ name: res.name, path: res.path, content: res.content });
      state.editor.activate(doc.id);
      renderTabs();
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  async function deleteFile(name) {
    var ok = await askConfirm(t("action.delete"), t("files.deleteConfirm", { name: name }),
      t("action.delete"));
    if (!ok) return;
    try {
      var res = await api("/api/files/delete", { name: name });
      setFiles(res.files);
      toast("success", t("toast.deleted", { name: name }));
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  async function renameFile(name) {
    var next = await askPrompt({ title: t("files.renameTitle"), label: t("files.nameLabel"), value: name });
    if (!next || next === name) return;
    try {
      var res = await api("/api/files/rename", { name: name, new_name: next });
      setFiles(res.files);
      state.editor.docs.forEach(function (doc) {
        if (doc.name === name && doc.path) { doc.name = res.name; doc.path = res.path; }
      });
      renderTabs();
      toast("success", t("toast.renamed", { name: res.name }));
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  function uploadFile() {
    $("#file-input").value = "";
    $("#file-input").click();
  }

  function handleFileInput(event) {
    var file = event.target.files && event.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      newDocument(String(reader.result || ""), file.name);
      toast("success", t("files.uploaded"));
    };
    reader.readAsText(file);
  }

  // ------------------------------------------------------------------ drawer
  var PANELS = ["files", "examples", "race", "help"];

  function togglePanel(panel) {
    var drawer = $("#drawer");
    var open = drawer.dataset.panel === panel && !drawer.hidden;
    $$(".rail button[data-panel]").forEach(function (button) {
      button.classList.toggle("active", !open && button.dataset.panel === panel);
    });
    if (open) {
      drawer.hidden = true;
      drawer.dataset.panel = "";
      return;
    }
    drawer.dataset.panel = panel;
    drawer.hidden = false;
    renderDrawer();
  }

  function closeDrawer() {
    $("#drawer").hidden = true;
    $("#drawer").dataset.panel = "";
    $$(".rail button[data-panel]").forEach(function (button) { button.classList.remove("active"); });
  }

  function renderDrawer() {
    var drawer = $("#drawer");
    var panel = drawer.dataset.panel;
    if (!panel || drawer.hidden) return;
    $("#drawer-title").textContent = t("panel." + panel);
    var body = $("#drawer-body");
    body.innerHTML = "";
    if (panel === "files") renderFilesPanel(body);
    else if (panel === "examples") renderExamplesPanel(body);
    else if (panel === "race") renderRacePanel(body);
    else if (panel === "help") renderHelpPanel(body);
  }

  function renderFilesPanel(body) {
    body.appendChild(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, [
      el("button", { class: "btn btn-sm btn-primary", onclick: function () { newDocument("", null); } }, [
        svgIcon("file-add", 15), el("span", { text: t("files.create") })
      ]),
      el("button", { class: "btn btn-sm btn-ghost", onclick: uploadFile }, [
        svgIcon("upload", 15), el("span", { text: t("files.upload") })
      ]),
      el("button", {
        class: "btn btn-sm btn-ghost", title: t("files.refresh"),
        onclick: refreshFiles
      }, [svgIcon("refresh", 15)])
    ]));

    body.appendChild(el("h4", { text: t("files.folder") }));
    var list = el("div", { class: "file-list" });
    if (!state.files.length) {
      list.appendChild(el("div", { class: "empty-note", text: t("files.empty") }));
    }
    state.files.forEach(function (file) {
      var actions = el("div", { class: "file-actions" }, [
        el("button", {
          title: t("files.download"),
          onclick: function (event) {
            event.stopPropagation();
            window.location.href = "/api/files/download?name=" + encodeURIComponent(file.name);
          }
        }, [svgIcon("download")]),
        el("button", {
          title: t("files.rename"),
          onclick: function (event) { event.stopPropagation(); renameFile(file.name); }
        }, [svgIcon("edit")]),
        el("button", {
          class: "danger", title: t("files.delete"),
          onclick: function (event) { event.stopPropagation(); deleteFile(file.name); }
        }, [svgIcon("trash")])
      ]);
      var row = el("div", { class: "file-row", tabindex: "0", onclick: function () { openFileByName(file.name); } }, [
        el("span", { class: "file-icon" }, [svgIcon("file")]),
        el("span", { class: "file-meta" }, [
          el("span", { class: "file-name", text: file.name }),
          el("span", { class: "file-sub", text: file.modified + " · " + formatBytes(file.size) })
        ]),
        actions
      ]);
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter") openFileByName(file.name);
      });
      list.appendChild(row);
    });
    body.appendChild(list);
    body.appendChild(el("div", { class: "hint-box" }, [
      svgIcon("info"), el("span", { text: t("files.hint") })
    ]));
  }

  function renderExamplesPanel(body) {
    body.appendChild(el("p", {
      style: "margin-top:0;color:var(--muted);font-size:12.5px;line-height:1.6",
      text: t("examples.subtitle")
    }));
    state.examples.forEach(function (example) {
      var title = example.name[window.I18N.lang] || example.name.en;
      var description = example.description[window.I18N.lang] || example.description.en;
      var card = el("div", { class: "example-card", tabindex: "0" }, [
        el("strong", { text: title }),
        el("span", { text: description })
      ]);
      var insert = function () {
        newDocument(example.code, example.id + ".py");
        toast("success", t("examples.insert"));
      };
      card.addEventListener("click", insert);
      card.addEventListener("keydown", function (event) {
        if (event.key === "Enter") insert();
      });
      body.appendChild(card);
    });
    body.appendChild(el("div", { class: "hint-box" }, [
      svgIcon("info"), el("span", { text: t("examples.hint") })
    ]));
  }

  function renderRacePanel(body) {
    var training = state.training || {};
    body.appendChild(el("h4", { text: t("race.sessionTitle") }));
    body.appendChild(el("div", { class: "metric-row" }, [
      el("span", { text: t("race.lines") }),
      el("b", { text: String(training.lines || 0) })
    ]));
    body.appendChild(el("div", { class: "metric-row" }, [
      el("span", { text: t("race.state") }),
      el("span", {
        class: "pill " + (training.available ? "ok" : (training.lost ? "err" : "")),
        text: training.available ? t("race.ready") : (training.lost ? t("race.lost") : "—")
      })
    ]));
    var actions = el("div", { style: "display:flex;flex-direction:column;gap:7px;margin-top:14px" }, [
      el("button", { class: "btn btn-primary", onclick: generateRace }, [
        svgIcon("race", 16), el("span", { text: t("race.generate") })
      ]),
      el("button", {
        class: "btn btn-ghost",
        onclick: async function () {
          var ok = await askConfirm(t("race.reset"), t("race.resetConfirm"));
          if (!ok) return;
          try {
            await api("/api/training/reset", {});
            toast("info", t("toast.reset"));
            renderDrawer();
          } catch (err) { toast("error", humanizeError(err)); }
        }
      }, [svgIcon("refresh", 16), el("span", { text: t("race.reset") })]),
      state.hub.mode === "simulation" ? el("button", {
        class: "btn btn-ghost",
        onclick: async function () {
          try {
            await api("/api/program/simulate-training", {});
            toast("success", t("toast.demo"));
            renderDrawer();
          } catch (err) { toast("error", humanizeError(err)); }
        }
      }, [svgIcon("terminal", 16), el("span", { text: t("race.demo") })]) : null
    ]);
    body.appendChild(actions);
    body.appendChild(el("div", { class: "hint-box" }, [
      svgIcon("info"), el("span", { text: t("race.hint") })
    ]));
  }

  function renderHelpPanel(body) {
    body.appendChild(el("p", {
      style: "margin-top:0;font-size:12.5px;line-height:1.7;color:var(--muted)",
      text: t("help.usageText")
    }));
    body.appendChild(el("button", {
      class: "btn btn-ghost", style: "width:100%",
      onclick: openHelp
    }, [svgIcon("help", 16), el("span", { text: t("help.title") })]));
  }

  // ------------------------------------------------------------------ race
  async function generateRace() {
    try {
      var res = await api("/api/race/preview", {});
      var dialog = $("#dlg-race");
      var area = $("#race-code");
      area.value = res.code;
      area.hidden = false;
      $("#race-empty").hidden = true;
      $("#race-stats").style.display = "flex";
      $("#race-stats-text").textContent = t("race.stats", {
        kept: res.kept, total: res.total, skipped: res.skipped
      });
      $("#btn-race-open").disabled = false;
      $("#btn-race-save").disabled = false;
      state.raceCode = res.code;
      if (!dialog.open) dialog.showModal();
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  function openRaceInEditor() {
    if (!state.raceCode) return;
    var stamp = new Date();
    var name = "race_" + pad(stamp.getHours()) + pad(stamp.getMinutes()) + pad(stamp.getSeconds()) + ".py";
    newDocument(state.raceCode, name);
    $("#dlg-race").close();
    toast("success", t("race.generated"));
  }

  async function saveRaceToWorkspace() {
    if (!state.raceCode) return;
    var name = await askPrompt({
      title: t("race.fileName"),
      label: t("files.nameLabel"),
      value: "race_" + Date.now().toString().slice(-6) + ".py"
    });
    if (!name) return;
    try {
      var res = await api("/api/files/save", { name: name, content: state.raceCode });
      setFiles(res.files);
      toast("success", t("toast.saved", { name: res.name }));
    } catch (err) {
      toast("error", humanizeError(err));
    }
  }

  // ------------------------------------------------------------------ settings
  function openSettings() {
    var dialog = $("#dlg-settings");
    $("#set-lang").value = state.settings.lang || "ar";
    $("#set-theme").value = state.settings.theme || "light";
    $("#set-font").value = state.settings.font_size || 14;
    $("#set-retries").value = state.settings.retries || 6;
    $("#set-timeout").value = state.settings.scan_timeout || 20;
    $("#set-autoconnect").checked = !!state.settings.autoconnect;
    $("#set-timestamps").checked = state.settings.timestamps !== false;
    $("#set-autoscroll").checked = state.settings.auto_scroll !== false;
    $("#set-workspace").value = (state.boot && state.boot.workspace.dir) || "";
    $("#set-logs").value = (state.boot && state.boot.paths.logs) || "";
    $("#set-version").textContent = (state.boot && state.boot.version) || "";
    $("#set-mode").textContent = state.hub.mode === "simulation" ? t("status.simulation") : t("status.hardware");
    if (!dialog.open) dialog.showModal();
  }

  async function submitSettings() {
    var changes = {
      lang: $("#set-lang").value,
      theme: $("#set-theme").value,
      font_size: parseInt($("#set-font").value, 10) || 14,
      retries: parseInt($("#set-retries").value, 10) || 6,
      scan_timeout: parseInt($("#set-timeout").value, 10) || 20,
      autoconnect: $("#set-autoconnect").checked,
      timestamps: $("#set-timestamps").checked,
      auto_scroll: $("#set-autoscroll").checked
    };
    state.autoScroll = changes.auto_scroll;
    applyTheme(changes.theme);
    applyFontSize(changes.font_size);
    await saveSettings(changes);
    if (changes.lang !== window.I18N.lang) applyLanguage(changes.lang, false);
    else renderShortcuts();
    toast("success", t("settings.saved"));
  }

  // ------------------------------------------------------------------ help
  var SHORTCUTS = [
    ["shortcut.run", "F5"],
    ["shortcut.runWait", "Ctrl+F5"],
    ["shortcut.stop", "Esc"],
    ["shortcut.save", "Ctrl+S"],
    ["shortcut.saveAs", "Ctrl+Shift+S"],
    ["shortcut.new", "Ctrl+N"],
    ["shortcut.open", "Ctrl+O"],
    ["shortcut.find", "Ctrl+F"],
    ["shortcut.closeTab", "Ctrl+W"],
    ["shortcut.nextTab", "Ctrl+Tab"],
    ["shortcut.prevTab", "Ctrl+Shift+Tab"],
    ["shortcut.race", "Ctrl+G"],
    ["shortcut.comment", "Ctrl+/"],
    ["shortcut.hint", "Ctrl+Space"],
    ["shortcut.copyConsole", "Ctrl+Shift+C"],
    ["shortcut.help", "F1"]
  ];

  function renderShortcuts() {
    var table = $("#shortcut-table");
    if (!table) return;
    table.innerHTML = "";
    SHORTCUTS.forEach(function (row) {
      table.appendChild(el("tr", {}, [
        el("td", { text: t(row[0]) }),
        el("td", {}, [el("span", { class: "kbd", text: row[1] })])
      ]));
    });
  }

  function openHelp() {
    renderShortcuts();
    var dialog = $("#dlg-help");
    if (!dialog.open) dialog.showModal();
  }

  // ------------------------------------------------------------------ console panel
  function setConsoleHeight(height) {
    var consoleEl = $("#console");
    var max = Math.max(160, window.innerHeight * 0.72);
    height = Math.max(80, Math.min(max, height));
    if (consoleEl.classList.contains("collapsed")) consoleEl.classList.remove("collapsed");
    consoleEl.style.height = height + "px";
    state.consoleHeight = height;
    try { localStorage.setItem("hyena.consoleHeight", String(height)); } catch (err) { /* ignore */ }
    if (state.editor) state.editor.refresh();
  }

  function collapseConsole(force) {
    var consoleEl = $("#console");
    var collapsed = force === undefined ? !consoleEl.classList.contains("collapsed") : force;
    consoleEl.classList.toggle("collapsed", collapsed);
    try { localStorage.setItem("hyena.consoleCollapsed", collapsed ? "1" : "0"); } catch (err) { /* ignore */ }
    if (state.editor) state.editor.refresh();
  }

  function setupResizer() {
    var resizer = $("#resizer");
    var dragging = false;
    var startY = 0;
    var startHeight = 0;

    resizer.addEventListener("pointerdown", function (event) {
      dragging = true;
      startY = event.clientY;
      startHeight = $("#console").getBoundingClientRect().height;
      resizer.setPointerCapture(event.pointerId);
      document.body.style.userSelect = "none";
    });
    resizer.addEventListener("pointermove", function (event) {
      if (!dragging) return;
      // A horizontal divider behaves the same in RTL and LTR.
      var delta = event.clientY - startY;
      setConsoleHeight(startHeight - delta);
    });
    var finish = function () {
      dragging = false;
      document.body.style.userSelect = "";
      if (state.editor) state.editor.refresh();
    };
    resizer.addEventListener("pointerup", finish);
    resizer.addEventListener("pointercancel", finish);
    resizer.addEventListener("dblclick", function () { collapseConsole(); });
    resizer.addEventListener("keydown", function (event) {
      var height = $("#console").getBoundingClientRect().height;
      if (event.key === "ArrowUp") { event.preventDefault(); setConsoleHeight(height - 24); }
      if (event.key === "ArrowDown") { event.preventDefault(); setConsoleHeight(height + 24); }
    });
  }

  // ------------------------------------------------------------------ boot
  function renderShortcutsTableOnce() { renderShortcuts(); }

  function wireUI() {
    $("#btn-menu").addEventListener("click", function (event) {
      event.stopPropagation();
      togglePopover("#main-menu", "#btn-menu");
    });
    $("#btn-run-menu").addEventListener("click", function (event) {
      event.stopPropagation();
      togglePopover("#run-popover", "#btn-run-menu");
    });
    document.addEventListener("click", function () {
      closePopovers();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closePopovers();
    });

    $("#btn-run").addEventListener("click", function () { runProgram("run", false); });
    $("#btn-stop").addEventListener("click", stopProgram);
    $$("#run-popover [data-run]").forEach(function (item) {
      item.addEventListener("click", function () {
        closePopovers();
        var mode = item.dataset.run;
        if (mode === "wait") runProgram("run", true);
        else if (mode === "download") runProgram("download", false);
        else if (mode === "stored") runProgram("stored", false);
        else runProgram("run", false);
      });
    });

    $("#btn-lang").addEventListener("click", function () {
      applyLanguage(window.I18N.lang === "ar" ? "en" : "ar", true);
    });
    $("#btn-theme").addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      saveSettings({ theme: next });
    });
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-help").addEventListener("click", openHelp);
    $("#hub-chip").addEventListener("click", openConnect);

    $("#menu-new").addEventListener("click", function () { closePopovers(); newDocument("", null); });
    $("#menu-open").addEventListener("click", function () { closePopovers(); togglePanel("files"); });
    $("#menu-save").addEventListener("click", function () { closePopovers(); saveDocument(null, false); });
    $("#menu-save-as").addEventListener("click", function () { closePopovers(); saveDocument(null, true); });
    $("#menu-examples").addEventListener("click", function () { closePopovers(); togglePanel("examples"); });
    $("#menu-race").addEventListener("click", function () { closePopovers(); generateRace(); });
    $("#menu-settings").addEventListener("click", function () { closePopovers(); openSettings(); });
    $("#menu-help").addEventListener("click", function () { closePopovers(); openHelp(); });

    $("#tab-add").addEventListener("click", function () { newDocument("", null); });
    $("#btn-save").addEventListener("click", function () { saveDocument(null, false); });
    $("#btn-search").addEventListener("click", function () { state.editor.find(); });

    $$(".rail button[data-panel]").forEach(function (button) {
      button.addEventListener("click", function () { togglePanel(button.dataset.panel); });
    });
    $("#drawer-close").addEventListener("click", closeDrawer);

    $("#console-toggle").addEventListener("click", function () { collapseConsole(); });
    $("#console-copy").addEventListener("click", copyConsole);
    $("#console-clear").addEventListener("click", clearConsole);
    $("#console-export").addEventListener("click", exportConsole);
    $("#console-autoscroll").addEventListener("click", function () {
      state.autoScroll = !state.autoScroll;
      $("#console-autoscroll").style.color = state.autoScroll ? "" : "var(--muted-2)";
      saveSettings({ auto_scroll: state.autoScroll });
    });

    $("#btn-scan").addEventListener("click", startScan);
    $("#btn-disconnect").addEventListener("click", doDisconnect);
    $("#connect-form").addEventListener("submit", function (event) {
      event.preventDefault();
      doConnect();
    });
    $$("#dlg-connect [data-close]").forEach(function (button) {
      button.addEventListener("click", function () { $("#dlg-connect").close(); });
    });

    $("#settings-form").addEventListener("submit", function (event) {
      event.preventDefault();
      $("#dlg-settings").close();
      submitSettings();
    });
    $$("#dlg-settings [data-close]").forEach(function (button) {
      button.addEventListener("click", function () { $("#dlg-settings").close(); });
    });
    $("#btn-reset-settings").addEventListener("click", async function () {
      await saveSettings({
        lang: "ar", theme: "light", font_size: 14, retries: 6, scan_timeout: 20,
        autoconnect: false, timestamps: true, auto_scroll: true
      });
      applyTheme("light");
      applyFontSize(14);
      applyLanguage("ar", false);
      openSettings();
      toast("success", t("settings.resetDone"));
    });

    $("#btn-race-generate").addEventListener("click", generateRace);
    $("#btn-race-open").addEventListener("click", openRaceInEditor);
    $("#btn-race-save").addEventListener("click", saveRaceToWorkspace);
    $$("#dlg-race [data-close]").forEach(function (button) {
      button.addEventListener("click", function () { $("#dlg-race").close(); });
    });

    $$("#dlg-help [data-close]").forEach(function (button) {
      button.addEventListener("click", function () { $("#dlg-help").close(); });
    });

    $("#file-input").addEventListener("change", handleFileInput);

    $("#prompt-form").addEventListener("submit", function (event) {
      var input = $("#prompt-input");
      if (!input.value.trim()) {
        event.preventDefault();
        input.reportValidity();
      }
    });

    setupResizer();
    setupGlobalKeys();

    window.addEventListener("resize", function () {
      if (state.editor) state.editor.refresh();
    });
    window.addEventListener("beforeunload", function (event) {
      if (state.editor && state.editor.docs.some(function (doc) {
        return state.editor.isDirty(doc);
      })) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  function togglePopover(popoverSelector, buttonSelector) {
    var popover = $(popoverSelector);
    var isOpen = popover.classList.contains("open");
    closePopovers();
    if (!isOpen) {
      popover.classList.add("open");
      $(buttonSelector).setAttribute("aria-expanded", "true");
    }
  }

  function closePopovers() {
    $$(".popover.open").forEach(function (popover) { popover.classList.remove("open"); });
    $$("[aria-expanded='true']").forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
  }

  function setupGlobalKeys() {
    document.addEventListener("keydown", function (event) {
      if (event.defaultPrevented) return;
      var mod = event.ctrlKey || event.metaKey;
      var key = event.key;
      var inDialog = !!document.querySelector("dialog[open]");

      if (key === "Escape" && !inDialog) {
        event.preventDefault();
        stopProgram();
        return;
      }
      if (key === "F5") {
        event.preventDefault();
        runProgram("run", event.ctrlKey);
        return;
      }
      if (key === "F1") {
        event.preventDefault();
        openHelp();
        return;
      }
      if (!mod) return;
      if (key === "s" || key === "S") {
        event.preventDefault();
        saveDocument(null, event.shiftKey);
      } else if (key === "n" || key === "N") {
        event.preventDefault();
        newDocument("", null);
      } else if (key === "o" || key === "O") {
        event.preventDefault();
        togglePanel("files");
      } else if (key === "w" || key === "W") {
        event.preventDefault();
        var doc = state.editor.active();
        if (doc) closeDocument(doc);
      } else if (key === "g" || key === "G") {
        event.preventDefault();
        generateRace();
      } else if (key === "Tab") {
        event.preventDefault();
        var docs = state.editor.docs;
        var index = docs.findIndex(function (doc) { return doc.id === state.editor.activeId; });
        var next = (index + (event.shiftKey ? -1 : 1) + docs.length) % docs.length;
        if (docs[next]) state.editor.activate(docs[next].id);
      } else if ((key === "C" || key === "c") && event.shiftKey) {
        var active = document.activeElement;
        if (active && active.closest && active.closest("#console")) {
          event.preventDefault();
          copyConsole();
        }
      }
    });
  }

  // ------------------------------------------------------------------ SSE
  function connectEvents() {
    var source = new EventSource("/api/events");
    source.onmessage = function (message) {
      try {
        appendEvent(JSON.parse(message.data));
      } catch (err) {
        console.error("bad event", err, message.data);
      }
    };
    source.onerror = function () {
      var connection = $("#status-connection");
      if (connection && !state.hub.connected) connection.textContent = t("status.disconnected");
    };
    state.stream = source;
  }

  // ------------------------------------------------------------------ init
  var started = false;

  async function init() {
    if (started) return;
    started = true;
    var boot;
    try {
      boot = await api("/api/bootstrap");
    } catch (err) {
      document.body.insertAdjacentHTML("afterbegin",
        '<div style="padding:24px;font-family:system-ui">' +
        "The local server did not answer. Start it with <code>python spike_server.py</code>" +
        "</div>");
      return;
    }
    state.boot = boot;
    state.settings = boot.settings || {};
    state.files = (boot.workspace && boot.workspace.files) || [];
    state.examples = boot.examples || [];
    state.autoScroll = state.settings.auto_scroll !== false;

    window.I18N.setLang(state.settings.lang || "ar");
    applyTheme(state.settings.theme || "light");
    applyFontSize(state.settings.font_size || 14);
    $("#btn-lang").textContent = (state.settings.lang === "ar") ? "EN" : "ع";

    // editor first, so language-driven re-renders have something to talk to
    state.editor = new window.HyenaEditor.EditorManager({
      host: $("#editor-wrap"),
      onChange: function () { renderTabs(); },
      onDocs: renderTabs,
      onCursor: function (position) {
        $("#status-cursor").textContent = "Ln " + position.line + ", Col " + position.ch;
      },
      onDiagnostics: function (diagnostics) {
        state.diagnostics = diagnostics;
      },
      onRun: function () { runProgram("run", false); },
      onRunWait: function () { runProgram("run", true); },
      onStop: stopProgram,
      onSave: function () { saveDocument(null, false); },
      onSaveAs: function () { saveDocument(null, true); },
      onNew: function () { newDocument("", null); },
      onOpen: function () { togglePanel("files"); },
      onCloseTab: function () { var doc = state.editor.active(); if (doc) closeDocument(doc); },
      onNextTab: function () { switchTab(1); },
      onPrevTab: function () { switchTab(-1); },
      onHelp: openHelp,
      onRace: generateRace,
      lintEnabled: function () { return state.settings.lint !== false; }
    });
    state.editor.setTheme(state.settings.theme === "dark");

    // restore the last edited program, otherwise show the starter template
    var last = state.settings.last_file;
    var restored = false;
    if (last) {
      try {
        var opened = await api("/api/files/open", { name: last });
        state.editor.addDocument({ name: opened.name, path: opened.path, content: opened.content });
        restored = true;
      } catch (err) { restored = false; }
    }
    if (!restored) {
      state.editor.addDocument({ name: "main.py", content: boot.starter || "" });
    }
    state.editor.activate(state.editor.docs[0].id, { force: true });
    renderTabs();

    wireUI();
    renderShortcuts();
    applyHubState(boot.state);
    renderTraining();

    $("#console-autoscroll").style.color = state.autoScroll ? "" : "var(--muted-2)";

    // console backlog from the server
    (boot.logs || []).forEach(appendEvent);
    if (!state.events.length) rerenderConsole();

    appendLocal("sys", "Hyena Control " + boot.version + " — " +
      (boot.simulation ? t("status.simulation") : t("status.hardware")));
    if (boot.simulation) {
      appendLocal("info", t("toast.simulationMode"));
    }

    var savedHeight = parseInt(localStorage.getItem("hyena.consoleHeight") || "", 10);
    if (savedHeight) setConsoleHeight(savedHeight);
    if (localStorage.getItem("hyena.consoleCollapsed") === "1") collapseConsole(true);

    connectEvents();
    if (state.settings.autoconnect && !boot.state.connected) {
      api("/api/hub/connect", { target: state.settings.hub_name || null }).catch(function () { });
    }
    window.addEventListener("resize", function () { state.editor.refresh(); });
  }

  function switchTab(direction) {
    var docs = state.editor.docs;
    if (!docs.length) return;
    var index = docs.findIndex(function (doc) { return doc.id === state.editor.activeId; });
    var next = (index + direction + docs.length) % docs.length;
    state.editor.activate(docs[next].id);
  }

  function appendLocal(level, text) {
    var log = $("#console-log");
    if (!log) return;
    var empty = log.querySelector(".console-empty");
    if (empty) empty.remove();
    log.appendChild(el("div", { class: "line " + level }, [
      el("span", { class: "txt", text: text })
    ]));
    log.scrollTop = log.scrollHeight;
  }

  document.addEventListener("DOMContentLoaded", init);
})();
