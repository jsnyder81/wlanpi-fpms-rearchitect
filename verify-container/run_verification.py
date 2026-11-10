#!/usr/bin/env python3
"""Container verification script for wlanpi-menud + wlanpi-webui nginx integration.

Validates all Phase 2 exit criteria:
- Real Flask create_app behind real nginx + real menud
- Concurrent TUI on local.sock + multiple browser tabs (rich private + thin mirror + second user)
- Bidirectional thin mirroring and concurrent rich action invocations
- Real POST /logout via nginx revoking only that user's menu sessions with 4401
- Server-side password-change revocation closing live WebSocket with 4401
- Security boundaries: nginx auth_request, forged header dropping, origin check (1008),
  and local admin privilege check on local.sock (real UID 1001 operator gets FORBIDDEN).
- The redesigned /menu page driven in real headless Chromium (Playwright,
  drive_menu_ui.mjs) through nginx: hash routing, run-on-arrival for read-only
  views only, no mutation without Run, paging, prompts, job progress, the Front
  panel pane on a second connection, resume across a menud restart, logout.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("verifier")

WORKSPACE = Path("/src")
MENUD_PATH = WORKSPACE / "wlanpi-menud"
WEBUI_PATH = WORKSPACE / "wlanpi-webui"

# Ensure menud, webui and their dependencies are importable
sys.path.insert(0, str(WORKSPACE / "verify-container"))
sys.path.insert(0, str(MENUD_PATH))
sys.path.insert(0, str(MENUD_PATH / "tests"))
sys.path.insert(0, str(WEBUI_PATH))
sys.path.insert(0, str(WEBUI_PATH / ".venv/lib/python3.13/site-packages"))
sys.path.insert(0, str(MENUD_PATH / ".venv/lib/python3.13/site-packages"))

from fakecore import BASE, FakeCore
from wlanpi_menud.config import Config as MConfig
from wlanpi_menud.core import CoreClient, TokenSource
from wlanpi_menud.poller import Poller
from wlanpi_menud.server import Server
from wlanpi_menud.service import Service
from wlanpi_webui.menud import revoke_menu_session

import wlanpi_webui.app as webapp
import wlanpi_webui.auth.auth as wauth
from wlanpi_webui.config import Config as WConfig


# ---------------------------------------------------------------------------
# WebSocket frame encode/decode helpers
# ---------------------------------------------------------------------------

def make_ws_frame(payload: bytes, opcode: int = 0x01) -> bytes:
    """Create a client-to-server WebSocket frame (masked)."""
    mask = b"\x12\x34\x56\x78"
    masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length]) + mask
    elif length < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length) + mask
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length) + mask
    return header + masked_payload


def read_ws_frame(sock: socket.socket, timeout: float = 4.0) -> dict | None:
    """Read a single WebSocket frame from a socket."""
    sock.settimeout(timeout)
    try:
        header = sock.recv(2)
    except (socket.timeout, TimeoutError):
        return None
    except OSError:
        return None

    if len(header) < 2:
        return None

    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    payload_len = b2 & 0x7F

    if payload_len == 126:
        ext = sock.recv(2)
        if len(ext) < 2:
            return None
        payload_len = struct.unpack("!H", ext)[0]
    elif payload_len == 127:
        ext = sock.recv(8)
        if len(ext) < 8:
            return None
        payload_len = struct.unpack("!Q", ext)[0]

    mask = sock.recv(4) if is_masked else b""
    if is_masked and len(mask) < 4:
        return None

    data = bytearray()
    while len(data) < payload_len:
        try:
            chunk = sock.recv(payload_len - len(data))
        except (socket.timeout, TimeoutError, OSError):
            break
        if not chunk:
            break
        data.extend(chunk)

    if len(data) < payload_len:
        return None

    if is_masked:
        data = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))

    if opcode == 0x08:  # Close frame
        code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
        reason = data[2:].decode("utf-8", errors="ignore")
        return {"type": "close", "code": code, "reason": reason}

    if opcode == 0x01:  # Text frame
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return {"type": "raw", "data": bytes(data)}

    return {"type": f"opcode_{opcode}", "data": bytes(data)}


def drain_until(s: socket.socket, pred, limit: int = 25, timeout: float = 4.0) -> dict | None:
    """Drain frames from WebSocket until predicate matches."""
    for _ in range(limit):
        f = read_ws_frame(s, timeout)
        if f is None:
            return None
        if pred(f):
            return f
    return None


def raw_ws_connect(
    host: str,
    port: int,
    path: str,
    headers: dict[str, str],
    timeout: float = 4.0,
) -> tuple[int, dict[str, str], ssl.SSLSocket]:
    """Perform a raw TLS WebSocket handshake."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    raw_sock = socket.create_connection((host, port), timeout=timeout)
    tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)

    req_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {headers.get('Host', host)}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in headers.items():
        if k.lower() not in ("host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version"):
            req_lines.append(f"{k}: {v}")
    req_lines.append("\r\n")

    tls_sock.sendall("\r\n".join(req_lines).encode("iso-8859-1"))

    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = tls_sock.recv(1024)
        if not chunk:
            break
        resp += chunk

    headers_text = resp.decode("iso-8859-1")
    lines = headers_text.split("\r\n")
    status_line = lines[0]
    status_code = int(status_line.split()[1])
    resp_headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()

    return status_code, resp_headers, tls_sock


