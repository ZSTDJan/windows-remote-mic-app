import inspect
import unittest

from ovb_rc003 import single_instance


class BridgeInstanceStatusTests(unittest.TestCase):
    def test_open_handle_proves_running_and_is_closed(self):
        closed = []
        running = single_instance.bridge_instance_running(
            _open_mutex=lambda _name: single_instance.MutexOpenResult(41, 0),
            _close_handle=lambda handle: closed.append(handle) or True,
        )

        self.assertTrue(running)
        self.assertEqual(closed, [41])

    def test_access_denied_also_proves_the_named_mutex_exists(self):
        running = single_instance.bridge_instance_running(
            _open_mutex=lambda _name: single_instance.MutexOpenResult(
                0, single_instance._ERROR_ACCESS_DENIED
            ),
            _close_handle=lambda _handle: True,
        )

        self.assertTrue(running)

    def test_open_handle_close_false_is_reported_as_cleanup_failure(self):
        with self.assertRaises(single_instance.MutexCleanupError) as ctx:
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: single_instance.MutexOpenResult(41, 0),
                _close_handle=lambda _handle: False,
            )

        self.assertIn("CloseHandle returned FALSE", str(ctx.exception))

    def test_open_handle_close_exception_is_reported_without_handle_value(self):
        plausible_handle = 0x0000_0140_0000_1000
        with self.assertRaises(single_instance.MutexCleanupError) as ctx:
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: single_instance.MutexOpenResult(
                    plausible_handle, 0
                ),
                _close_handle=lambda _handle: (_ for _ in ()).throw(
                    OSError("simulated close failure")
                ),
            )

        message = str(ctx.exception)
        self.assertIn("CloseHandle raised an exception", message)
        self.assertNotIn(str(plausible_handle), message)
        self.assertNotIn(hex(plausible_handle), message)

    def test_missing_mutex_reports_not_running(self):
        self.assertFalse(
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: single_instance.MutexOpenResult(0, 2),
                _close_handle=lambda _handle: True,
            )
        )

    def test_unavailable_api_fails_closed(self):
        with self.assertRaises(single_instance.SingleInstanceUnavailableError):
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: (_ for _ in ()).throw(OSError("boom")),
                _close_handle=lambda _handle: True,
            )

    def test_unexpected_open_error_fails_closed(self):
        with self.assertRaises(single_instance.SingleInstanceUnavailableError):
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: single_instance.MutexOpenResult(0, 87),
                _close_handle=lambda _handle: True,
            )

    def test_real_open_mutex_declares_pointer_safe_win32_prototype(self):
        source = inspect.getsource(single_instance._real_open_mutex)
        self.assertIn("OpenMutexW.argtypes", source)
        self.assertIn("OpenMutexW.restype = wintypes.HANDLE", source)
        self.assertIn("use_last_error=True", source)


if __name__ == "__main__":
    unittest.main()
