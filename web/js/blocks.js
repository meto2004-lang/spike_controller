/* Pybricks style block programming for Hyena Control.
 *
 * The picture this is modelled on has two yellow "hat" blocks (set up and
 * program) and blue statement blocks such as `print "Hello, Pybricks!"`.
 * Blocks are kept as a small JSON model so they can be saved, generated into
 * Pybricks Python and tested without a browser.
 *
 * The model is intentionally simple:
 *   {version, setup:{...}, program:[{id,type, ...fields, body?:[...]}]}
 */
(function (global) {
  "use strict";

  var PORTS = ["A", "B", "C", "D", "E", "F"];
  var COLOR_VALUES = ["GREEN", "RED", "BLUE", "YELLOW", "WHITE", "MAGENTA", "ORANGE", "AZURE", "VIOLET", "BLACK"];
  var COLOR_LABELS = {
    ar: {
      GREEN: "أخضر", RED: "أحمر", BLUE: "أزرق", YELLOW: "أصفر", WHITE: "أبيض",
      MAGENTA: "أرجواني", ORANGE: "برتقالي", AZURE: "سماوي", VIOLET: "بنفسجي", BLACK: "أسود"
    },
    en: {
      GREEN: "green", RED: "red", BLUE: "blue", YELLOW: "yellow", WHITE: "white",
      MAGENTA: "magenta", ORANGE: "orange", AZURE: "azure", VIOLET: "violet", BLACK: "black"
    }
  };
  var SIDE_LABELS = { ar: { left: "الأيسر", right: "الأيمن" }, en: { left: "left", right: "right" } };
  var READ_LABELS = { ar: { reflection: "الانعكاس", color: "اللون" }, en: { reflection: "reflection", color: "colour" } };

  var DEFAULT_SETUP = {
    leftPort: "A", rightPort: "B", wheel: 62.4, axle: 110,
    gyro: true, leftSensor: "C", rightSensor: "D"
  };

  // Every block type: tone (colour family), label key, fields and whether it
  // owns a nested stack of statements.
  var DEFS = {
    print: {
      tone: "statement", label: "blocks.print", icon: "terminal",
      fields: [{ name: "text", kind: "text", "default": "Hello, Pybricks!", width: 20 }]
    },
    wait: {
      tone: "statement", label: "blocks.wait", icon: "clock",
      fields: [{ name: "ms", kind: "number", "default": 500, min: 0, max: 60000, step: 50, unit: "ms" }]
    },
    straight: {
      tone: "statement", label: "blocks.straight", icon: "straight",
      fields: [{ name: "mm", kind: "number", "default": 500, min: -10000, max: 10000, step: 10, unit: "mm" }]
    },
    turn: {
      tone: "statement", label: "blocks.turn", icon: "turn",
      fields: [{ name: "deg", kind: "number", "default": 90, min: -3600, max: 3600, step: 5, unit: "°" }]
    },
    drive: {
      tone: "statement", label: "blocks.drive", icon: "drive",
      fields: [
        { name: "speed", kind: "number", "default": 300, min: -1000, max: 1000, step: 25, unit: "mm/s" },
        { name: "turnRate", kind: "number", "default": 0, min: -1000, max: 1000, step: 25, unit: "°/s" }
      ]
    },
    stop: { tone: "statement", label: "blocks.stop", icon: "stop", fields: [] },
    motorRun: {
      tone: "motor", label: "blocks.motorRun", icon: "motor",
      fields: [
        { name: "side", kind: "select", options: "sides", "default": "left" },
        { name: "speed", kind: "number", "default": 500, min: -1000, max: 1000, step: 25, unit: "°/s" }
      ]
    },
    motorStop: {
      tone: "motor", label: "blocks.motorStop", icon: "stop",
      fields: [{ name: "side", kind: "select", options: "sides", "default": "left" }]
    },
    light: {
      tone: "lights", label: "blocks.light", icon: "light",
      fields: [{ name: "color", kind: "select", options: "colors", "default": "GREEN" }]
    },
    beep: {
      tone: "lights", label: "blocks.beep", icon: "speaker",
      fields: [
        { name: "freq", kind: "number", "default": 900, min: 64, max: 20000, step: 50, unit: "Hz" },
        { name: "ms", kind: "number", "default": 200, min: 10, max: 5000, step: 50, unit: "ms" }
      ]
    },
    resetHeading: { tone: "lights", label: "blocks.resetHeading", icon: "compass", fields: [] },
    printSensor: {
      tone: "sensor", label: "blocks.printSensor", icon: "sensor",
      fields: [
        { name: "side", kind: "select", options: "sides", "default": "left" },
        { name: "read", kind: "select", options: "reads", "default": "reflection" }
      ]
    },
    repeat: {
      tone: "control", label: "blocks.repeat", icon: "repeat", container: true,
      fields: [{ name: "times", kind: "number", "default": 4, min: 0, max: 10000, step: 1, unit: "×" }]
    },
    forever: { tone: "control", label: "blocks.forever", icon: "forever", container: true, fields: [] },
    ifColor: {
      tone: "control", label: "blocks.ifColor", icon: "condition", container: true,
      fields: [
        { name: "side", kind: "select", options: "sides", "default": "left" },
        { name: "color", kind: "select", options: "colors", "default": "RED" }
      ]
    }
  };

  var GROUPS = [
    { id: "program", types: ["print", "printSensor", "wait"] },
    { id: "flow", types: ["repeat", "forever", "ifColor"] },
    { id: "move", types: ["straight", "turn", "drive", "stop"] },
    { id: "motors", types: ["motorRun", "motorStop"] },
    { id: "lights", types: ["light", "beep", "resetHeading"] }
  ];

  var INDENT = "    ";
  var MAX_DEPTH = 5;
  var MAX_BLOCKS = 400;
  var nextId = 1;

  function freshId() {
    return "b" + (nextId++).toString(36) + Math.floor(Math.random() * 1e4).toString(36);
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function clampNumber(value, fallback, min, max) {
    var number = Number(value);
    if (!isFinite(number)) number = fallback;
    number = Math.max(min, Math.min(max, number));
    return Math.round(number * 10000) / 10000;
  }

  function formatNumber(value, fallback) {
    var number = Number(value);
    if (!isFinite(number)) number = fallback;
    return String(Math.round(number * 10000) / 10000);
  }

  function pyString(text) {
    return JSON.stringify(String(text === undefined || text === null ? "" : text));
  }

  function pick(value, allowed, fallback) {
    return allowed.indexOf(value) >= 0 ? value : fallback;
  }

  // -- model -----------------------------------------------------------
  function normalizeSetup(raw) {
    var setup = isObject(raw) ? raw : {};
    var left = pick(setup.leftPort, PORTS, DEFAULT_SETUP.leftPort);
    var right = pick(setup.rightPort, PORTS, DEFAULT_SETUP.rightPort);
    var leftSensor = pick(setup.leftSensor, PORTS, DEFAULT_SETUP.leftSensor);
    var rightSensor = pick(setup.rightSensor, PORTS, DEFAULT_SETUP.rightSensor);
    // DriveBase needs two different motor ports and sensors need their own ports.
    if (right === left) right = left === "B" ? "A" : "B";
    if (rightSensor === leftSensor) rightSensor = leftSensor === "D" ? "C" : "D";
    return {
      leftPort: left,
      rightPort: right,
      wheel: clampNumber(setup.wheel, DEFAULT_SETUP.wheel, 10, 300),
      axle: clampNumber(setup.axle, DEFAULT_SETUP.axle, 10, 500),
      gyro: setup.gyro === undefined ? DEFAULT_SETUP.gyro : !!setup.gyro,
      leftSensor: leftSensor,
      rightSensor: rightSensor
    };
  }

  function normalizeBlock(raw, depth, budget) {
    if (!isObject(raw) || typeof raw.type !== "string" || !DEFS[raw.type]) return null;
    var def = DEFS[raw.type];
    var block = { id: typeof raw.id === "string" && raw.id ? raw.id.slice(0, 40) : freshId(), type: raw.type };
    def.fields.forEach(function (field) {
      var value = raw[field.name];
      if (field.kind === "number") {
        block[field.name] = clampNumber(value, field["default"], field.min, field.max);
      } else if (field.kind === "select") {
        var allowed = field.options === "sides" ? ["left", "right"]
          : field.options === "reads" ? ["reflection", "color"] : COLOR_VALUES;
        block[field.name] = pick(value, allowed, field["default"]);
      } else {
        block[field.name] = String(value === undefined || value === null ? field["default"] : value).slice(0, 200);
      }
    });
    if (def.container) {
      block.body = normalizeList(raw.body, depth + 1, budget);
    }
    return block;
  }

  function normalizeList(raw, depth, budget) {
    if (!Array.isArray(raw) || depth > MAX_DEPTH) return [];
    var blocks = [];
    for (var index = 0; index < raw.length; index += 1) {
      if (budget.count >= MAX_BLOCKS) break;
      var block = normalizeBlock(raw[index], depth, budget);
      if (block) {
        budget.count += 1;
        blocks.push(block);
      }
    }
    return blocks;
  }

  function normalize(raw) {
    var source = isObject(raw) ? raw : {};
    return {
      version: 1,
      setup: normalizeSetup(source.setup),
      program: normalizeList(source.program, 0, { count: 0 })
    };
  }

  function createBlock(type) {
    if (!DEFS[type]) throw new Error("unknown block type: " + type);
    return normalizeBlock({ type: type }, 0, { count: 0 });
  }

  function serialize(model) {
    return JSON.stringify(normalize(model), null, 2);
  }

  function clone(model) {
    return normalize(JSON.parse(JSON.stringify(model)));
  }

  function defaultModel() {
    return normalize({ setup: DEFAULT_SETUP, program: [] });
  }

  // -- locations -------------------------------------------------------
  function locateIn(list, id, parentPath) {
    for (var index = 0; index < list.length; index += 1) {
      var block = list[index];
      if (block.id === id) return { list: list, index: index, path: parentPath };
      if (block.body) {
        var found = locateIn(block.body, id, parentPath.concat([index, "body"]));
        if (found) return found;
      }
    }
    return null;
  }

  function locate(model, id) {
    return locateIn(model.program, id, []);
  }

  function containerAt(model, path) {
    var list = model.program;
    for (var index = 0; index < path.length; index += 2) {
      var block = list[path[index]];
      if (!block) return null;
      if (path[index + 1] !== "body" || !Array.isArray(block.body)) return null;
      list = block.body;
    }
    return list;
  }

  function insertAt(model, path, index, block) {
    var list = containerAt(model, path);
    if (!list) return false;
    var at = Math.max(0, Math.min(list.length, index));
    list.splice(at, 0, block);
    return true;
  }

  function removeById(model, id) {
    var found = locate(model, id);
    if (!found) return null;
    return found.list.splice(found.index, 1)[0];
  }

  function moveById(model, id, delta) {
    var found = locate(model, id);
    if (!found) return false;
    var target = found.index + delta;
    if (target < 0 || target >= found.list.length) return false;
    var block = found.list.splice(found.index, 1)[0];
    found.list.splice(target, 0, block);
    return true;
  }

  function pathOf(model, id) {
    var found = locate(model, id);
    return found ? found.path.concat([found.index]) : null;
  }

  function isDescendantPath(ancestorPath, candidatePath) {
    if (!ancestorPath) return false;
    if (candidatePath.length < ancestorPath.length) return false;
    for (var index = 0; index < ancestorPath.length; index += 1) {
      if (ancestorPath[index] !== candidatePath[index]) return false;
    }
    return true;
  }

  function countBlocks(list) {
    var total = 0;
    (list || []).forEach(function (block) {
      total += 1;
      if (block.body) total += countBlocks(block.body);
    });
    return total;
  }

  // -- code generation -------------------------------------------------
  function sensorName(side) {
    return side === "right" ? "right_sensor" : "left_sensor";
  }

  function motorName(side) {
    return side === "right" ? "right_motor" : "left_motor";
  }

  function generateSetup(setup) {
    var s = normalizeSetup(setup);
    var lines = [
      "from pybricks.hubs import PrimeHub",
      "from pybricks.pupdevices import Motor, ColorSensor",
      "from pybricks.parameters import Port, Direction, Color",
      "from pybricks.robotics import DriveBase",
      "from pybricks.tools import wait",
      "",
      "hub = PrimeHub()",
      "left_motor = Motor(Port." + s.leftPort + ", Direction.COUNTERCLOCKWISE)",
      "right_motor = Motor(Port." + s.rightPort + ")",
      "left_sensor = ColorSensor(Port." + s.leftSensor + ")",
      "right_sensor = ColorSensor(Port." + s.rightSensor + ")",
      "robot = DriveBase(left_motor, right_motor, " + formatNumber(s.wheel, 62.4) + ", " +
        formatNumber(s.axle, 110) + ")"
    ];
    if (s.gyro) {
      lines.push("robot.use_gyro(True)");
      lines.push("hub.imu.reset_heading(0)");
    }
    return lines.join("\n");
  }

  function generateStatements(list, depth) {
    var pad = new Array(depth + 1).join(INDENT);
    if (!list || !list.length) return pad + "pass";
    return list.map(function (block) { return generateBlock(block, depth); }).join("\n");
  }

  function generateBlock(raw, depth) {
    var pad = new Array(depth + 1).join(INDENT);
    if (!isObject(raw) || !DEFS[raw.type]) return pad + "pass";
    var def = DEFS[raw.type];
    var block = normalizeBlock(raw, depth, { count: 0 }) || {};
    switch (raw.type) {
      case "print":
        return pad + "print(" + pyString(block.text) + ")";
      case "wait":
        return pad + "wait(" + formatNumber(block.ms, 0) + ")";
      case "straight":
        return pad + "robot.straight(" + formatNumber(block.mm, 0) + ")";
      case "turn":
        return pad + "robot.turn(" + formatNumber(block.deg, 0) + ")";
      case "drive":
        return pad + "robot.drive(" + formatNumber(block.speed, 0) + ", " +
          formatNumber(block.turnRate, 0) + ")";
      case "stop":
        return pad + "robot.stop()";
      case "motorRun":
        return pad + motorName(block.side) + ".run(" + formatNumber(block.speed, 0) + ")";
      case "motorStop":
        return pad + motorName(block.side) + ".stop()";
      case "light":
        return pad + "hub.light.on(Color." + block.color + ")";
      case "beep":
        return pad + "hub.speaker.beep(" + formatNumber(block.freq, 900) + ", " +
          formatNumber(block.ms, 200) + ")";
      case "resetHeading":
        return pad + "hub.imu.reset_heading(0)";
      case "printSensor":
        return pad + "print(" + sensorName(block.side) + "." +
          (block.read === "color" ? "color()" : "reflection()") + ")";
      case "repeat":
        return pad + "for _ in range(" + formatNumber(block.times, 0) + "):\n" +
          generateStatements(block.body, depth + 1);
      case "forever":
        return pad + "while True:\n" + generateStatements(block.body, depth + 1);
      case "ifColor":
        return pad + "if " + sensorName(block.side) + ".color() == Color." + block.color + ":\n" +
          generateStatements(block.body, depth + 1);
      default:
        return pad + "pass";
    }
  }

  function generate(raw) {
    var model = normalize(raw);
    return generateSetup(model.setup) + "\n\n# --- program ---\n" +
      generateStatements(model.program, 0) + "\n";
  }

  // -- view ------------------------------------------------------------
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

  var ICONS = {
    terminal: '<path d="M4 6h16v12H4z"/><path d="m7 10 2 2-2 2M12 14h4"/>',
    clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/>',
    straight: '<path d="M12 4v16M8 16l4 4 4-4M8 8l4-4 4 4"/>',
    turn: '<path d="M5 17a7 7 0 0 1 7-7h6M14 6l4 4-4 4"/>',
    drive: '<path d="M4 16h16M7 16l2-6M17 16l-2-6M9 10h6"/><circle cx="8" cy="18" r="1.6"/><circle cx="16" cy="18" r="1.6"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
    motor: '<rect x="6" y="8" width="12" height="8" rx="2"/><path d="M12 8V5M9 20l3-4 3 4"/>',
    light: '<path d="M9 17h6M10 20h4M12 3a6 6 0 0 1 3.5 10.9V17h-7v-3.1A6 6 0 0 1 12 3z"/>',
    speaker: '<path d="M5 9v6h4l5 4V5L9 9z"/><path d="M17 9a4 4 0 0 1 0 6"/>',
    compass: '<circle cx="12" cy="12" r="8"/><path d="m15 9-2 4-4 2 2-4z"/>',
    sensor: '<rect x="5" y="9" width="14" height="6" rx="2"/><path d="M9 9V6M15 9V6M9 18h6"/>',
    repeat: '<path d="M5 12a7 7 0 0 1 7-7h5M17 5l2 2-2 2M19 12a7 7 0 0 1-7 7H7M7 19l-2-2 2-2"/>',
    forever: '<path d="M7 12a5 5 0 1 1 5 5H8"/><path d="m10 14-2 3 3 2"/>',
    condition: '<path d="M12 4 4 9l8 5 8-5z"/><path d="M4 14l8 5 8-5"/>',
    monitor: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M9 21h6M12 17v4"/>',
    play: '<path d="M8 5.5 18 12 8 18.5z"/>',
    trash: '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>',
    up: '<path d="m6 14 6-6 6 6"/>',
    down: '<path d="m6 10 6 6 6-6"/>'
  };

  function blockIcon(name, size) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "blk-ico");
    if (size) svg.setAttribute("style", "width:" + size + "px;height:" + size + "px");
    svg.innerHTML = '<g fill="none" stroke="currentColor" stroke-width="1.9" ' +
      'stroke-linecap="round" stroke-linejoin="round">' + (ICONS[name] || "") + "</g>";
    return svg;
  }

  function BlockView(host, options) {
    this.host = host;
    this.options = options || {};
    this.lang = this.options.lang || "ar";
    this.model = normalize(this.options.model || defaultModel());
    this.t = this.options.t || function (key) { return key; };
    this.drag = null;
    this._onPointerMove = this._pointerMove.bind(this);
    this._onPointerUp = this._pointerUp.bind(this);
    this.render();
  }

  BlockView.prototype.setLanguage = function (lang) {
    this.lang = lang;
    this.render();
  };

  BlockView.prototype.setTheme = function () { /* colours are CSS variables */ };

  BlockView.prototype.getModel = function () {
    return normalize(this.model);
  };

  BlockView.prototype.destroy = function () {
    document.removeEventListener("pointermove", this._onPointerMove);
    document.removeEventListener("pointerup", this._onPointerUp);
    this.host.innerHTML = "";
  };

  BlockView.prototype._changed = function (structural) {
    if (structural) this.render();
    if (this.options.onChange) this.options.onChange(this.model);
  };

  BlockView.prototype._label = function (type) {
    return this.t(DEFS[type].label);
  };

  BlockView.prototype._optionLabel = function (field, value) {
    if (field.options === "sides") return SIDE_LABELS[this.lang][value] || value;
    if (field.options === "reads") return READ_LABELS[this.lang][value] || value;
    return (COLOR_LABELS[this.lang] || COLOR_LABELS.en)[value] || value;
  };

  // -- palette ---------------------------------------------------------
  BlockView.prototype._renderPalette = function () {
    var self = this;
    var palette = el("aside", { class: "blk-palette", "aria-label": this.t("blocks.palette") });
    var scroll = el("div", { class: "blk-palette-scroll" });
    GROUPS.forEach(function (group) {
      var section = el("section", { class: "blk-group" }, [
        el("h5", { text: self.t("blocks.group." + group.id) })
      ]);
      group.types.forEach(function (type) {
        var def = DEFS[type];
        var item = el("button", {
          type: "button",
          class: "blk-palette-item tone-" + def.tone,
          dataset: { type: type },
          title: self.t("blocks.addHint")
        }, [
          def.icon ? blockIcon(def.icon) : null,
          el("span", { class: "blk-palette-label", text: self._label(type) }),
          el("span", { class: "blk-palette-plus", text: "+" })
        ]);
        item.addEventListener("pointerdown", function (event) {
          self._startDrag(event, type, null);
        });
        item.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            self._addToProgram(type);
          }
        });
        section.appendChild(item);
      });
      scroll.appendChild(section);
    });
    palette.appendChild(scroll);
    palette.appendChild(el("div", { class: "blk-palette-hint", text: this.t("blocks.dragHint") }));
    return palette;
  };

  BlockView.prototype._addToProgram = function (type) {
    this.model.program.push(createBlock(type));
    this._changed(true);
  };

  // -- canvas ----------------------------------------------------------
  BlockView.prototype.render = function () {
    this.host.innerHTML = "";
    var root = el("div", { class: "blk-root" }, [this._renderPalette()]);
    var canvas = el("div", { class: "blk-canvas" });

    canvas.appendChild(this._renderSetup());
    canvas.appendChild(this._renderProgram());

    if (!this.model.program.length) {
      canvas.appendChild(el("div", { class: "blk-hint", text: this.t("blocks.empty") }));
    }
    root.appendChild(canvas);
    this.host.appendChild(root);
    this.canvas = canvas;
  };

  BlockView.prototype._renderSetup = function () {
    var self = this;
    var setup = this.model.setup;
    var card = el("div", { class: "blk blk-hat blk-setup", dataset: { block: "setup" } });
    card.appendChild(el("div", { class: "blk-head" }, [
      blockIcon("monitor"),
      el("span", { class: "blk-title", text: this.t("blocks.setup") })
    ]));

    var grid = el("div", { class: "blk-setup-grid" });
    function portField(labelKey, key) {
      var select = el("select", { class: "blk-input", "aria-label": self.t(labelKey) });
      PORTS.forEach(function (port) {
        select.appendChild(el("option", { value: port, text: port }));
      });
      select.value = setup[key];
      select.addEventListener("change", function () {
        setup[key] = select.value;
        self.model.setup = normalizeSetup(setup);
        self._changed(true);
      });
      return el("label", { class: "blk-setup-field" }, [
        el("span", { text: self.t(labelKey) }),
        select
      ]);
    }
    function numberField(labelKey, key, unit, min, max, step) {
      var input = el("input", {
        class: "blk-input", type: "number", value: setup[key],
        min: min, max: max, step: step, dir: "ltr"
      });
      input.addEventListener("input", function () {
        setup[key] = input.value;
        if (input.value !== "") {
          setup[key] = clampNumber(input.value, DEFAULT_SETUP[key], min, max);
        }
        self._changed(false);
      });
      input.addEventListener("change", function () {
        setup[key] = clampNumber(input.value, DEFAULT_SETUP[key], min, max);
        input.value = setup[key];
        self._changed(false);
      });
      return el("label", { class: "blk-setup-field" }, [
        el("span", { text: self.t(labelKey) }),
        el("span", { class: "blk-unit-wrap" }, [input, el("i", { text: unit })])
      ]);
    }

    grid.appendChild(portField("blocks.leftMotor", "leftPort"));
    grid.appendChild(portField("blocks.rightMotor", "rightPort"));
    grid.appendChild(numberField("blocks.wheel", "wheel", "mm", 10, 300, 0.1));
    grid.appendChild(numberField("blocks.axle", "axle", "mm", 10, 500, 0.1));
    grid.appendChild(portField("blocks.leftSensor", "leftSensor"));
    grid.appendChild(portField("blocks.rightSensor", "rightSensor"));

    var gyro = el("input", { type: "checkbox" });
    gyro.checked = !!setup.gyro;
    gyro.addEventListener("change", function () {
      setup.gyro = gyro.checked;
      self._changed(true);
    });
    grid.appendChild(el("label", { class: "blk-setup-check" }, [gyro, el("span", { text: this.t("blocks.gyro") })]));

    card.appendChild(grid);
    return card;
  };

  BlockView.prototype._renderProgram = function () {
    var wrapper = el("div", { class: "blk-program" });
    var hat = el("div", { class: "blk blk-hat blk-program-hat" }, [
      el("div", { class: "blk-head" }, [
        blockIcon("play"),
        el("span", { class: "blk-title", text: this.t("blocks.program") })
      ])
    ]);
    wrapper.appendChild(hat);
    wrapper.appendChild(this._renderStack(this.model.program, [], "program"));
    return wrapper;
  };

  BlockView.prototype._renderStack = function (list, path, extraClass) {
    var self = this;
    var stack = el("div", {
      class: "blk-stack" + (extraClass ? " " + extraClass : ""),
      dataset: { stack: path.join("/") }
    });
    list.forEach(function (block, index) {
      stack.appendChild(self._renderBlock(block, path, index));
    });
    return stack;
  };

  BlockView.prototype._renderBlock = function (block, path, index) {
    var self = this;
    var def = DEFS[block.type];
    var selfPath = path.concat([index]);
    var node = el("div", {
      class: "blk blk-" + def.tone + (def.container ? " blk-container" : ""),
      dataset: { id: block.id, type: block.type }
    });

    var head = el("div", { class: "blk-head" });
    var grip = el("span", { class: "blk-grip", title: this.t("blocks.dragHint") });
    head.appendChild(grip);
    if (def.icon) head.appendChild(blockIcon(def.icon));
    head.appendChild(el("span", { class: "blk-title", text: this._label(block.type) }));

    (def.fields || []).forEach(function (field) {
      head.appendChild(self._renderField(block, field));
      if (field.unit && field.kind === "number") {
        head.appendChild(el("span", { class: "blk-unit", text: field.unit }));
      }
    });

    var actions = el("span", { class: "blk-actions" });
    function action(icon, labelKey, disabled, handler) {
      var button = el("button", {
        type: "button", class: "blk-action", title: self.t(labelKey),
        "aria-label": self.t(labelKey)
      }, [blockIcon(icon, 13)]);
      button.disabled = !!disabled;
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        handler();
      });
      return button;
    }
    var siblings = containerAt(this.model, path) || [];
    actions.appendChild(action("up", "blocks.moveUp", index === 0, function () {
      if (moveById(self.model, block.id, -1)) self._changed(true);
    }));
    actions.appendChild(action("down", "blocks.moveDown", index === siblings.length - 1, function () {
      if (moveById(self.model, block.id, 1)) self._changed(true);
    }));
    actions.appendChild(action("trash", "blocks.delete", false, function () {
      removeById(self.model, block.id);
      self._changed(true);
    }));
    head.appendChild(actions);
    node.appendChild(head);

    if (def.container) {
      var body = this._renderStack(block.body || [], selfPath.concat(["body"]), "blk-body");
      if (!block.body || !block.body.length) {
        body.appendChild(el("div", { class: "blk-empty", text: this.t("blocks.dropHere") }));
      }
      node.appendChild(body);
    }

    grip.addEventListener("pointerdown", function (event) {
      self._startDrag(event, block.type, block.id);
    });
    return node;
  };

  BlockView.prototype._renderField = function (block, field) {
    var self = this;
    if (field.kind === "select") {
      var select = el("select", { class: "blk-input blk-select", "aria-label": this.t(DEFS[block.type].label) });
      var values = field.options === "sides" ? ["left", "right"]
        : field.options === "reads" ? ["reflection", "color"] : COLOR_VALUES;
      values.forEach(function (value) {
        select.appendChild(el("option", { value: value, text: self._optionLabel(field, value) }));
      });
      select.value = block[field.name];
      select.addEventListener("change", function () {
        block[field.name] = select.value;
        self._changed(false);
      });
      select.addEventListener("pointerdown", function (event) { event.stopPropagation(); });
      return el("span", { class: "blk-slot" }, [select]);
    }
    var input = el("input", {
      class: "blk-input",
      type: field.kind === "number" ? "number" : "text",
      value: block[field.name],
      dir: field.kind === "number" ? "ltr" : "auto",
      spellcheck: "false"
    });
    if (field.kind === "number") {
      input.min = field.min;
      input.max = field.max;
      input.step = field.step;
    } else {
      input.style.width = Math.max(6, Math.min(30, field.width || 14)) + "ch";
    }
    input.addEventListener("pointerdown", function (event) { event.stopPropagation(); });
    input.addEventListener("input", function () {
      block[field.name] = field.kind === "number" && input.value === "" ? "" : input.value;
      self._changed(false);
    });
    input.addEventListener("change", function () {
      if (field.kind === "number") {
        block[field.name] = clampNumber(input.value, field["default"], field.min, field.max);
        input.value = block[field.name];
      }
      self._changed(false);
    });
    return el("span", { class: "blk-slot" }, [input]);
  };

  // -- dragging --------------------------------------------------------
  BlockView.prototype._startDrag = function (event, type, id) {
    event.preventDefault();
    if (this.drag) return;
    var self = this;
    var ghost = el("div", {
      class: "blk blk-ghost blk-" + DEFS[type].tone
    }, [el("span", { class: "blk-title", text: this._label(type) })]);
    document.body.appendChild(ghost);
    this.drag = {
      type: type,
      id: id,
      ghost: ghost,
      startX: event.clientX,
      startY: event.clientY,
      moved: false
    };
    ghost.style.left = event.clientX + 12 + "px";
    ghost.style.top = event.clientY - 14 + "px";
    document.addEventListener("pointermove", this._onPointerMove);
    document.addEventListener("pointerup", this._onPointerUp);
  };

  BlockView.prototype._pointerMove = function (event) {
    var drag = this.drag;
    if (!drag) return;
    if (!drag.moved && Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 4) {
      drag.moved = true;
    }
    drag.ghost.style.left = event.clientX + 12 + "px";
    drag.ghost.style.top = event.clientY - 14 + "px";
    this._highlight(event.clientX, event.clientY);
  };

  BlockView.prototype._dropTarget = function (x, y) {
    if (!this.canvas) return null;
    var element = document.elementFromPoint(x, y);
    var stack = element && element.closest ? element.closest("[data-stack]") : null;
    if (!stack || !this.canvas.contains(stack)) return null;
    var path = stack.dataset.stack ? stack.dataset.stack.split("/").filter(function (part) {
      return part !== "";
    }).map(function (part) {
      return part === "body" ? "body" : parseInt(part, 10);
    }) : [];
    var children = Array.prototype.slice.call(stack.children).filter(function (child) {
      return child.classList && child.classList.contains("blk");
    });
    var index = children.length;
    for (var position = 0; position < children.length; position += 1) {
      var rect = children[position].getBoundingClientRect();
      if (y < rect.top + rect.height / 2) {
        index = position;
        break;
      }
    }
    return { stack: stack, path: path, index: index };
  };

  BlockView.prototype._highlight = function (x, y) {
    var target = this._dropTarget(x, y);
    Array.prototype.forEach.call(this.host.querySelectorAll(".blk-stack.drop-active"), function (node) {
      node.classList.remove("drop-active");
    });
    if (target) target.stack.classList.add("drop-active");
  };

  BlockView.prototype._pointerUp = function (event) {
    var drag = this.drag;
    if (!drag) return;
    this.drag = null;
    document.removeEventListener("pointermove", this._onPointerMove);
    document.removeEventListener("pointerup", this._onPointerUp);
    if (drag.ghost) drag.ghost.remove();
    Array.prototype.forEach.call(this.host.querySelectorAll(".blk-stack.drop-active"), function (node) {
      node.classList.remove("drop-active");
    });
    var target = drag.moved ? this._dropTarget(event.clientX, event.clientY) : null;

    if (!drag.id) {
      // palette block: a plain click appends to the end of the program
      if (target && !this._isInsideDragged(null, target.path)) {
        this._insertNew(drag.type, target);
      } else if (!drag.moved) {
        this._addToProgram(drag.type);
      }
      return;
    }

    if (!target) return;
    var ownPath = pathOf(this.model, drag.id);
    if (this._isInsideDragged(ownPath, target.path)) return;
    // removeById returns the same object, so all field values survive the move.
    var block = removeById(this.model, drag.id);
    if (!block) return;
    if (!insertAt(this.model, target.path, target.index, block)) {
      insertAt(this.model, ownPath.slice(0, -1), ownPath[ownPath.length - 1], block);
    }
    this._changed(true);
  };

  BlockView.prototype._isInsideDragged = function (ownPath, targetPath) {
    if (!ownPath) return false;
    return isDescendantPath(ownPath.concat(["body"]), targetPath);
  };

  BlockView.prototype._insertNew = function (type, target) {
    var block = createBlock(type);
    if (!insertAt(this.model, target.path, target.index, block)) return;
    this._changed(true);
  };

  global.HyenaBlocks = {
    DEFS: DEFS,
    GROUPS: GROUPS,
    PORTS: PORTS,
    COLOR_VALUES: COLOR_VALUES,
    DEFAULT_SETUP: DEFAULT_SETUP,
    normalize: normalize,
    normalizeSetup: normalizeSetup,
    createBlock: createBlock,
    serialize: serialize,
    clone: clone,
    defaultModel: defaultModel,
    generate: generate,
    generateSetup: generateSetup,
    locate: locate,
    containerAt: containerAt,
    insertAt: insertAt,
    removeById: removeById,
    moveById: moveById,
    pathOf: pathOf,
    countBlocks: countBlocks,
    BlockView: BlockView
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = global.HyenaBlocks;
  }
})(typeof window !== "undefined" ? window : globalThis);