def raw_ws_connect_unix(
    sock_path: str | Path,
    path: str = "/ui/ws",
    timeout: float = 4.0,
) -> tuple[int, dict[str, str], socket.socket]:
    """Connect to a local unix domain socket and perform a WebSocket upgrade."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(sock_path))

    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock.sendall(req)

    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(1024)
        if not chunk:
            break
        resp += chunk

    headers_text = resp.decode("iso-8859-1")
    lines = headers_text.split("\r\n")
    status_line = lines[0]
    status_code = int(status_line.split()[1])
    resp_headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()

    return status_code, resp_headers, sock


def run_real_uid_operator_client(
    sock_path: str | Path,
    uid: int,
    user_name: str | None,
    session_id_to_revoke: str,
) -> tuple[dict | None, dict | None]:
    """Connect to local.sock as a real separate process with operator UID.

    Uses sudo -u so SO_PEERCRED carried by the Linux kernel genuinely
    reflects the operator UID (1001). Returns (welcome_frame, error_frame).
    """
    helper_code = f"""
import json, socket, struct, sys

def make_ws_frame(payload: bytes) -> bytes:
    mask = b"\\xaa\\xbb\\xcc\\xdd"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length]) + mask
    elif length < 65536:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, length) + mask
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, length) + mask
    return header + masked

def read_ws_frame(sock, timeout=4.0):
    sock.settimeout(timeout)
    try:
        head = sock.recv(2)
    except (socket.timeout, TimeoutError):
        return None
    if len(head) < 2:
        return None
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    payload_len = b2 & 0x7F
    if payload_len == 126:
        payload_len = int.from_bytes(sock.recv(2), "big")
    elif payload_len == 127:
        payload_len = int.from_bytes(sock.recv(8), "big")
    mask = sock.recv(4) if is_masked else b""
    data = b""
    while len(data) < payload_len:
        chunk = sock.recv(payload_len - len(data))
        if not chunk:
            break
        data += chunk
    if is_masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if opcode == 0x08:
        code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
        reason = data[2:].decode("utf-8", "ignore")
        return {{"type": "close", "code": code, "reason": reason}}
    if opcode == 0x01:
        return json.loads(data.decode("utf-8"))
    return None

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(4.0)
sock.connect({repr(str(sock_path))})

# HTTP WebSocket handshake
req = (
    "GET /ui/ws HTTP/1.1\\r\\n"
    "Host: localhost\\r\\n"
    "Upgrade: websocket\\r\\n"
    "Connection: Upgrade\\r\\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\\r\\n"
    "Sec-WebSocket-Version: 13\\r\\n\\r\\n"
).encode()
sock.sendall(req)
resp = b""
while b"\\r\\n\\r\\n" not in resp:
    chunk = sock.recv(1024)
    if not chunk:
        break
    resp += chunk

# Send hello as operator
sock.sendall(make_ws_frame(json.dumps({{"v": 1, "type": "hello", "id": "h:op", "mode": "rich", "protocol": [1]}}).encode()))
welcome = read_ws_frame(sock)

# Send revoke
sock.sendall(make_ws_frame(json.dumps({{"v": 1, "type": "revoke", "id": "r:op", "webui_session": {repr(session_id_to_revoke)}}}).encode()))

err = None
while True:
    f = read_ws_frame(sock, timeout=2.0)
    if not f:
        break
    if f.get("type") == "error":
        err = f
        break

sock.close()
print(json.dumps({{"welcome": welcome, "error": err}}))
"""
    sudo_bin = shutil.which("sudo")
    if sudo_bin and user_name:
        cmd = [sudo_bin, "-u", user_name, sys.executable, "-c", helper_code]
        preexec = None
    elif sudo_bin:
        cmd = [sudo_bin, "-u", f"#{uid}", sys.executable, "-c", helper_code]
        preexec = None
    else:
        cmd = [sys.executable, "-c", helper_code]
        def set_creds():
            try:
                os.setgroups([])
            except Exception:
                pass
            os.setgid(uid)
            os.setuid(uid)
        preexec = set_creds

    proc = subprocess.run(
        cmd,
        preexec_fn=preexec,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    return data.get("welcome"), data.get("error")


# ---------------------------------------------------------------------------
# Real Flask app & Nginx management
# ---------------------------------------------------------------------------

def setup_nginx() -> subprocess.Popen:
    """Setup self-signed certs and real nginx configuration."""
    ssl_dir = Path("/etc/nginx/ssl")
    ssl_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes", "-days", "1", "-newkey", "rsa:2048",
            "-keyout", str(ssl_dir / "self-signed-wlanpi.key"),
            "-out", str(ssl_dir / "self-signed-wlanpi.cert"),
            "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    shutil.copy(WEBUI_PATH / "install/etc/nginx/nginx.conf", "/etc/nginx/nginx.conf")
    sites_enabled = Path("/etc/nginx/sites-enabled")
    sites_enabled.mkdir(parents=True, exist_ok=True)
    shutil.copy(WEBUI_PATH / "install/etc/nginx/wlanpi_webui.conf", sites_enabled / "wlanpi_webui.conf")

    conf_d = Path("/etc/wlanpi-webui/nginx/conf.d")
    conf_d.mkdir(parents=True, exist_ok=True)
    shutil.copy(WEBUI_PATH / "install/etc/nginx/conf.d/wlanpi-menud.conf", conf_d / "wlanpi-menud.conf")

    test_res = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    assert test_res.returncode == 0, f"nginx -t failed: {test_res.stderr}"

    proc = subprocess.Popen(["nginx", "-g", "daemon off;"])
    time.sleep(1)
    log.info("nginx started successfully")
    return proc


def start_flask():
    """Start real Flask create_app answering /auth/check and /logout on unix socket."""
    WConfig.SESSION_STORE_PATH = "/tmp/sessions.json"
    wauth.account_still_allowed = lambda: True  # no PAM/core daemon in the container
    webapp.system_service_running_state = lambda *a, **k: True
    app = webapp.create_app(WConfig)

    from werkzeug.serving import make_server
    sock_path = "/run/wlanpi_webui.sock"
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = make_server("unix://" + sock_path, 0, app, threaded=True)
    os.chmod(sock_path, 0o666)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Real Flask WebUI serving on %s", sock_path)
    return app


def mint_session(app, username: str) -> tuple[str, str, str, str]:
    """Mint a signed Flask session cookie for `username`."""
    import flask
    with app.test_request_context("/"):
        wauth._login_session(username)
        data = dict(flask.session)
        cookie = app.session_interface.get_signing_serializer(app).dumps(data)
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    return cookie_name, cookie, data["sid"], data["csrf_token"]


def browser_connect(cookie_hdr: str, hello: dict):
    """Connect to /ui/ws over TLS and perform hello exchange."""
    st, _, s = raw_ws_connect(
        "127.0.0.1", 443, "/ui/ws",
        {"Host": "localhost", "Origin": "https://localhost", "Cookie": cookie_hdr},
    )
    if st != 101:
        return st, None, None
    s.sendall(make_ws_frame(json.dumps(hello).encode()))
    w = read_ws_frame(s)
    return st, s, w


def http_logout(cookie_hdr: str, csrf_token: str) -> int:
    """Send a real POST /logout through nginx HTTPS."""
    ctx = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection("127.0.0.1", 443, context=ctx, timeout=5)
    body = "csrf_token=" + csrf_token
    conn.request(
        "POST", "/logout", body=body,
        headers={
            "Host": "localhost",
            "Origin": "https://localhost",
            "Cookie": cookie_hdr,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": csrf_token,
        },
    )
    resp = conn.getresponse()
    resp.read()
    return resp.status


# ---------------------------------------------------------------------------
# Control channel for the browser-driven e2e (drive_menu_ui.mjs)
# ---------------------------------------------------------------------------

CONTROL_PORT = 8099


class InvokeAudit(logging.Handler):
    """Collects menud's audit-log invoke lines: the server-side record that an action ran."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.invokes: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if isinstance(record.msg, str) and record.msg.startswith("invoke %s by %s") and record.args:
            node, user, kind = record.args[0], record.args[1], record.args[2]
            self.invokes.append({"node": str(node), "user": str(user), "kind": str(kind)})


