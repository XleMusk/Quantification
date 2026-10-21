import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from a_stock_committee.market_data import _baostock_session_lock, _baostock_transport, baostock_session


class BaoStockSessionTests(unittest.TestCase):
    def test_other_process_rejected_and_lock_released_without_network(self):
        code = "from a_stock_committee.market_data import _baostock_session_lock\nwith _baostock_session_lock(): print('acquired')"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BAOSTOCK_SESSION_LOCK": str(Path(folder) / "lock")}):
            with _baostock_session_lock():
                result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BaoStock session busy", result.stderr)
                self.assertNotIn("acquired", result.stdout)
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("acquired", result.stdout)

    def test_exception_logs_out_and_does_not_leave_lock(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BAOSTOCK_SESSION_LOCK": str(Path(folder) / "lock")}), patch("baostock.login", return_value=SimpleNamespace(error_code="0")), patch("baostock.logout", side_effect=lambda: calls.append("logout")):
            with self.assertRaisesRegex(ValueError, "query failed"):
                with baostock_session():
                    raise ValueError("query failed")
            self.assertEqual(calls, ["logout"])
            with _baostock_session_lock():
                pass

    def test_real_sdk_eof_stops_and_restores_transport(self):
        import baostock.common.context as context
        import baostock.util.socketutil as transport
        from unittest.mock import Mock

        connection = Mock()
        connection.recv.return_value = b""
        original = transport.send_msg
        with patch.object(context, "default_socket", connection, create=True):
            with _baostock_transport() as state:
                with self.assertRaisesRegex(ConnectionError, "peer closed"):
                    transport.send_msg("probe")
                self.assertTrue(state["failed"])
                self.assertIs(context.default_socket, connection)
                connection.recv.assert_called_once_with(8192)
            self.assertIs(transport.send_msg, original)
            self.assertIsNone(context.default_socket)
            connection.close.assert_called_once()

    def test_normal_sdk_response_remains_unchanged(self):
        import baostock.common.context as context
        import baostock.common.contants as constants
        import baostock.data.messageheader as header
        import baostock.util.socketutil as transport
        from unittest.mock import Mock

        message = header.to_message_header(constants.MESSAGE_TYPE_LOGOUT_RESPONSE, 2) + "ok<![CDATA[]]>\n"
        connection = Mock()
        connection.recv.side_effect = [message[:10].encode(), message[10:].encode()]
        with patch.object(context, "default_socket", connection, create=True), _baostock_transport() as state:
            self.assertEqual(transport.send_msg("probe"), message)
            self.assertFalse(state["failed"])
            self.assertIs(context.default_socket, connection)

    def test_failed_query_does_not_mask_error_with_logout(self):
        import baostock.common.context as context
        import baostock.util.socketutil as transport
        from unittest.mock import Mock

        connection = Mock()
        connection.recv.return_value = b""
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BAOSTOCK_SESSION_LOCK": str(Path(folder) / "lock")}), patch.object(context, "default_socket", connection, create=True), patch("baostock.login", return_value=SimpleNamespace(error_code="0")), patch("baostock.logout") as logout:
            with self.assertRaisesRegex(ConnectionError, "peer closed"):
                with baostock_session():
                    transport.send_msg("probe")
            logout.assert_not_called()
            connection.close.assert_called_once()
            with _baostock_session_lock():
                pass


if __name__ == "__main__":
    unittest.main()
