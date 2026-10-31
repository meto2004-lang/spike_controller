"""Smoke tests for the official LEGO firmware helper module."""
from __future__ import annotations

import unittest

from spike_official import (
    _BehaviorSubject, _lwp_frame, _lwp_hub_property_request, _lwp_parse_hub_property,
    LEGO_HUB_SERVICE_UUID, LEGO_HUB_CHAR_INBOX, LEGO_HUB_CHAR_OUTBOX,
    PROP_FW_VERSION, PROP_BATTERY_VOLTAGE,
    is_official_firmware_usb,
)


class TestLWP(unittest.TestCase):
    def test_frame_prepends_length_le(self):
        payload = bytes([0x01, 0x02, 0x03])
        f = _lwp_frame(payload)
        self.assertEqual(f, b"\x03\x00\x01\x02\x03")

    def test_property_request_is_well_formed(self):
        data = _lwp_hub_property_request(PROP_FW_VERSION)
        # Payload is 3 bytes: type + prop + operation; length header is LE over payload length
        self.assertEqual(data[:2], bytes([3, 0]))
        self.assertEqual(data[2:], bytes([0x01, PROP_FW_VERSION, 0x05]))

    def test_parse_hub_property(self):
        # Hub property update: type=1, prop=FW_VERSION, op=0x06 update, payload 4B version
        msg = bytes([0x01, PROP_FW_VERSION, 0x06, 0x03, 0x02, 0x01, 0x00])
        prop, value = _lwp_parse_hub_property(msg)
        self.assertEqual(prop, PROP_FW_VERSION)
        self.assertEqual(value, bytes([0x03, 0x02, 0x01, 0x00]))

    def test_parse_rejects_other_types(self):
        self.assertIsNone(_lwp_parse_hub_property(bytes([0x02, 0x00])))
        self.assertIsNone(_lwp_parse_hub_property(bytes([0x01, 0x01, 0x01])))  # unknown op


class TestSubject(unittest.TestCase):
    def test_subject_replays_initial(self):
        s = _BehaviorSubject(7)
        seen = []
        s.subscribe(seen.append)
        self.assertEqual(seen, [7])

    def test_subject_broadcasts_updates(self):
        s = _BehaviorSubject(0)
        seen = []
        sub = s.subscribe(seen.append)
        seen.clear()
        s.on_next(1)
        s.on_next(2)
        self.assertEqual(seen, [1, 2])
        sub.dispose()
        s.on_next(3)
        self.assertEqual(seen, [1, 2])


class TestUsbHeuristic(unittest.TestCase):
    def _fake_dev(self, vid, pid, classes):
        class _Cfg:
            def __init__(self, classes):
                self._classes = classes
            def __iter__(self):
                return iter([type("I", (), {"bInterfaceClass": c}) for c in self._classes])
        class _Dev:
            def __init__(self, vid, pid, classes):
                self.idVendor = vid
                self.idProduct = pid
                self._cfg = _Cfg(classes)
            def configurations(self):
                return [self._cfg]
        return _Dev(vid, pid, classes)

    def test_official_firmware_has_no_ff(self):
        dev = self._fake_dev(0x0694, 0x0009, [0x02, 0x0A, 0xFF & 0x00])  # no 0xFF
        # Force classes to be simple non-FF
        dev._cfg._classes = [0x02, 0x0A]
        self.assertTrue(is_official_firmware_usb(dev))

    def test_pybricks_has_ff(self):
        dev = self._fake_dev(0x0694, 0x0009, [0x02, 0x0A, 0xFF])
        self.assertFalse(is_official_firmware_usb(dev))

    def test_other_device_rejected(self):
        dev = self._fake_dev(0x1234, 0x5678, [0x02])
        self.assertFalse(is_official_firmware_usb(dev))


if __name__ == "__main__":
    unittest.main()
