"""Static consistency checks for the browser front-end (no browser needed).

These catch the most common integration mistakes between the HTML, the JS and
the translation tables before anyone opens the app.
"""
import json
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


class TranslationTests(unittest.TestCase):
    def setUp(self):
        source = read("js/i18n.js")
        # Split the two language blocks so missing keys per language are visible.
        ar_start = source.index("    ar: {")
        en_start = source.index("    en: {")
        ar_block = source[ar_start:en_start]
        en_block = source[en_start:]
        self.ar_keys = set(re.findall(r'"([a-z][\w.\-]*)":', ar_block))
        self.en_keys = set(re.findall(r'"([a-z][\w.\-]*)":', en_block))
        self.source = source

    def test_both_languages_have_the_same_keys(self):
        self.assertEqual(sorted(self.ar_keys - self.en_keys), [])
        self.assertEqual(sorted(self.en_keys - self.ar_keys), [])

    def test_html_keys_exist(self):
        html = read("index.html")
        used = set(re.findall(r'data-i18n(?:-title|-ph)?="([\w.\-]+)"', html))
        missing = sorted(used - self.ar_keys)
        self.assertEqual(missing, [], f"missing translation keys: {missing}")

    def test_js_keys_exist(self):
        js = read("js/app.js")
        used = set(re.findall(r'\bt\(\s*"([\w.\-]+)"', js))
        used |= set(re.findall(r'I18N\.t\(\s*"([\w.\-]+)"', js))
        missing = sorted(key for key in used - self.ar_keys if not key.endswith("."))
        self.assertEqual(missing, [], f"missing translation keys: {missing}")

    def test_placeholder_params_match_between_languages(self):
        def params(key):
            pattern = re.compile(r'"' + re.escape(key) + r'":\s*"((?:[^"\\]|\\.)*)"')
            groups = {}
            for lang in ("ar", "en"):
                start = self.source.index("    %s: {" % lang)
                end = len(self.source) if lang == "en" else self.source.index("    en: {")
                block = self.source[start:end]
                match = pattern.search(block)
                groups[lang] = set(re.findall(r"\{(\w+)\}", match.group(1))) if match else set()
            return groups

        for key in sorted(self.ar_keys & self.en_keys):
            found = params(key)
            self.assertEqual(found["ar"], found["en"], f"placeholder mismatch in {key}")


class MarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")

    def test_every_icon_reference_has_a_symbol(self):
        symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', self.html))
        used = set(re.findall(r'href="#i-([\w-]+)"', self.html))
        used |= set(re.findall(r'svgIcon\("([\w-]+)"', read("js/app.js")))
        self.assertEqual(sorted(used - symbols), [], "missing sprite symbols")

    def test_ids_used_by_the_app_exist(self):
        js = read("js/app.js")
        used = set(re.findall(r'\$\("#([\w-]+)"', js))
        present = set(re.findall(r'\bid="([\w-]+)"', self.html))
        missing = sorted(used - present)
        self.assertEqual(missing, [], f"missing element ids: {missing}")

    def test_scripts_and_stylesheets_exist(self):
        for src in re.findall(r'<script src="([^"]+)"', self.html):
            self.assertTrue((WEB / src.lstrip("/")).is_file(), src)
        for href in re.findall(r'<link[^>]+href="([^"]+)"', self.html):
            if href.startswith("/"):
                self.assertTrue((WEB / href.lstrip("/")).is_file(), href)

    def test_all_api_paths_are_implemented(self):
        js = read("js/app.js")
        server = (ROOT / "spike_server.py").read_text(encoding="utf-8")
        used = set(re.findall(r'"(/api/[\w/]+)"', js))
        missing = sorted(path for path in used if path not in server)
        self.assertEqual(missing, [], f"endpoints not implemented: {missing}")

    def test_no_leftover_mojibake(self):
        for name in ("index.html", "js/app.js", "js/i18n.js", "css/app.css"):
            text = read(name)
            self.assertNotIn("\ufffd", text, name)


if __name__ == "__main__":
    unittest.main()
