/* CodeMirror wrapper: documents, tabs, Pybricks autocomplete and syntax checks. */
(function () {
  "use strict";

  var PYBRICKS_WORDS = [
    "from", "import", "as", "def", "class", "return", "if", "elif", "else", "for",
    "while", "break", "continue", "pass", "True", "False", "None", "and", "or",
    "not", "in", "is", "try", "except", "finally", "with", "lambda", "global",
    "yield", "raise", "assert", "del", "await", "async",
    "print", "range", "len", "int", "float", "str", "bool", "list", "dict",
    "enumerate", "abs", "min", "max", "round", "sum", "sorted", "zip",
    "wait", "const",
    "PrimeHub", "TechnicHub", "CityHub", "InventorHub", "EssentialHub",
    "Motor", "ColorSensor", "ColorDistanceSensor", "UltrasonicSensor",
    "ForceSensor", "Light", "Speaker", "IMU", "Battery", "Buttons", "Display",
    "DriveBase", "Port", "Direction", "Color", "Button", "Stop", "Icon",
    "Matrix", "Number", "Sound", "ColorHSV", "Side",
    "hub.light.on", "hub.light.off", "hub.speaker.beep", "hub.speaker.volume",
    "hub.display.text", "hub.display.icon", "hub.display.off", "hub.display.number",
    "hub.buttons.pressed", "hub.buttons.on_click", "hub.imu.heading",
    "hub.imu.reset_heading", "hub.imu.ready", "hub.battery.voltage",
    "hub.battery.current", "hub.system.set_stop_button", "hub.system.shutdown",
    "robot.straight", "robot.turn", "robot.drive", "robot.stop", "robot.settings",
    "robot.use_gyro", "robot.distance", "robot.angle", "robot.reset",
    "motor.run", "motor.run_angle", "motor.run_time", "motor.stop", "motor.brake",
    "motor.hold", "motor.angle", "motor.speed", "motor.reset_angle",
    "sensor.color", "sensor.reflection", "sensor.rgb", "sensor.hsv",
    "sensor.distance", "sensor.pressed", "sensor.force", "sensor.touched",
    "sensor.lights.on", "sensor.lights.off"
  ];

  function wordList(cm) {
    var seen = Object.create(null);
    var words = [];
    function push(word) {
      if (!word || seen[word]) return;
      seen[word] = true;
      words.push(word);
    }
    PYBRICKS_WORDS.forEach(push);
    var text = cm.getValue();
    var match;
    var pattern = /[A-Za-z_][A-Za-z0-9_]{2,}/g;
    while ((match = pattern.exec(text)) !== null) push(match[0]);
    return words.sort();
  }

  function hyenaHint(cm) {
    var cursor = cm.getCursor();
    var line = cm.getLine(cursor.line) || "";
    var start = cursor.ch;
    while (start > 0 && /[\w.]/.test(line.charAt(start - 1))) start -= 1;
    var typed = line.slice(start, cursor.ch);
    var list = wordList(cm).filter(function (word) {
      return !typed || word.toLowerCase().indexOf(typed.toLowerCase()) === 0;
    });
    return {
      list: list.slice(0, 80),
      from: CodeMirror.Pos(cursor.line, start),
      to: CodeMirror.Pos(cursor.line, cursor.ch)
    };
  }

  function makeAnnotation(diagnostic, cm) {
    var line = Math.max(0, Math.min(cm.lineCount() - 1, (diagnostic.line || 1) - 1));
    var lineText = cm.getLine(line) || "";
    var ch = Math.max(0, Math.min(lineText.length, (diagnostic.column || 1) - 1));
    var end = Math.min(lineText.length, ch + 1);
    return {
      from: CodeMirror.Pos(line, ch),
      to: CodeMirror.Pos(line, Math.max(end, ch)),
      message: diagnostic.message,
      severity: diagnostic.severity === "warning" ? "warning" : "error"
    };
  }

  function EditorManager(options) {
    this.options = options || {};
    this.docs = [];
    this.activeId = null;
    this.nextId = 1;
    this._lintCache = new Map();
    this._lintTimer = null;
    this._lintRequest = 0;
    this._scrollTops = new Map();
    this._suppress = false;

    var self = this;
    this.cm = CodeMirror(this.options.host, {
      value: "",
      mode: "python",
      theme: "hyena",
      lineNumbers: true,
      lineWrapping: false,
      indentUnit: 4,
      tabSize: 4,
      indentWithTabs: false,
      smartIndent: true,
      electricChars: true,
      matchBrackets: true,
      autoCloseBrackets: { pairs: "()[]{}''\"\"", triples: "", explode: "()[]{}" },
      styleActiveLine: true,
      foldGutter: true,
      gutters: ["CodeMirror-linenumbers", "CodeMirror-foldgutter", "CodeMirror-lint-markers"],
      extraKeys: this._extraKeys(),
      hintOptions: { hint: hyenaHint, completeSingle: false },
      lint: {
        // CodeMirror 5 calls this synchronously and expects the annotations
        // back; the server check is cached and re-applied with performLint().
        getAnnotations: function (text) { return self._getAnnotations(text); },
        delay: 500,
        tooltips: true
      },
      scrollbarStyle: "native"
    });

    this.cm.on("change", function () { if (!self._suppress) self._changed(); });
    this.cm.on("cursorActivity", function () {
      if (self.options.onCursor) {
        var cursor = self.cm.getCursor();
        self.options.onCursor({ line: cursor.line + 1, ch: cursor.ch + 1 });
      }
    });
    this.cm.on("inputRead", function (cmInstance, change) {
      var inserted = change.text.join("\n");
      if (inserted === "." || (inserted.length === 1 && /[A-Za-z_]/.test(inserted))) {
        var line = cmInstance.getLine(change.from.line) || "";
        if (inserted === "." || /[A-Za-z0-9_]\)?\.[A-Za-z_]+$/.test(line)) {
          clearTimeout(self._hintTimer);
          self._hintTimer = setTimeout(function () {
            if (!cmInstance.state.completionActive) cmInstance.showHint({ completeSingle: false });
          }, 140);
        }
      }
    });
  }

  EditorManager.prototype._extraKeys = function () {
    var self = this;
    var call = function (name) {
      return function () {
        if (self.options[name]) self.options[name]();
      };
    };
    return {
      "Tab": function (cm) {
        if (cm.somethingSelected()) cm.indentSelection("add");
        else cm.replaceSelection("    ", "end");
      },
      "Shift-Tab": function (cm) { cm.indentSelection("subtract"); },
      "Ctrl-/": "toggleComment",
      "Cmd-/": "toggleComment",
      "Ctrl-Space": "autocomplete",
      "Alt-/": "autocomplete",
      "Ctrl-F": "find",
      "Cmd-F": "find",
      "F5": call("onRun"),
      "Ctrl-F5": call("onRunWait"),
      "Esc": call("onStop"),
      "Ctrl-S": call("onSave"),
      "Cmd-S": call("onSave"),
      "Ctrl-Shift-S": call("onSaveAs"),
      "Ctrl-N": call("onNew"),
      "Ctrl-O": call("onOpen"),
      "Ctrl-W": call("onCloseTab"),
      "Ctrl-Tab": call("onNextTab"),
      "Ctrl-Shift-Tab": call("onPrevTab"),
      "F1": call("onHelp"),
      "Ctrl-G": call("onRace")
    };
  };

  // -- documents ------------------------------------------------------
  EditorManager.prototype.addDocument = function (spec) {
    var kind = spec.kind === "blocks" ? "blocks" : "python";
    var content = spec.content === undefined ? "" : spec.content;
    var cmDoc = kind === "python" ? CodeMirror.Doc(content, "python") : null;
    var doc = {
      id: this.nextId++,
      kind: kind,
      name: spec.name || "program.py",
      path: spec.path || null,
      cmDoc: cmDoc,
      blocks: spec.blocks || null,
      viewMode: kind === "blocks" ? "blocks" : "python",
      dirtyBlocks: false,
      savedContent: kind === "python"
        ? (spec.savedContent === undefined ? (spec.path ? content : "") : spec.savedContent)
        : ""
    };
    this.docs.push(doc);
    if (this.activeId === null) this.activate(doc.id, { force: true });
    if (this.options.onDocs) this.options.onDocs();
    return doc;
  };

  EditorManager.prototype.active = function () {
    var id = this.activeId;
    return this.docs.filter(function (doc) { return doc.id === id; })[0] || null;
  };

  EditorManager.prototype.byId = function (id) {
    return this.docs.filter(function (doc) { return doc.id === id; })[0] || null;
  };

  EditorManager.prototype.activate = function (id, opts) {
    opts = opts || {};
    var target = this.byId(id);
    if (!target) return null;
    var current = this.active();
    if (current && current.id !== id) {
      this._scrollTops.set(current.id, this.cm.getScrollInfo().top);
    }
    if (this.activeId === id && !opts.force) return target;
    this.activeId = id;
    this._suppress = true;
    if (target.kind === "python") {
      this.cm.swapDoc(target.cmDoc);
      this.cm.setOption("mode", "python");
    }
    this._suppress = false;
    if (target.kind === "python") {
      var top = this._scrollTops.get(id) || 0;
      var cm = this.cm;
      requestAnimationFrame(function () { cm.scrollTo(null, top); cm.focus(); });
      this.cm.performLint();
      if (this.options.onCursor) {
        var cursor = this.cm.getCursor();
        this.options.onCursor({ line: cursor.line + 1, ch: cursor.ch + 1 });
      }
    }
    if (this.options.onDocs) this.options.onDocs();
    if (this.options.onActivate) this.options.onActivate(target, current);
    return target;
  };

  EditorManager.prototype.removeDocument = function (id) {
    var index = this.docs.findIndex(function (doc) { return doc.id === id; });
    if (index < 0) return;
    var wasActive = this.activeId === id;
    this.docs.splice(index, 1);
    this._scrollTops.delete(id);
    if (wasActive) {
      this.activeId = null;
      if (this.docs.length) {
        var next = this.docs[Math.min(index, this.docs.length - 1)];
        this.activate(next.id, { force: true });
      }
    }
    if (this.options.onDocs) this.options.onDocs();
  };

  EditorManager.prototype.getValue = function () { return this.cm.getValue(); };

  EditorManager.prototype.focus = function () { this.cm.focus(); };

  EditorManager.prototype.refresh = function () { this.cm.refresh(); };

  EditorManager.prototype.setTheme = function (dark) {
    this.cm.setOption("theme", dark ? "hyena-dark" : "hyena");
  };

  EditorManager.prototype.find = function () {
    this.cm.execCommand("find");
  };

  EditorManager.prototype.jumpToLine = function (line) {
    var pos = CodeMirror.Pos(Math.max(0, line - 1), 0);
    this.cm.setCursor(pos);
    this.cm.scrollIntoView(pos, 80);
    this.cm.focus();
  };

  EditorManager.prototype.markSaved = function (doc, info) {
    if (!doc) return;
    if (info && info.name) doc.name = info.name;
    if (info && info.path) doc.path = info.path;
    if (doc.kind === "blocks") doc.dirtyBlocks = false;
    else doc.savedContent = doc.cmDoc.getValue();
    if (this.options.onDocs) this.options.onDocs();
  };

  EditorManager.prototype.isDirty = function (doc) {
    if (!doc) return false;
    if (doc.kind === "blocks") return !!doc.dirtyBlocks;
    return doc.cmDoc.getValue() !== doc.savedContent;
  };

  EditorManager.prototype._changed = function () {
    var doc = this.active();
    if (!doc) return;
    this._scrollTops.set(doc.id, this.cm.getScrollInfo().top);
    if (this.options.onChange) this.options.onChange(doc);
    if (this.options.onDocs) this.options.onDocs();
  };

  // -- linting --------------------------------------------------------
  EditorManager.prototype._getAnnotations = function (text) {
    var cached = this._lintCache.get(text);
    if (cached) return cached;
    if (text.trim().length) {
      var self = this;
      clearTimeout(this._lintTimer);
      this._lintTimer = setTimeout(function () { self._fetchAnnotations(text); }, 420);
    }
    return [];
  };

  EditorManager.prototype._fetchAnnotations = function (text) {
    if (!this.options.lintEnabled || !this.options.lintEnabled()) return;
    var self = this;
    var request = ++this._lintRequest;
    fetch("/api/lint", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Hyena-Client": "web" },
      body: JSON.stringify({ code: text })
    }).then(function (response) {
      return response.json();
    }).then(function (data) {
      if (request !== self._lintRequest || !data || !data.diagnostics) return;
      var annotations = data.diagnostics.map(function (item) {
        return makeAnnotation(item, self.cm);
      });
      if (self._lintCache.size > 30) {
        self._lintCache.delete(self._lintCache.keys().next().value);
      }
      self._lintCache.set(text, annotations);
      if (self.cm.getValue() === text) {
        self.cm.performLint();
        if (self.options.onDiagnostics) self.options.onDiagnostics(data.diagnostics);
      }
    }).catch(function () { /* a lint failure must never disturb editing */ });
  };

  window.HyenaEditor = {
    EditorManager: EditorManager,
    hyenaHint: hyenaHint
  };
})();
