"""Tests for the block programming mode (model, code generator, workspace).

The block engine itself is JavaScript, so the heavy lifting is delegated to
``tools/blocks_check.js`` when Node.js is available. The static checks here make
sure the browser bundle, the styles and the translations stay in sync even on
machines without Node.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
NODE = shutil.which("node")

sys.path.insert(0, str(ROOT))
from spike_workspace import Workspace, WorkspaceError  # noqa: E402


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def i18n_blocks():
    source = read("js/i18n.js")
    ar_start = source.index("    ar: {")
    en_start = source.index("    en: {")
    ar_block = source[ar_start:en_start]
    en_block = source[en_start:]
    ar_keys = set(re.findall(r'"([a-z][\w.\-]*)":', ar_block))
    en_keys = set(re.findall(r'"([a-z][\w.\-]*)":', en_block))
    return ar_keys, en_keys


class BlockAssetTests(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")
        self.blocks = read("js/blocks.js")
        self.app = read("js/app.js")

    def test_page_loads_the_block_engine(self):
        self.assertIn('href="/css/blocks.css"', self.html)
        self.assertIn('src="/js/blocks.js"', self.html)
        self.assertLess(self.html.index('src="/js/editor.js"'), self.html.index('src="/js/blocks.js"'))

    def test_block_hosts_exist(self):
        for element in ('id="blocks-host"', 'id="code-preview"', 'id="mode-switch"',
                        'id="btn-convert"', 'id="menu-new-blocks"'):
            self.assertIn(element, self.html, element)

    def test_hidden_hosts_are_really_hidden(self):
        css = read("css/blocks.css")
        self.assertIn(".blocks-host[hidden]", css)
        self.assertIn("#btn-convert[hidden]", css)
        self.assertIn("display: none !important", css)

    def test_app_wires_the_block_view(self):
        for symbol in ("HyenaBlocks.BlockView", "showActiveDoc", "newBlockDocument",
                       "convertBlocksToPython", "onActivate"):
            self.assertIn(symbol, self.app, symbol)

    def test_block_mode_is_offered_for_block_documents(self):
        # Block tabs must not be swapped into CodeMirror, and running a block
        # program uploads the generated Python under a .py name.
        self.assertIn('doc.kind === "blocks"', self.app)
        self.assertIn('doc.name.replace(/\\.blocks$/i, "") + ".py"', self.app)
        self.assertIn('window.HyenaBlocks.generate(doc.blocks)', self.app)

    def test_block_translation_keys_exist(self):
        ar_keys, en_keys = i18n_blocks()
        used = set(re.findall(r't\(\s*"(blocks\.[\w.]+)"', self.blocks))
        used |= set(re.findall(r'label:\s*"(blocks\.[\w.]+)"', self.blocks))
        used |= set(re.findall(r'field\("(blocks\.[\w.]+)"', self.blocks))
        used |= {
            "blocks.setup", "blocks.program", "blocks.palette", "blocks.dragHint",
            "blocks.empty", "blocks.dropHere", "blocks.addHint", "blocks.moveUp",
            "blocks.moveDown", "blocks.delete", "blocks.modeBlocks", "blocks.modePython",
            "blocks.convert", "blocks.new", "blocks.fileLabel", "blocks.invalidFile",
            "blocks.converted",
        }
        used |= {"blocks.group." + name for name in
                 ("program", "flow", "move", "motors", "lights")}
        used |= set(re.findall(r'"(blocks\.(?:group\.)?[\w.]+)"', self.html))
        used |= set(re.findall(r't\(\s*"(blocks\.[\w.]+)"', self.app))
        used = {key for key in used if not key.endswith(".")}
        missing_ar = sorted(key for key in used if key not in ar_keys)
        missing_en = sorted(key for key in used if key not in en_keys)
        self.assertEqual(missing_ar, [], f"missing Arabic block strings: {missing_ar}")
        self.assertEqual(missing_en, [], f"missing English block strings: {missing_en}")

    def test_every_block_type_has_a_palette_group(self):
        defs = re.findall(r'^\s{4}(\w+): \{\n\s+tone: "(\w+)"', self.blocks, re.M)
        self.assertTrue(defs, "block definitions not found")
        types = {name for name, _tone in defs}
        group_block = self.blocks[self.blocks.index("var GROUPS = ["):]
        grouped = set(re.findall(r'"(\w+)"', group_block.split("];")[0]))
        self.assertEqual(sorted(types - grouped), [], "block types missing from the palette")

    def test_block_files_are_clean_utf8(self):
        for name in ("js/blocks.js", "css/blocks.css"):
            text = read(name)
            self.assertNotIn("\ufffd", text, name)
            self.assertNotIn("\t", text.splitlines()[0], name)


class BlockWorkspaceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_block_files_can_be_saved_and_reopened(self):
        model = {"version": 1, "setup": {"leftPort": "A"}, "program": [{"type": "print", "text": "hi"}]}
        saved = self.workspace.write("demo.blocks", json.dumps(model))
        self.assertEqual(saved["name"], "demo.blocks")
        reopened = self.workspace.read("demo.blocks")
        self.assertEqual(json.loads(reopened["content"])["program"][0]["text"], "hi")
        self.assertEqual([item["name"] for item in self.workspace.list()], ["demo.blocks"])

    def test_block_sidecar_can_live_next_to_the_python_file(self):
        self.workspace.write("robot.py", "print('hi')\n")
        self.workspace.write("robot.blocks", '{"version": 1, "program": []}')
        names = sorted(item["name"] for item in self.workspace.list())
        self.assertEqual(names, ["robot.blocks", "robot.py"])

    def test_unknown_suffixes_are_still_refused(self):
        with self.assertRaises(WorkspaceError):
            self.workspace.write("robot.hyb", "{}")

    def test_block_names_stay_inside_the_workspace(self):
        with self.assertRaises(WorkspaceError):
            self.workspace.write("../evil.blocks", "{}")


@unittest.skipUnless(NODE, "Node.js is required to run the block engine checks")
class BlockEngineTests(unittest.TestCase):
    def test_engine_checks_pass(self):
        script = ROOT / "tools" / "blocks_check.js"
        self.assertTrue(script.is_file())
        result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("BLOCKS OK", result.stdout)

    def test_generated_program_is_valid_python(self):
        model = {
            "version": 1,
            "setup": {"leftPort": "A", "rightPort": "B", "wheel": 56, "axle": 112,
                      "gyro": True, "leftSensor": "C", "rightSensor": "D"},
            "program": [
                {"type": "print", "text": "start"},
                {"type": "repeat", "times": 3, "body": [
                    {"type": "straight", "mm": 400},
                    {"type": "ifColor", "side": "right", "color": "BLUE", "body": [
                        {"type": "beep", "freq": 700, "ms": 150}
                    ]}
                ]},
                {"type": "forever", "body": []}
            ]
        }
        script = (
            "const b = require(process.argv[1]);"
            "process.stdout.write(b.generate(JSON.parse(process.argv[2])));"
        )
        result = subprocess.run(
            [NODE, "-e", script, str(WEB / "js" / "blocks.js"), json.dumps(model)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        code = result.stdout
        compile(code, "<blocks>", "exec")
        self.assertIn("for _ in range(3):", code)
        self.assertIn("    robot.straight(400)", code)
        self.assertIn("        hub.speaker.beep(700, 150)", code)
        self.assertIn("right_sensor.color() == Color.BLUE", code)
        self.assertTrue(code.rstrip().endswith("pass"))


if __name__ == "__main__":
    unittest.main()