class TuiClient:
    """A raw TUI-style client on local.sock, attached to the default session."""

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self.sock: socket.socket | None = None
        self.last_view: dict | None = None
        self.lock = threading.Lock()
        self.n = 0

    def _connect(self) -> None:
        _, _, sock = raw_ws_connect_unix(self.sock_path)
        sock.sendall(make_ws_frame(json.dumps({
            "v": 1, "type": "hello", "id": "h:e2e-tui", "mode": "thin", "session": "default", "protocol": [1],
            "client": {"name": "wlanpi-menu-tui"},
        }).encode()))
        welcome = read_ws_frame(sock)
        assert welcome and welcome.get("type") == "welcome", f"TUI welcome: {welcome}"
        self.sock = sock
        self.last_view = drain_until(sock, lambda f: f.get("type") == "view")

    def _drain(self, timeout: float) -> None:
        while self.sock is not None:
            f = read_ws_frame(self.sock, timeout)
            if f is None:
                return
            if f.get("type") == "close":
                self.sock = None
                return
            if f.get("type") == "view":
                self.last_view = f

    def press(self, key: str) -> dict | None:
        with self.lock:
            if self.sock is None:
                self._connect()
            self._drain(0.05)
            if self.sock is None:
                self._connect()
            assert self.sock is not None
            self.n += 1
            mid = f"i:e2e:{self.n}"
            self.sock.sendall(make_ws_frame(json.dumps({"v": 1, "type": "input", "id": mid, "key": key}).encode()))
            reply = drain_until(self.sock, lambda f: f.get("type") == "view" and f.get("id") == mid)
            if reply is not None:
                self.last_view = reply
            return reply

    def view(self, wait: float = 0.6) -> dict | None:
        with self.lock:
            if self.sock is None:
                self._connect()
            self._drain(wait)
            return self.last_view

    def close(self) -> None:
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


