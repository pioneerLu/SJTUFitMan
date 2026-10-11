"""Import a site session and verify it with one read-only request. Never log secrets."""

import argparse
import http.cookiejar
import json
from http.cookies import SimpleCookie
from pathlib import Path
import ssl
import sys
import tomllib
from configuration import CONFIG_PATH, read_config
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://sports.sjtu.edu.cn"
HOST = "sports.sjtu.edu.cn"
CURRENT_USER_URL = ORIGIN + "/system/user/currentUser"
MAX_EXPORT_BYTES = 2 * 1024 * 1024


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_config(path):
    return read_config(path)


def session_path(config, key):
    value = config.get("auth", {}).get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing auth.{key} in config/config.toml.")
    path = (ROOT / value).resolve()
    private_root = (ROOT / ".local").resolve()
    if not path.is_relative_to(private_root) or path == private_root:
        raise ValueError(f"auth.{key} must point to a file under .local/.")
    return path


def storage_values(value):
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError("Invalid browser storage format; export again.")
    return value


def prepare_session(exported):
    if not isinstance(exported, dict) or exported.get("schema_version") != 1:
        raise ValueError("Unsupported session export format.")
    if exported.get("origin") != ORIGIN:
        raise ValueError("Only the sports.sjtu.edu.cn session is accepted.")
    local_values = storage_values(exported.get("local_storage", {}))
    session_values = storage_values(exported.get("session_storage", {}))
    visible_header = exported.get("visible_cookie_header", "")
    if not isinstance(visible_header, str):
        raise ValueError("Invalid visible cookie format.")
    visible = SimpleCookie()
    visible.load(visible_header)
    cookie = visible.get("JSESSIONID")
    session_id = (
        exported.get("server_session_id")
        or (cookie.value if cookie else "")
        or session_values.get("token", "")
    )
    if not isinstance(session_id, str) or not session_id or any(
        char in ";,\"\\" or not 33 <= ord(char) <= 126 for char in session_id
    ):
        raise ValueError("No usable session identifier found; full browser cookie export is needed.")
    # Reconstruct this host's session cookie from the authenticated site's identifier.
    # Console export cannot preserve the original HttpOnly cookie's full metadata.
    cookies = [{
        "name": "JSESSIONID",
        "value": session_id,
        "domain": HOST,
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }]
    state = {
        "cookies": cookies,
        "origins": [{
            "origin": ORIGIN,
            "localStorage": [{"name": key, "value": value} for key, value in local_values.items()],
        }],
    }
    return cookies, state, {ORIGIN: session_values}


def import_session(config, source):
    if source.stat().st_size > MAX_EXPORT_BYTES:
        raise ValueError("Session export is too large.")
    with source.open(encoding="utf-8-sig") as stream:
        exported = json.load(stream)
    snapshots = prepare_session(exported)
    keys = ("cookies_file", "storage_state_file", "session_storage_file")
    paths = [session_path(config, key) for key in keys]
    if len(set(paths)) != len(paths):
        raise ValueError("Session output paths must be different files.")
    for path, snapshot in zip(paths, snapshots):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Session imported locally. Login must still pass the server check.")


def cookie_jar(cookies):
    if not isinstance(cookies, list):
        raise ValueError("Cookie file must contain a browser cookie list.")
    jar = http.cookiejar.CookieJar()
    for item in cookies:
        # Ignore cookies outside the requested site's domain, including jAccount cookies.
        if not isinstance(item, dict) or item.get("domain", "").lstrip(".") != HOST:
            continue
        expires = item.get("expires", -1)
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=item["name"], value=item["value"], port=None,
            port_specified=False, domain=HOST, domain_specified=False,
            domain_initial_dot=False, path=item.get("path", "/"), path_specified=True,
            secure=True, expires=None if expires is None or expires < 0 else int(expires),
            discard=expires is None or expires < 0, comment=None, comment_url=None,
            rest={"HttpOnly": None} if item.get("httpOnly") else {}, rfc2109=False,
        ))
    return jar


def authenticated_response(result):
    return (
        isinstance(result, dict)
        and (result.get("code") == 0 or result.get("msg") == "操作成功")
        and isinstance(result.get("data"), dict)
        and isinstance(result["data"].get("loginName"), str)
        and bool(result["data"]["loginName"].strip())
    )


def check_login(config):
    headers = {"Accept": "application/json", "User-Agent": "SJTUFitMan/0.1"}
    raw_header = config.get("auth", {}).get("cookie_header", "").strip()
    if "\r" in raw_header or "\n" in raw_header:
        raise ValueError("Cookie header must be a single line.")
    if raw_header:
        headers["Cookie"] = raw_header
        jar = http.cookiejar.CookieJar()
    else:
        path = session_path(config, "cookies_file")
        if not path.exists():
            raise ValueError("No local session found. Import the session first.")
        with path.open(encoding="utf-8-sig") as stream:
            jar = cookie_jar(json.load(stream))
        if not list(jar):
            raise ValueError("No cookies for sports.sjtu.edu.cn were found.")
    opener = build_opener(NoRedirects(), HTTPCookieProcessor(jar), HTTPSHandler(context=ssl.create_default_context()))
    request = Request(CURRENT_USER_URL, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(MAX_EXPORT_BYTES + 1)
            if len(body) > MAX_EXPORT_BYTES:
                raise ValueError("Unexpectedly large login-check response.")
        result = json.loads(body)
    except HTTPError as error:
        print(f"Login not confirmed (HTTP {error.code}); no redirects were followed.")
        return 2
    except (URLError, TimeoutError, ssl.SSLError):
        print("Login check could not reach the server; session validity remains unknown.")
        return 3
    except (json.JSONDecodeError, UnicodeError):
        print("Server did not return the expected JSON; login is not confirmed.")
        return 2
    if not authenticated_response(result):
        print("Server did not confirm login. Re-login or export full browser cookies.")
        return 2
    print("Login confirmed by the server. No reservation was submitted and no email was sent.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Import a Console session export saved locally.")
    importer.add_argument("source", type=Path)
    commands.add_parser("check", help="Make one read-only current-user request.")
    options = parser.parse_args()
    try:
        config = load_config(options.config)
        if options.command == "import":
            import_session(config, options.source)
            return 0
        return check_login(config)
    except (ValueError, KeyError, TypeError, OSError, http.cookiejar.LoadError):
        # Input/config exceptions can contain pasted tokens. Do not print exception text.
        print("Session operation failed. Check the config, local export, and file paths; credentials were not printed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
