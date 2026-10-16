import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from spike_transfer import (BLE_SAFE_WRITE_SIZE, FILE_IO_IN_PROGRESS,
                            USER_PROGRAM_RUNNING, configure_ble_upload,
                            transfer_environment, wait_hub_idle)


class BLEPacketTests(unittest.TestCase):
    def test_caps_only_this_hub_at_twenty_bytes(self):
        hub = SimpleNamespace(_max_write_size=244)
        other = SimpleNamespace(_max_write_size=244)
        self.assertEqual(configure_ble_upload(hub), (244, 20))
        self.assertEqual(hub._max_write_size, BLE_SAFE_WRITE_SIZE)
        self.assertEqual(other._max_write_size, 244)

    def test_does_not_enlarge_a_smaller_advertised_limit(self):
        hub = SimpleNamespace(_max_write_size=16)
        self.assertEqual(configure_ble_upload(hub), (16, 16))

    def test_invalid_limits_fail_explicitly(self):
        for limit in (None, 0, 5, True, '244'):
            with self.subTest(limit=limit), self.assertRaises(RuntimeError):
                configure_ble_upload(SimpleNamespace(_max_write_size=limit))

    def test_environment_handles_missing_distributions(self):
        from importlib.metadata import PackageNotFoundError
        with patch('spike_transfer.version', side_effect=PackageNotFoundError):
            text = transfer_environment(SimpleNamespace(fw_version='4.1.0'))
        self.assertIn('firmware=4.1.0', text)
        self.assertIn('pybricksdev=unknown', text)
        self.assertIn('bleak=unknown', text)

    def test_real_pybricksdev_chunks_fit_baseline_att_mtu(self):
        try:
            from pybricksdev.connections.pybricks import PybricksHubBLE
        except ImportError:
            self.skipTest('Runtime dependencies not installed; CI installs requirements.txt')
        async def check():
            hub = PybricksHubBLE.__new__(PybricksHubBLE)
            hub._max_write_size = 244
            hub._max_user_program_size = 4096
            hub.write_gatt_char = AsyncMock()
            configure_ble_upload(hub)
            program = bytes(range(100))
            with patch('pybricksdev.connections.pybricks.tqdm'):
                await hub.download_user_program(program)
            packets = [call.args[1] for call in hub.write_gatt_char.call_args_list]
            self.assertTrue(all(len(packet) <= 20 for packet in packets))
            self.assertTrue(all(call.kwargs['response'] for call in hub.write_gatt_char.call_args_list))
            self.assertEqual(packets[0], b'\x03\x00\x00\x00\x00')
            chunks = [packet for packet in packets if packet[0] == 4]
            self.assertEqual([int.from_bytes(p[1:5], 'little') for p in chunks], list(range(0, 100, 15)))
            self.assertEqual(b''.join(packet[5:] for packet in chunks), program)
            self.assertEqual(packets[-1], b'\x03\x64\x00\x00\x00')
        asyncio.run(check())


class HubReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_idle_returns_without_fixed_delay(self):
        hub = SimpleNamespace(status_observable=SimpleNamespace(value=0))
        with patch('spike_transfer.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await wait_hub_idle(hub)
        sleep.assert_not_awaited()

    async def test_waits_for_running_and_file_transfer_flags(self):
        for flag in (USER_PROGRAM_RUNNING, FILE_IO_IN_PROGRESS, USER_PROGRAM_RUNNING | FILE_IO_IN_PROGRESS):
            with self.subTest(flag=flag):
                status = SimpleNamespace(value=flag)
                hub = SimpleNamespace(status_observable=status)
                async def become_idle(delay):
                    status.value = 0
                with patch('spike_transfer.asyncio.sleep', side_effect=become_idle) as sleep:
                    await wait_hub_idle(hub)
                sleep.assert_awaited_once()

    async def test_idle_ignores_unrelated_battery_flags(self):
        hub = SimpleNamespace(status_observable=SimpleNamespace(value=1))
        await wait_hub_idle(hub, timeout=0)

    async def test_stuck_hub_times_out_without_starting_transfer(self):
        hub = SimpleNamespace(status_observable=SimpleNamespace(value=FILE_IO_IN_PROGRESS))
        with self.assertRaises(TimeoutError):
            await wait_hub_idle(hub, timeout=0)

    async def test_invalid_status_fails_instead_of_assuming_ready(self):
        hub = SimpleNamespace(status_observable=SimpleNamespace(value=None))
        with self.assertRaises(RuntimeError):
            await wait_hub_idle(hub)

    async def test_wait_is_cancellable(self):
        entered = asyncio.Event()
        hub = SimpleNamespace(status_observable=SimpleNamespace(value=FILE_IO_IN_PROGRESS))
        async def sleep_forever(delay):
            entered.set()
            await asyncio.Event().wait()
        with patch('spike_transfer.asyncio.sleep', side_effect=sleep_forever):
            task = asyncio.create_task(wait_hub_idle(hub))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == '__main__':
    unittest.main()
