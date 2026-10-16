"""Run regression tests for the historical audit findings.

The original audit probes asserted buggy behavior. This entry point now runs
checks that assert the corrected behavior, using the actual application modules.
GUI/hardware boundaries are mocked when testing without Tkinter or a robot.
"""
from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        # Keep failing tracebacks available in the Checks API as well as job logs.
        for test, detail in result.errors + result.failures:
            message = f'{test}: {detail}'
            message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print(f'::error::{message}')
    raise SystemExit(0 if result.wasSuccessful() else 1)