def start_control_server(handlers: dict) -> ThreadingHTTPServer:
    """127.0.0.1 only. `handlers` maps "METHOD /path" to fn(query, body) -> JSON-able."""

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            fn = handlers.get(f"{self.command} {url.path}")
            if fn is None:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                out, status = fn(query, body), 200
            except Exception as exc:  # reported to the driver, which fails that assertion
                log.exception("control %s %s failed", self.command, url.path)
                out, status = {"error": f"{exc.__class__.__name__}: {exc}"}, 500
            data = json.dumps(out, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _serve
        do_POST = _serve

        def log_message(self, *args) -> None:  # keep the transcript to the driver's own lines
            return

    srv = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# Main Verification Execution
# ---------------------------------------------------------------------------

async def main():
    log.info("Starting comprehensive container verification...")

    nginx_proc = setup_nginx()
    flask_app = start_flask()

    # Operator UID 1001 for SO_PEERCRED privilege check
    OPERATOR_UID = 1001
    OPERATOR_USER = "operator"
    subprocess.run(["useradd", "-u", str(OPERATOR_UID), "-g", "operator", OPERATOR_USER], capture_output=True)

    fake_core = FakeCore()
    tokens = TokenSource(("get-core-token",), runner=fake_core.token_runner())

    menud_proxy_sock = Path("/run/wlanpi-menud/proxy/proxy.sock")
    menud_local_sock = Path("/run/wlanpi-menud/local.sock")
    menud_config = MConfig(
        builtin_dir=MENUD_PATH / "share" / "menu.d",
        proxy_sock=menud_proxy_sock,
        local_sock=menud_local_sock,
        proxy_sock_group="www-data",
        local_uids={os.getuid(): ("admin",), OPERATOR_UID: ("operator",)},
        allowed_origins=(),  # Origin netloc must match Host
        validate_outbound=True,
    )

    # The running service lives in a holder so the browser e2e can restart it
    # mid-run (a new Service means a new epoch, as on a real restart).
    menud: dict = {}

    async def start_menud() -> None:
        core_client = CoreClient(BASE, tokens, transport=fake_core.transport())
        svc = Service(menud_config, core=core_client, tokens=tokens)
        await Poller(svc).poll_once()
        srv = Server(svc)
        await srv.start()
        try:
            os.chmod(menud_proxy_sock, 0o666)
            os.chmod(menud_local_sock, 0o666)
        except Exception:
            pass
        menud["service"], menud["server"] = svc, srv

    async def stop_menud() -> None:
        await menud["service"].aclose()
        await menud["server"].close()

    await start_menud()
    log.info("wlanpi-menud started: proxy.sock and local.sock ready")

    invoke_audit = InvokeAudit()
    logging.getLogger("wlanpi_menud.audit").addHandler(invoke_audit)
    loop = asyncio.get_running_loop()

    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = ""):
        results.append((name, bool(cond), detail))
        log.info("[%s] %s%s", "PASS" if cond else "FAIL", name, f" [{detail}]" if detail else "")

    # Mint sessions for alice and bob
    name_a, cookie_a, sid_a, csrf_a = mint_session(flask_app, "alice")
    name_b, cookie_b, sid_b, csrf_b = mint_session(flask_app, "bob")
    ck_hdr_a = f"{name_a}={cookie_a}"
    ck_hdr_b = f"{name_b}={cookie_b}"

    def run():
        # -------------------------------------------------------------------
        # Test 1: Real /auth/check via nginx: valid session authenticates
        # -------------------------------------------------------------------
        log.info("Running Test 1: Valid auth session reaches proxy.sock...")
        st1, s1, w1 = browser_connect(
            ck_hdr_a,
            {"v": 1, "type": "hello", "id": "h:t1", "mode": "rich", "session": "new", "protocol": [1]},
        )
        check(
            "Test 1: Browser-less websocket with valid session reaches proxy.sock",
            st1 == 101 and w1 and w1.get("principal", {}).get("name") == "alice",
            f"status={st1} name={w1 and w1.get('principal', {}).get('name')}",
        )
        drain_until(s1, lambda f: f.get("type") == "menu")
        s1.close()

        # -------------------------------------------------------------------
        # Test 2: Websocket without valid auth returns 401
        # -------------------------------------------------------------------
        log.info("Running Test 2: Websocket without valid auth returns 401...")
        st2, _, s2 = raw_ws_connect("127.0.0.1", 443, "/ui/ws", {"Host": "localhost", "Origin": "https://localhost"})
        s2.close()
        check("Test 2: Websocket without valid auth returns 401", st2 == 401, f"status={st2}")

        # -------------------------------------------------------------------
        # Test 3: Origin mismatch refused (close code 1008)
        # -------------------------------------------------------------------
        log.info("Running Test 3: Origin mismatch is refused...")
        st3, _, s3 = raw_ws_connect(
            "127.0.0.1", 443, "/ui/ws",
            {"Host": "localhost", "Origin": "https://evil.attacker.com", "Cookie": ck_hdr_a},
        )
        if st3 == 101:
            f3 = read_ws_frame(s3)
            s3.close()
            check(
                "Test 3: Origin mismatch is refused (close code 1008)",
                f3 and (f3.get("code") == 1008 or f3.get("close_code") == 1008),
                f"frame={f3}",
            )
        else:
            s3.close()
            check("Test 3: Origin mismatch is refused (close code 1008)", st3 in (403, 1008), f"status={st3}")

        # -------------------------------------------------------------------
        # Test 4: Client-forged X-WebUI-User header overwritten by auth_request
        # -------------------------------------------------------------------
        log.info("Running Test 4: Client-forged headers overwritten by auth_request...")
        st4, _, s4 = raw_ws_connect(
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "Cookie": ck_hdr_a,
                "X-WebUI-User": "mallory",
                "X-WebUI-Session": "forged_session_id",
            },
        )
        assert st4 == 101, f"Expected 101, got {st4}"
        s4.sendall(make_ws_frame(json.dumps({"v": 1, "type": "hello", "id": "h:f", "mode": "rich", "protocol": [1]}).encode()))
        wf4 = read_ws_frame(s4)
        s4.close()
        check(
            "Test 4: Client-forged X-WebUI-User header overwritten by auth_request",
            wf4 and wf4.get("principal", {}).get("name") == "alice",
            f"name={wf4 and wf4.get('principal', {}).get('name')}",
        )

        # -------------------------------------------------------------------
        # Test 5: Rich mode invokes 3 actions and gets typed view block results
        # -------------------------------------------------------------------
        log.info("Running Test 5: Rich mode invokes actions and gets view blocks...")
        st5, s5, w5 = browser_connect(
            ck_hdr_a,
            {
                "v": 1, "type": "hello", "id": "h:rich", "mode": "rich", "session": "new", "protocol": [1],
                "capabilities": {"blocks": ["text", "kv", "table", "list", "qr", "status", "progress", "confirm"]},
            },
        )
        drain_until(s5, lambda f: f.get("type") == "menu")

        def invoke_and_check(node_id: str, mid: str, expected_block_type: str) -> bool:
            s5.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": mid, "node": node_id}).encode()))
            res = drain_until(s5, lambda f: f.get("type") == "job" and f.get("state") == "result")
            blocks = res.get("result", {}).get("blocks", []) if res else []
            return any(b.get("type") == expected_block_type for b in blocks)

        ok_table = invoke_and_check("network.interfaces", "act:1", "table")
        ok_kv1 = invoke_and_check("network.eth0_ipconfig", "act:2", "kv")
        ok_kv2 = invoke_and_check("network.lldp", "act:3", "kv")
        s5.close()
        check(
            "Test 5: Rich mode invokes 3 actions and gets typed view block results",
            ok_table and ok_kv1 and ok_kv2,
            f"table={ok_table} kv1={ok_kv1} kv2={ok_kv2}",
        )

        # -------------------------------------------------------------------
        # Test 5d: Grouped interfaces table at protocol level (the browser-driven
        # check of the same table is in Test 10)
        # -------------------------------------------------------------------
        log.info("Running Test 5d: network.interfaces returns one grouped table...")
        st5d, s5d, _ = browser_connect(
            ck_hdr_a,
            {"v": 1, "type": "hello", "id": "h:grouped", "mode": "rich", "session": "new", "protocol": [1]},
        )
        drain_until(s5d, lambda f: f.get("type") == "menu")
        s5d.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "act:grouped", "node": "network.interfaces"}).encode()))
        res5d = drain_until(s5d, lambda f: f.get("type") == "job" and f.get("state") == "result")
        s5d.close()
        tables5d = [b for b in (res5d or {}).get("result", {}).get("blocks", []) if b.get("type") == "table"]
        groups5d = [r[0] for t in tables5d for r in t.get("rows", []) if r and r[0]]
        check(
            "Test 5d: Grouped interfaces table: one table with Ethernet and WLAN groups",
            len(tables5d) == 1 and "Ethernet" in groups5d and "WLAN" in groups5d,
            f"tables={len(tables5d)} groups={groups5d}",
        )

        # -------------------------------------------------------------------
        # Test 5c: Nav frame assertion (branch, view-action, and invalid node)
        # -------------------------------------------------------------------
        log.info("Running Test 5c: Nav frame navigates to branch and view-action and reports errors...")
        st5c, s5c, w5c = browser_connect(
            ck_hdr_a,
            {
                "v": 1, "type": "hello", "id": "h:nav", "mode": "rich", "session": "new", "protocol": [1],
                "capabilities": {"blocks": ["text", "kv", "table", "list", "qr", "status", "progress", "confirm"]},
            },
        )
        drain_until(s5c, lambda f: f.get("type") == "menu")

        # 1. Nav to branch
        s5c.sendall(make_ws_frame(json.dumps({"v": 1, "type": "nav", "id": "nav:branch", "node": "network"}).encode()))
        v_branch = drain_until(s5c, lambda f: f.get("id") == "nav:branch" and f.get("type") == "view")
        ok_branch = (
            v_branch is not None
            and v_branch.get("screen") == "menu"
            and v_branch.get("breadcrumb") == ["Network"]
            and v_branch.get("title") == "Network"
        )

        # 2. Nav to view-action
        s5c.sendall(make_ws_frame(json.dumps({"v": 1, "type": "nav", "id": "nav:leaf", "node": "network.interfaces"}).encode()))
        v_leaf = drain_until(s5c, lambda f: f.get("type") == "view" and f.get("screen") == "result" and f.get("node") == "network.interfaces")
        ok_leaf = v_leaf is not None and any(b.get("type") == "table" for b in v_leaf.get("blocks", []))

        # 3. Nav to unknown node
        s5c.sendall(make_ws_frame(json.dumps({"v": 1, "type": "nav", "id": "nav:err", "node": "invalid.nonexistent"}).encode()))
        err_nav = drain_until(s5c, lambda f: f.get("id") == "nav:err" and f.get("type") == "error")
        ok_err = err_nav is not None and err_nav.get("code") == "NOT_FOUND"

        s5c.close()
        check(
            "Test 5c: Nav frame navigates to branch and view-action and reports errors",
            ok_branch and ok_leaf and ok_err,
            f"branch={ok_branch} leaf={ok_leaf} err={ok_err}",
        )

        # -------------------------------------------------------------------
        # Test 6: Concurrency: TUI + 2 alice browser tabs (rich + mirror) + 1 bob tab
        # -------------------------------------------------------------------
        log.info("Running Test 6: TUI and 3 browser tabs running concurrently...")
        # 1. TUI on local.sock (thin mode, default session)
        st_tui, _, sock_tui = raw_ws_connect_unix(menud_local_sock)
        sock_tui.sendall(make_ws_frame(json.dumps({
            "v": 1, "type": "hello", "id": "h:tui", "mode": "thin", "session": "default", "protocol": [1],
            "client": {"name": "wlanpi-menu-tui"},
        }).encode()))
        wt = read_ws_frame(sock_tui)
        drain_until(sock_tui, lambda f: f.get("type") == "view")
        default_id = wt["session_id"]

        # 2. Tab 1: alice rich new session
        st_a1, sock_a1, w_a1 = browser_connect(
            ck_hdr_a,
            {"v": 1, "type": "hello", "id": "h:a1", "mode": "rich", "session": "new", "protocol": [1]},
        )
        drain_until(sock_a1, lambda f: f.get("type") == "menu")

        # 3. Tab 2: alice thin mirror on default session
        st_a2, sock_a2, w_a2 = browser_connect(
            ck_hdr_a,
            {"v": 1, "type": "hello", "id": "h:a2", "mode": "thin", "session": "default", "protocol": [1]},
        )
        drain_until(sock_a2, lambda f: f.get("type") == "view")

        # 4. Tab 3: bob rich new session
        st_b1, sock_b1, w_b1 = browser_connect(
            ck_hdr_b,
            {"v": 1, "type": "hello", "id": "h:b1", "mode": "rich", "session": "new", "protocol": [1]},
        )
        drain_until(sock_b1, lambda f: f.get("type") == "menu")

        # Session assertions
        sessions_ok = (
            w_a2.get("session_id") == default_id and
            w_a1.get("session_id") != w_b1.get("session_id") and
            w_a1.get("session_id") != default_id
        )
        check("Test 6a: Concurrency: sessions established and separated", sessions_ok)

        # Bidirectional mirroring: TUI inputs 'down', mirror follows
        sock_tui.sendall(make_ws_frame(json.dumps({"v": 1, "type": "input", "id": "i:down", "key": "down"}).encode()))
        tui_view1 = drain_until(sock_tui, lambda f: f.get("type") == "view")
        mirror_view1 = drain_until(sock_a2, lambda f: f.get("type") == "view")
        tui_drives_mirror = (
            tui_view1 is not None and
            mirror_view1 is not None and
            mirror_view1.get("driven_by", {}).get("self") is False
        )
        check("Test 6b: TUI input fans out to browser mirror", tui_drives_mirror)

        # Mirror inputs 'up', TUI follows
        sock_a2.sendall(make_ws_frame(json.dumps({"v": 1, "type": "input", "id": "i:up", "key": "up"}).encode()))
        mirror_view2 = drain_until(sock_a2, lambda f: f.get("type") == "view")
        tui_view2 = drain_until(sock_tui, lambda f: f.get("type") == "view")
        mirror_drives_tui = (
            mirror_view2 is not None and
            tui_view2 is not None and
            tui_view2.get("driven_by", {}).get("self") is False
        )
        check("Test 6c: Browser mirror input drives TUI", mirror_drives_tui)

        # Concurrent rich invokes across different users
        sock_a1.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "inv:a", "node": "network.interfaces"}).encode()))
        sock_b1.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "inv:b", "node": "network.interfaces"}).encode()))
        res_a1 = drain_until(sock_a1, lambda f: f.get("type") == "job" and f.get("state") == "result")
        res_b1 = drain_until(sock_b1, lambda f: f.get("type") == "job" and f.get("state") == "result")
        check(
            "Test 6d: Concurrent rich action invocations for multiple users",
            res_a1 is not None and res_b1 is not None,
        )

        # -------------------------------------------------------------------
        # Test 6e: Private thin session assertion (mode=thin, session=new)
        # -------------------------------------------------------------------
        log.info("Running Test 6e: Private thin session navigates independently...")
        st_thin_priv, sock_thin_priv, w_thin_priv = browser_connect(
            ck_hdr_a,
            {"v": 1, "type": "hello", "id": "h:thin_priv", "mode": "thin", "session": "new", "protocol": [1]},
        )
        v_priv_home = drain_until(sock_thin_priv, lambda f: f.get("type") == "view")
        is_private_session = (
            w_thin_priv.get("session") == "new"
            and w_thin_priv.get("session_id") != default_id
            and v_priv_home is not None
        )
        # Drive input on the private thin session
        sock_thin_priv.sendall(make_ws_frame(json.dumps({"v": 1, "type": "input", "id": "in:priv", "key": "down"}).encode()))
        v_priv_menu = drain_until(sock_thin_priv, lambda f: f.get("type") == "view" and f.get("screen") == "menu")
        sock_thin_priv.close()
        check(
            "Test 6e: Private thin session (mode=thin, session=new) navigates with own cursor",
            is_private_session and v_priv_menu is not None,
            f"session_new={is_private_session} menu_view={v_priv_menu is not None}",
        )

        # -------------------------------------------------------------------
        # Test 6f: Rich + Mirror dual connection assertion (Item 8)
        # Simultaneous rich private session + thin mirror connection on same user
        # -------------------------------------------------------------------
        log.info("Running Test 6f: Rich + Mirror dual connection on single user session...")
        st_side, sock_side, w_side = browser_connect(
            ck_hdr_a,
            {
                "v": 1, "type": "hello", "id": "h:side_mirror", "mode": "thin", "session": "default", "protocol": [1],
                "client": {"name": "wlanpi-webui-side-mirror"},
            },
        )
        drain_until(sock_side, lambda f: f.get("type") == "view")

        # Simultaneously invoke on sock_a1 (rich private) and input on sock_side (thin mirror)
        sock_a1.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "inv:dual_a", "node": "network.lldp"}).encode()))
        sock_side.sendall(make_ws_frame(json.dumps({"v": 1, "type": "input", "id": "in:dual_side", "key": "up"}).encode()))

        res_dual_rich = drain_until(sock_a1, lambda f: f.get("type") == "job" and f.get("state") == "result")
        v_dual_side = drain_until(sock_side, lambda f: f.get("type") == "view")

        # Assert driven-by indicator on side-mirror socket
        driven_by = v_dual_side.get("driven_by") if v_dual_side else None
        driven_ok = (
            driven_by is not None
            and driven_by.get("self") is True
            and driven_by.get("surface") == "wlanpi-webui-side-mirror"
            and driven_by.get("principal", {}).get("name") == "alice"
        )

        # Independent sockets: sock_a1 must remain open and functional while sock_side is open
        sock_a1.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "inv:dual_after", "node": "network.eth0_ipconfig"}).encode()))
        res_dual_after = drain_until(sock_a1, lambda f: f.get("type") == "job" and f.get("state") == "result")

        dual_conn_ok = (
            w_side.get("session_id") == default_id
            and res_dual_rich is not None
            and v_dual_side is not None
            and driven_ok
            and res_dual_after is not None
        )
        check(
            "Test 6f: Rich + Mirror dual connection: concurrent frames, driven-by indicator, and independent sockets",
            dual_conn_ok,
            f"dual_ok={dual_conn_ok} driven_ok={driven_ok}",
        )

        # -------------------------------------------------------------------
        # Test 7: Real POST /logout closes Alice's sessions and preserves default & Bob
        # -------------------------------------------------------------------
        log.info("Running Test 7: Real POST /logout closes only Alice's sessions...")
        logout_st = http_logout(ck_hdr_a, csrf_a)
        check("Test 7a: POST /logout via nginx returns 302", logout_st in (302, 303, 200), f"status={logout_st}")

        # Alice's Tab 1, Tab 2, and side-mirror must all receive close frame 4401
        c_a1 = drain_until(sock_a1, lambda f: f.get("type") == "close")
        c_a2 = drain_until(sock_a2, lambda f: f.get("type") == "close")
        c_side = drain_until(sock_side, lambda f: f.get("type") == "close")
        check(
            "Test 7b: Tab 1 (alice rich) closed 4401",
            c_a1 is not None and c_a1.get("code") == 4401,
            f"frame={c_a1}",
        )
        check(
            "Test 7c: Tab 2 (alice mirror) closed 4401",
            c_a2 is not None and c_a2.get("code") == 4401,
            f"frame={c_a2}",
        )
        check(
            "Test 7c2: Side mirror (alice thin default) closed 4401 on logout",
            c_side is not None and c_side.get("code") == 4401,
            f"frame={c_side}",
        )
        try:
            sock_side.close()
        except Exception:
            pass

        # Tab 3 (bob) must stay open and usable
        no_close_bob = read_ws_frame(sock_b1, 0.4) is None
        sock_b1.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "inv:b2", "node": "network.interfaces"}).encode()))
        res_b2 = drain_until(sock_b1, lambda f: f.get("type") == "job" and f.get("state") == "result")
        check(
            "Test 7d: Bob's session stays open and usable after Alice logout",
            no_close_bob and res_b2 is not None,
        )

        # TUI still attached and driving default session
        sock_tui.sendall(make_ws_frame(json.dumps({"v": 1, "type": "input", "id": "i:after", "key": "down"}).encode()))
        tui_after = drain_until(sock_tui, lambda f: f.get("type") == "view")
        check("Test 7e: TUI remains attached and driving default session", tui_after is not None)
        check("Test 7f: Default session still present in service", default_id in menud["service"].sessions)

        # New TUI attach gets same default session ID
        st_tui2, _, sock_tui2 = raw_ws_connect_unix(menud_local_sock)
        sock_tui2.sendall(make_ws_frame(json.dumps({"v": 1, "type": "hello", "id": "h:tui2", "mode": "thin", "session": "default", "protocol": [1]}).encode()))
        wt2 = read_ws_frame(sock_tui2)
        check("Test 7g: New TUI attach gets same default session ID", wt2 and wt2.get("session_id") == default_id)

        # Alice's old cookie now gets 401 at /ui/ws
        st_a_old, _, s_a_old = raw_ws_connect("127.0.0.1", 443, "/ui/ws", {"Host": "localhost", "Origin": "https://localhost", "Cookie": ck_hdr_a})
        s_a_old.close()
        check("Test 7h: Alice's old cookie now refused with 401 at /ui/ws", st_a_old == 401, f"status={st_a_old}")

        sock_tui.close()
        sock_tui2.close()
        sock_a1.close()
        sock_a2.close()
        sock_b1.close()

        # -------------------------------------------------------------------
        # Test 8: Real-UID operator on local.sock receives FORBIDDEN on revoke
        # -------------------------------------------------------------------
        log.info("Running Test 8: Real-UID operator on local.sock receives FORBIDDEN on revoke...")
        # Bob connects a browser session
        st_b2, sock_b2, w_b2 = browser_connect(
            ck_hdr_b,
            {"v": 1, "type": "hello", "id": "h:b2", "mode": "rich", "session": "new", "protocol": [1]},
        )
        drain_until(sock_b2, lambda f: f.get("type") == "menu")

        w_op, err_op = run_real_uid_operator_client(
            menud_local_sock,
            OPERATOR_UID,
            OPERATOR_USER,
            sid_b,
        )
        check(
            "Test 8a: Real-UID operator (uid=1001) receives FORBIDDEN on revoke",
            err_op is not None and err_op.get("code") == "FORBIDDEN" and "local admin principals" in err_op.get("message", "").lower(),
            f"code={err_op and err_op.get('code')} msg={err_op and err_op.get('message')}",
        )
        # Bob's socket was NOT closed by operator's forbidden revoke
        no_frame_bob = read_ws_frame(sock_b2, 0.4) is None
        check("Test 8b: Bob's browser session remained open during operator revoke attempt", no_frame_bob)
        sock_b2.close()

        # -------------------------------------------------------------------
        # Test 9: Password change revocation closes live WebSocket with 4401
        # -------------------------------------------------------------------
        log.info("Running Test 9: Password change server-side revocation closes live WebSocket with 4401...")
        name_c, cookie_c, sid_c, csrf_c = mint_session(flask_app, "carol")
        ck_hdr_c = f"{name_c}={cookie_c}"

        st_c, sock_c, w_c = browser_connect(
            ck_hdr_c,
            {"v": 1, "type": "hello", "id": "h:carol", "mode": "rich", "session": "new", "protocol": [1]},
        )
        assert st_c == 101, f"Expected 101 for Carol, got {st_c}"
        drain_until(sock_c, lambda f: f.get("type") == "menu")

        # Simulate password change path: _revoke_user_sessions("carol") runs
        with flask_app.test_request_context("/"):
            wauth._revoke_user_sessions("carol")

        # Carol's live /ui/ws socket must receive close frame 4401
        close_carol = drain_until(sock_c, lambda f: f.get("type") == "close", limit=10, timeout=3.0)
        check(
            "Test 9a: Live /ui/ws socket closes with 4401 on password change revocation",
            close_carol is not None and close_carol.get("code") == 4401,
            f"close_frame={close_carol}",
        )

        # Subsequent invoke fails (socket closed)
        try:
            sock_c.sendall(make_ws_frame(json.dumps({"v": 1, "type": "invoke", "id": "inv:c_after", "node": "network.interfaces"}).encode()))
            res_c_after = drain_until(sock_c, lambda f: f.get("type") == "job", limit=5, timeout=1.0)
        except Exception:
            res_c_after = None
        check("Test 9b: Subsequent action invoke fails on closed socket", res_c_after is None)
        sock_c.close()

        # Carol's old cookie gets 401 at /ui/ws
        st_c_refused, _, s_c_refused = raw_ws_connect("127.0.0.1", 443, "/ui/ws", {"Host": "localhost", "Origin": "https://localhost", "Cookie": ck_hdr_c})
        s_c_refused.close()
        check("Test 9c: Carol's old session cookie returns 401 at /ui/ws", st_c_refused == 401, f"status={st_c_refused}")

        # -------------------------------------------------------------------
        # Test 10: The redesigned /menu page in real headless Chromium
        # (Playwright) through nginx, Flask and menud. Runs last: it restarts
        # menud and logs its user out.
        # -------------------------------------------------------------------
        log.info("Running Test 10: browser-driven menu UI (Playwright, headless Chromium)...")
        name_e, cookie_e, sid_e, csrf_e = mint_session(flask_app, "erin")
        tui = TuiClient(menud_local_sock)

        def on_loop(coro, timeout: float = 30.0):
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

        async def restart(down_ms: int) -> None:
            await stop_menud()
            await asyncio.sleep(down_ms / 1000)
            await start_menud()

        def ctl_restart(query, body):
            tui.close()
            on_loop(restart(int(body.get("down_ms", 3000))))
            return {"epoch": menud["service"].epoch}

        def ctl_attachments(query, body):
            svc = menud["service"]
            found = [
                {"session": s.id, "kind": s.kind, "mode": s.mode, "client": (a.client or {}).get("name")}
                for s in svc.sessions.values() for a in s.attachments
                if a.principal.kind == "webui" and a.principal.name == query.get("user")
            ]
            return {"count": len(found), "attachments": found}

        def ctl_core_set(query, body):
            for k, v in body.items():
                assert hasattr(fake_core, k), f"FakeCore has no {k}"
                setattr(fake_core, k, v)
            return {"ok": True}

        control = start_control_server({
            "GET /invokes": lambda q, b: {"invokes": list(invoke_audit.invokes)},
            "POST /menud/restart": ctl_restart,
            "GET /menud/attachments": ctl_attachments,
            "POST /core": ctl_core_set,
            "GET /core": lambda q, b: {"reboots": fake_core.reboots, "calls": dict(fake_core.calls)},
            "POST /tui/input": lambda q, b: {"view": tui.press(b["key"])},
            "GET /tui/view": lambda q, b: {"view": tui.view(float(q.get("wait", "0.6")))},
        })

        e2e_config = {
            "base": "https://localhost",
            "control": f"http://127.0.0.1:{CONTROL_PORT}",
            "user": "erin",
            "cookie": {"name": name_e, "value": cookie_e},
            "csrf": csrf_e,
        }
        driver = WORKSPACE / "verify-container" / "drive_menu_ui.mjs"
        ui_results: list[dict] = []
        rc = -1
        try:
            proc = subprocess.Popen(
                ["node", str(driver)],
                env={**os.environ, "E2E_CONFIG": json.dumps(e2e_config)},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            killer = threading.Timer(900, proc.kill)
            killer.start()
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith("RESULT "):
                    try:
                        ui_results.append(json.loads(line[len("RESULT "):]))
                    except json.JSONDecodeError:
                        log.error("unreadable driver result: %s", line)
                elif line:
                    log.info("[playwright] %s", line)
            rc = proc.wait()
            killer.cancel()
        finally:
            tui.close()
            control.shutdown()
        for r in ui_results:
            check(f"Test 10 ({r.get('id')}): {r.get('name')}", bool(r.get("ok")), str(r.get("detail", "")))
        check(
            "Test 10: Playwright driver completed and reported its assertions",
            rc == 0 and len(ui_results) >= 40,
            f"rc={rc} assertions={len(ui_results)}",
        )

    try:
        await asyncio.to_thread(run)
    finally:
        log.info("Cleaning up services...")
        await menud["server"].close()
        nginx_proc.terminate()
        nginx_proc.wait(timeout=3)

    log.info("\n" + "=" * 70)
    log.info("CONTAINER VERIFICATION SUMMARY: %d / %d passed", sum(ok for _, ok, _ in results), len(results))
    for name, ok, detail in results:
        log.info("  [%s] %s%s", "PASS" if ok else "FAIL", name, f" [{detail}]" if detail else "")
    log.info("=" * 70)

    if not all(ok for _, ok, _ in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
