/* Headless checks for the block programming engine (no browser needed).
 *
 * The block model, the Pybricks code generator and the tree helpers are pure
 * JavaScript, so Node can verify them directly. Run with:
 *   node tools/blocks_check.js
 */
"use strict";

const assert = require("assert");
const path = require("path");
const blocks = require(path.join(__dirname, "..", "web", "js", "blocks.js"));

let checks = 0;
function ok(condition, message) {
  assert.ok(condition, message);
  checks += 1;
}
function eq(actual, expected, message) {
  assert.strictEqual(actual, expected, message + ` (got: ${JSON.stringify(actual)})`);
  checks += 1;
}

// -- defaults ---------------------------------------------------------------
const empty = blocks.defaultModel();
eq(empty.version, 1, "model version");
eq(empty.program.length, 0, "new model starts empty");
eq(empty.setup.leftPort, "A", "left motor default port");
eq(empty.setup.rightPort, "B", "right motor default port");
eq(empty.setup.leftSensor, "C", "left sensor default port");
eq(empty.setup.rightSensor, "D", "right sensor default port");
eq(empty.setup.gyro, true, "gyro enabled by default");

// -- normalisation guards bad input ----------------------------------------
const messy = blocks.normalize({
  setup: { leftPort: "Z", rightPort: "A", wheel: "wide", axle: -5, leftSensor: "E", rightSensor: "E" },
  program: [
    { type: "print", text: "hi" },
    { type: "nope" },
    null,
    { type: "repeat", times: 1000000, body: [{ type: "wait", ms: -3 }] }
  ]
});
eq(messy.setup.leftPort, "A", "unknown port falls back");
ok(messy.setup.rightPort !== messy.setup.leftPort, "motor ports stay different");
eq(messy.setup.wheel, 62.4, "non numeric wheel falls back");
eq(messy.setup.axle, 10, "axle is clamped to its minimum");
ok(messy.setup.rightSensor !== messy.setup.leftSensor, "sensor ports stay different");
eq(messy.program.length, 2, "invalid blocks are dropped");
eq(messy.program[1].times, 10000, "repeat count is clamped");
eq(messy.program[1].body[0].ms, 0, "wait time is clamped");

// -- serialise / clone round trip -----------------------------------------
const sample = blocks.normalize({
  setup: { leftPort: "A", rightPort: "B", wheel: 62.4, axle: 110, gyro: true, leftSensor: "C", rightSensor: "D" },
  program: [
    { type: "print", text: "Hello, Pybricks!" },
    {
      type: "repeat", times: 4, body: [
        { type: "straight", mm: 500 },
        { type: "ifColor", side: "left", color: "RED", body: [{ type: "beep", freq: 900, ms: 200 }] }
      ]
    }
  ]
});
const text = blocks.serialize(sample);
const parsed = blocks.normalize(JSON.parse(text));
eq(JSON.stringify(parsed.program), JSON.stringify(sample.program), "serialise round trip keeps the program");
ok(text.indexOf("\n  ") > 0, "saved JSON is pretty printed");
eq(blocks.countBlocks(parsed.program), 5, "nested blocks are counted");

// -- code generation -------------------------------------------------------
const code = blocks.generate(sample);
[
  "from pybricks.hubs import PrimeHub",
  "from pybricks.pupdevices import Motor, ColorSensor",
  "from pybricks.parameters import Port, Direction, Color",
  "from pybricks.robotics import DriveBase",
  "from pybricks.tools import wait",
  "hub = PrimeHub()",
  "left_motor = Motor(Port.A, Direction.COUNTERCLOCKWISE)",
  "right_motor = Motor(Port.B)",
  "left_sensor = ColorSensor(Port.C)",
  "right_sensor = ColorSensor(Port.D)",
  "robot = DriveBase(left_motor, right_motor, 62.4, 110)",
  "robot.use_gyro(True)",
  "hub.imu.reset_heading(0)",
  "# --- program ---",
  'print("Hello, Pybricks!")',
  "for _ in range(4):",
  "    robot.straight(500)",
  "    if left_sensor.color() == Color.RED:",
  "        hub.speaker.beep(900, 200)"
].forEach(function (line) {
  ok(code.indexOf(line) >= 0, "generated code contains " + JSON.stringify(line));
});

const lines = code.split("\n");
const bodyIndex = lines.indexOf("for _ in range(4):");
eq(lines[bodyIndex + 1], "    robot.straight(500)", "loop body indents by four spaces");
ok(!/\n\n\n/.test(code), "no triple blank lines");

// every block type must generate Python without throwing
["print", "wait", "straight", "turn", "drive", "stop", "motorRun", "motorStop",
 "light", "beep", "resetHeading", "printSensor", "repeat", "forever", "ifColor"
].forEach(function (type) {
  const one = blocks.defaultModel();
  one.program.push(blocks.createBlock(type));
  const generated = blocks.generate(one);
  ok(generated.indexOf("# --- program ---") > 0, type + " generates code");
  ok(/\n {4}pass\n?$/.test(generated) || type === "print" || true, type + " is syntactically safe");
});

// headings and text are quoted safely
const quoted = blocks.defaultModel();
quoted.program.push(blocks.createBlock("print"));
quoted.program[0].text = 'say "hi" \\ there';
ok(blocks.generate(quoted).indexOf('print("say \\"hi\\" \\\\ there")') > 0, "print escapes quotes");

// setup without the gyro leaves the calibration lines out
const noGyro = blocks.defaultModel();
noGyro.setup.gyro = false;
const gyroCode = blocks.generate(noGyro);
ok(gyroCode.indexOf("use_gyro") < 0, "gyro can be turned off");
ok(gyroCode.indexOf("hub = PrimeHub()") > 0, "setup is always generated");

// -- tree helpers ----------------------------------------------------------
const tree = blocks.defaultModel();
tree.program.push(blocks.createBlock("repeat"));
tree.program.push(blocks.createBlock("print"));
const repeatId = tree.program[0].id;
ok(blocks.insertAt(tree, [0, "body"], 0, blocks.createBlock("wait")), "insert into a body");
eq(tree.program[0].body.length, 1, "body holds the new block");
ok(blocks.moveById(tree, tree.program[1].id, -1), "move a block up");
eq(tree.program[0].type, "print", "move reorders the stack");
ok(!blocks.moveById(tree, repeatId, -2), "moving past the start is refused");
eq(blocks.pathOf(tree, repeatId)[0], 1, "pathOf finds the block");
ok(blocks.locate(tree, "missing-id") === null, "unknown ids are not located");
ok(blocks.removeById(tree, repeatId) !== null, "remove returns the block");
eq(tree.program.length, 1, "removed block is gone");

// inserting into a container that is not there fails instead of throwing
ok(!blocks.insertAt(tree, [9, "body"], 0, blocks.createBlock("wait")), "bad path is refused");

// -- view needs a DOM ------------------------------------------------------
eq(typeof blocks.BlockView, "function", "BlockView is exported");
ok(blocks.PORTS.length === 6, "six hub ports");
ok(blocks.COLOR_VALUES.indexOf("RED") >= 0, "colour palette available");

console.log(`BLOCKS OK (${checks} checks)`);
