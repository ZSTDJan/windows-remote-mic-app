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

    def test_missing_mutex_or_unavailable_api_reports_not_running(self):
        self.assertFalse(
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: single_instance.MutexOpenResult(0, 2),
                _close_handle=lambda _handle: True,
            )
        )
        self.assertFalse(
            single_instance.bridge_instance_running(
                _open_mutex=lambda _name: (_ for _ in ()).throw(OSError("boom")),
                _close_handle=lambda _handle: True,
            )
        )

    def test_real_open_mutex_declares_pointer_safe_win32_prototype(self):
        source = inspect.getsource(single_instance._real_open_mutex)
        self.assertIn("OpenMutexW.argtypes", source)
        self.assertIn("OpenMutexW.restype = wintypes.HANDLE", source)
        self.assertIn("use_last_error=True", source)


if __name__ == "__main__":
    unittest.main()
