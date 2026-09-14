"""Run the shipped Gadget DLL and script in a disposable child, never WUDFHost."""

import json
import lzma
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from ovb_rc003 import frida_hid_tap_runtime as runtime


@unittest.skipUnless(os.name == "nt", "Windows Gadget runtime")
class GadgetLifecycleTests(unittest.TestCase):
    def test_real_script_reconnects_after_eof_and_reloads_twice_in_same_host(self):
        archive = runtime.gadget_archive_path()
        self.assertEqual(runtime.sha256_file(archive), runtime.GADGET_ARCHIVE_SHA256)
        with tempfile.TemporaryDirectory(prefix="rc003-gadget-lifecycle-") as raw, socket.socket() as server:
            root = Path(raw)
            dll = root / runtime.GADGET_DLL_NAME
            dll.write_bytes(lzma.decompress(archive.read_bytes()))
            self.assertEqual(runtime.sha256_file(dll), runtime.GADGET_DLL_SHA256)
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            server.settimeout(10)
            config = json.loads(runtime.gadget_config_text())
            self.assertEqual(config["interaction"]["on_change"], "reload")
            config["interaction"]["parameters"]["port"] = server.getsockname()[1]
            (root / runtime.GADGET_CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
            script = root / runtime.GADGET_SCRIPT_NAME

            def replace_script(revision):
                # Only the reported diagnostic revision changes. The socket,
                # hooks, init/dispose and timers are the production code.
                source = runtime.GADGET_SCRIPT.replace(
                    f"const HID_DIAGNOSTIC_REVISION = {runtime.HID_DIAGNOSTIC_REVISION};",
                    f"const HID_DIAGNOSTIC_REVISION = {revision};",
                )
                temporary = script.with_suffix(".tmp")
                temporary.write_text(source, encoding="utf-8")
                os.replace(temporary, script)

            def accept_ready():
                connection, _address = server.accept()
                connection.settimeout(10)
                self.addCleanup(connection.close)
                data = b""
                while b"\n" not in data:
                    chunk = connection.recv(4096)
                    self.assertTrue(chunk, "Gadget closed before handshake")
                    data += chunk
                ready = json.loads(data.split(b"\n", 1)[0])
                self.assertEqual(ready["kind"], "ready")
                self.assertTrue(ready["hook_installed"])
                return connection, ready

            replace_script(1)
            child = subprocess.Popen(
                [sys.executable, "-c", "import ctypes,sys; ctypes.WinDLL(sys.argv[1]); sys.stdin.read()", str(dll)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                connection, ready = accept_ready()
                host = ready["pid"]
                self.assertEqual(ready["diagnostic_revision"], 1)
                # No pending writes: this specifically exercises read()'s EOF.
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
                connection, ready = accept_ready()
                self.assertEqual((ready["pid"], ready["diagnostic_revision"]), (host, 1))
                for revision in (2, 3):
                    replace_script(revision)
                    self.assertEqual(connection.recv(4096), b"", "dispose did not close old connection")
                    connection.close()
                    connection, ready = accept_ready()
                    self.assertEqual((ready["pid"], ready["diagnostic_revision"]), (host, revision))
                connection.close()
            finally:
                # Cooperative exit also closes the venv launcher's child.
                child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0)


if __name__ == "__main__":
    unittest.main()
