import contextlib
import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request


sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
spec = importlib.util.spec_from_file_location("session_tools", Path(__file__).resolve().parents[1] / "scripts" / "session.py")
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.exported = {
            "schema_version": 1,
            "origin": session.ORIGIN,
            "server_session_id": "EXAMPLE-SESSION-ID",
            "visible_cookie_header": "preference=example",
            "local_storage": {"example": "示例"},
            "session_storage": {"token": "EXAMPLE-SESSION-ID", "onAndOff": "0"},
        }

    def test_round_trip_preserves_origin_storage_without_logging_session_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "export.json"
            source.write_text(json.dumps(self.exported), encoding="utf-8")
            config = {"auth": {
                "cookies_file": ".local/auth/cookies.json",
                "storage_state_file": ".local/auth/state.json",
                "session_storage_file": ".local/auth/session.json",
            }}
            output = io.StringIO()
            with patch.object(session, "ROOT", root), contextlib.redirect_stdout(output):
                session.import_session(config, source)
            state = json.loads((root / ".local/auth/state.json").read_text(encoding="utf-8"))
            storage = json.loads((root / ".local/auth/session.json").read_text(encoding="utf-8"))
            self.assertEqual(storage, {session.ORIGIN: self.exported["session_storage"]})
            self.assertEqual(state["origins"][0]["localStorage"], [{"name": "example", "value": "示例"}])
            self.assertNotIn("EXAMPLE-SESSION-ID", output.getvalue())

    def test_rejects_session_from_other_site(self):
        self.exported["origin"] = "https://unrelated.example"
        with self.assertRaises(ValueError):
            session.prepare_session(self.exported)

    def test_rejects_cookie_header_injection(self):
        self.exported["server_session_id"] = "example\r\nInjected: value"
        with self.assertRaises(ValueError):
            session.prepare_session(self.exported)

    def test_session_token_can_supply_missing_httponly_cookie(self):
        self.exported["server_session_id"] = ""
        cookies, _, _ = session.prepare_session(self.exported)
        self.assertEqual(cookies[0]["value"], self.exported["session_storage"]["token"])

    def test_cookie_values_are_sent_only_to_requested_sports_endpoint(self):
        cookies, _, _ = session.prepare_session(self.exported)
        other = copy.deepcopy(cookies[0])
        other["domain"] = "jaccount.sjtu.edu.cn"
        other["name"] = "OTHER-COOKIE"
        jar = session.cookie_jar(cookies + [other])
        sports_request = Request(session.CURRENT_USER_URL)
        jar.add_cookie_header(sports_request)
        self.assertIn("JSESSIONID=EXAMPLE-SESSION-ID", sports_request.get_header("Cookie"))
        self.assertNotIn("OTHER-COOKIE", sports_request.get_header("Cookie"))
        for url in ("https://jaccount.sjtu.edu.cn/", "http://sports.sjtu.edu.cn/"):
            request = Request(url)
            jar.add_cookie_header(request)
            self.assertIsNone(request.get_header("Cookie"))

    def test_expired_cookie_is_not_sent(self):
        cookies, _, _ = session.prepare_session(self.exported)
        cookies[0]["expires"] = 1
        request = Request(session.CURRENT_USER_URL)
        session.cookie_jar(cookies).add_cookie_header(request)
        self.assertIsNone(request.get_header("Cookie"))

    def test_html_or_success_without_user_does_not_confirm_login(self):
        for result in (None, "login page", {"code": 0, "data": None}, {"code": 0, "data": {}}):
            self.assertFalse(session.authenticated_response(result))
        self.assertTrue(session.authenticated_response({"code": 0, "data": {"loginName": "example-user"}}))

    def test_redirect_is_not_followed_and_error_does_not_log_response(self):
        self.assertIsNone(session.NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://other.example/"))
        class RedirectingOpener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 302, "EXAMPLE-SECRET", {}, None)
        output = io.StringIO()
        with patch.object(session, "build_opener", return_value=RedirectingOpener()), contextlib.redirect_stdout(output):
            status = session.check_login({"auth": {"cookie_header": "JSESSIONID=EXAMPLE-SECRET"}})
        self.assertEqual(status, 2)
        self.assertNotIn("EXAMPLE-SECRET", output.getvalue())

    def test_rejects_output_outside_private_directory(self):
        for path in ("config.toml", ".local/../config.toml"):
            with self.assertRaises(ValueError):
                session.session_path({"auth": {"cookies_file": path}}, "cookies_file")


if __name__ == "__main__":
    unittest.main()
