#!/usr/bin/env python3
"""Container verification script for wlanpi-menud + wlanpi-webui nginx integration."""

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
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("verifier")

WORKSPACE = Path("/src")
MENUD_PATH = WORKSPACE / "wlanpi-menud"
WEBUI_PATH = WORKSPACE / "wlanpi-webui"

# Ensure menud, webui and their dependencies are importable
sys.path.insert(0, str(MENUD_PATH))
sys.path.insert(0, str(MENUD_PATH / "tests"))
sys.path.insert(0, str(WEBUI_PATH))
sys.path.insert(0, str(MENUD_PATH / ".venv/lib/python3.13/site-packages"))

from fakecore import BASE, FakeCore
from wlanpi_menud.config import Config
from wlanpi_menud.core import CoreClient, TokenSource
from wlanpi_menud.poller import Poller
from wlanpi_menud.server import Server
from wlanpi_menud.service import Service
from wlanpi_webui.menud import revoke_menu_session


# ---------------------------------------------------------------------------
# Dummy WebUI backend for /auth/check over unix socket
# ---------------------------------------------------------------------------

class FakeWebUIServer:
    """A lightweight HTTP server on unix socket answering /auth/check."""

    def __init__(self, sock_path: Path):
        self.sock_path = sock_path
        self._srv: socket.socket | None = None
        self._running = False
        self._thread = None

    def start(self):
        if self.sock_path.exists():
            self.sock_path.unlink()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o666)
        self._srv.listen(16)
        self._running = True
        import threading
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Fake WebUI listening on %s", self.sock_path)

    def _loop(self):
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            self._handle_client(conn)

    def _handle_client(self, conn: socket.socket):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                req += chunk

            headers_text = req.decode("iso-8859-1")
            first_line = headers_text.split("\r\n")[0]
            cookie_header = ""
            for line in headers_text.split("\r\n")[1:]:
                if line.lower().startswith("cookie:"):
                    cookie_header = line.split(":", 1)[1].strip()

            if "session=valid_alice_session" in cookie_header:
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 0\r\n"
                    b"X-WebUI-User: alice\r\n"
                    b"X-WebUI-Session: sess-alice-456\r\n\r\n"
                )
            else:
                resp = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
            conn.sendall(resp)
        except Exception as exc:
            log.warning("Fake WebUI error: %s", exc)
        finally:
            conn.close()

    def stop(self):
        self._running = False
        if self._srv:
            self._srv.close()
        if self.sock_path.exists():
            self.sock_path.unlink()


# ---------------------------------------------------------------------------
# WebSocket Client Helper (Pure TLS + HTTP/1.1 Upgrade + RFC 6455)
# ---------------------------------------------------------------------------

def make_ws_frame(payload: bytes) -> bytes:
    mask = b"\xaa\xbb\xcc\xdd"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length]) + mask
    elif length < 65536:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, length) + mask
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, length) + mask
    return header + masked


def read_ws_frame(sock: socket.socket, timeout: float = 4.0) -> dict | None:
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
    if opcode == 0x08:  # Close frame
        code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
        reason = data[2:].decode("utf-8", "ignore")
        return {"type": "close", "code": code, "reason": reason}
    if opcode == 0x01:  # Text frame
        return json.loads(data.decode("utf-8"))
    return None


def raw_ws_connect(
    host: str,
    port: int,
    path: str,
    headers: dict[str, str],
    timeout: float = 4.0,
) -> tuple[int, dict[str, str], ssl.SSLSocket]:
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

    Uses sudo -u or setuid so SO_PEERCRED carried by the Linux kernel genuinely
    reflects the operator UID. Returns (welcome_frame, error_frame).
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


async def main():
    log.info("Starting container verification...")

    # 1. Generate SSL self-signed certificates for nginx
    ssl_dir = Path("/etc/nginx/ssl")
    ssl_dir.mkdir(parents=True, exist_ok=True)
    cert_path = ssl_dir / "self-signed-wlanpi.cert"
    key_path = ssl_dir / "self-signed-wlanpi.key"
    if not cert_path.exists():
        subprocess.run(
            [
                "openssl", "req", "-x509", "-nodes", "-days", "365",
                "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-subj", "/CN=localhost"
            ],
            check=True,
            capture_output=True,
        )

    # 2. Setup nginx configuration
    nginx_conf = Path("/etc/nginx/nginx.conf")
    shutil.copy(WEBUI_PATH / "install/etc/nginx/nginx.conf", nginx_conf)

    sites_enabled = Path("/etc/nginx/sites-enabled")
    sites_enabled.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        WEBUI_PATH / "install/etc/nginx/wlanpi_webui.conf",
        sites_enabled / "wlanpi_webui.conf",
    )

    webui_conf_d = Path("/etc/wlanpi-webui/nginx/conf.d")
    webui_conf_d.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        WEBUI_PATH / "install/etc/nginx/conf.d/wlanpi-menud.conf",
        webui_conf_d / "wlanpi-menud.conf",
    )

    # Create dummy upstreams for librespeed and grafana so nginx starts cleanly
    dummy_upstreams = """
upstream librespeed { server 127.0.0.1:8081; }
upstream grafana { server 127.0.0.1:3000; }
"""
    # Test nginx configuration
    test_nginx = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test_nginx.returncode != 0:
        log.error("nginx -t failed:\n%s\n%s", test_nginx.stdout, test_nginx.stderr)
        sys.exit(1)
    log.info("nginx -t passed successfully")

    # Start nginx
    nginx_proc = subprocess.Popen(["nginx", "-g", "daemon off;"])
    time.sleep(1)

    # 4. Start fake WebUI on /run/wlanpi_webui.sock
    fake_webui = FakeWebUIServer(Path("/run/wlanpi_webui.sock"))
    fake_webui.start()

    # 5. Start real wlanpi-menud on /run/wlanpi-menud/proxy/proxy.sock with fakecore and real menu
    OPERATOR_UID = 1001
    OPERATOR_USER = "operator"
    # Ensure operator user exists with uid 1001 in 'operator' group
    subprocess.run(["useradd", "-u", str(OPERATOR_UID), "-g", "operator", OPERATOR_USER], capture_output=True)

    fake_core = FakeCore()
    tokens = TokenSource(("get-core-token",), runner=fake_core.token_runner())
    core_client = CoreClient(BASE, tokens, transport=fake_core.transport())

    menud_proxy_sock = Path("/run/wlanpi-menud/proxy/proxy.sock")
    menud_local_sock = Path("/run/wlanpi-menud/local.sock")
    menud_config = Config(
        builtin_dir=MENUD_PATH / "share" / "menu.d",
        proxy_sock=menud_proxy_sock,
        local_sock=menud_local_sock,
        proxy_sock_group="www-data",
        local_uids={os.getuid(): ("admin",), OPERATOR_UID: ("operator",)},
        allowed_origins=(),  # Origin host must match Host header
        validate_outbound=True,
    )
    service = Service(menud_config, core=core_client, tokens=tokens)
    await Poller(service).poll_once()
    server = Server(service)
    await server.start()
    log.info("wlanpi-menud started with fakecore, proxy.sock and local.sock ready")

    # Ensure proxy.sock and local.sock permissions
    try:
        os.chmod(menud_proxy_sock, 0o666)
        os.chmod(menud_local_sock, 0o666)
    except Exception:
        pass

    results = []

    try:
        # -------------------------------------------------------------------
        # Test 1: Valid auth session reaches proxy.sock and authenticates
        # -------------------------------------------------------------------
        log.info("Running Test 1: Valid auth session reaches proxy.sock...")
        status, headers, sock = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "Cookie": "session=valid_alice_session",
            },
        )
        assert status == 101, f"Expected 101 Switching Protocols, got {status}"
        hello = json.dumps({"v": 1, "type": "hello", "id": "h1", "mode": "rich", "protocol": [1]}).encode()
        await asyncio.to_thread(sock.sendall, make_ws_frame(hello))
        frame = await asyncio.to_thread(read_ws_frame, sock)
        assert frame is not None, "Did not receive frame from menud"
        assert frame.get("type") == "welcome", f"Expected welcome frame, got {frame}"
        principal = frame.get("principal", {})
        assert principal.get("kind") == "webui", f"Expected principal kind webui, got {principal}"
        assert principal.get("name") == "alice", f"Expected principal name alice, got {principal}"
        sock.close()
        results.append(("Test 1: Browser-less websocket with valid session reaches proxy.sock", True))
        log.info("--> Test 1 PASSED!")

        # -------------------------------------------------------------------
        # Test 2: Without valid auth session, nginx returns 401
        # -------------------------------------------------------------------
        log.info("Running Test 2: Without valid session, expect 401...")
        status, headers, sock = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
            },
        )
        sock.close()
        assert status == 401, f"Expected status 401, got {status}"
        results.append(("Test 2: Websocket without valid auth returns 401", True))
        log.info("--> Test 2 PASSED!")

        # -------------------------------------------------------------------
        # Test 3: Origin mismatch refused
        # -------------------------------------------------------------------
        log.info("Running Test 3: Origin mismatch refused...")
        status, headers, sock = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://evil.attacker.com",
                "Cookie": "session=valid_alice_session",
            },
        )
        if status == 101:
            frame = await asyncio.to_thread(read_ws_frame, sock)
            sock.close()
            assert frame is not None, "Expected close/error frame on origin mismatch"
            assert frame.get("type") in ("close", "error"), f"Unexpected frame on origin mismatch: {frame}"
            if frame.get("type") == "close":
                assert frame.get("code") == 1008, f"Expected close code 1008, got {frame.get('code')}"
            else:
                assert frame.get("code") == "FORBIDDEN" or frame.get("close_code") == 1008
        else:
            assert status in (403, 1008), f"Unexpected status {status} for origin mismatch"
            sock.close()
        results.append(("Test 3: Origin mismatch is refused (close code 1008)", True))
        log.info("--> Test 3 PASSED!")

        # -------------------------------------------------------------------
        # Test 4: Client-forged X-WebUI-User header cannot survive the proxy
        # -------------------------------------------------------------------
        log.info("Running Test 4: Client-forged X-WebUI-User header...")
        status, headers, sock = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "Cookie": "session=valid_alice_session",
                "X-WebUI-User": "mallory",
                "X-WebUI-Session": "forged_session_id",
            },
        )
        assert status == 101, f"Expected 101, got {status}"
        hello = json.dumps({"v": 1, "type": "hello", "id": "h2", "mode": "rich", "protocol": [1]}).encode()
        await asyncio.to_thread(sock.sendall, make_ws_frame(hello))
        frame = await asyncio.to_thread(read_ws_frame, sock)
        assert frame is not None and frame.get("type") == "welcome", f"Unexpected frame: {frame}"
        principal = frame.get("principal", {})
        assert principal.get("name") == "alice", f"Forged header survived! Name is {principal.get('name')}"
        sock.close()

        status, headers, sock = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "X-WebUI-User": "root",
                "X-WebUI-Session": "fake_admin_session",
            },
        )
        sock.close()
        assert status == 401, f"Expected 401 when forging without session, got {status}"
        results.append(("Test 4: Client-forged X-WebUI-User header overwritten by auth_request", True))
        log.info("--> Test 4 PASSED!")

        # -------------------------------------------------------------------
        # Test 5: Rich mode invokes 3 actions and gets results
        # -------------------------------------------------------------------
        log.info("Running Test 5: Rich mode invokes 3 actions and gets results...")
        status, headers, sock = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "Cookie": "session=valid_alice_session",
            },
        )
        assert status == 101, f"Expected 101, got {status}"
        hello_r = json.dumps({
            "v": 1, "type": "hello", "id": "h:rich", "mode": "rich", "session": "new", "protocol": [1],
            "client": {"name": "wlanpi-webui-test"},
            "capabilities": {"blocks": ["text", "kv", "table", "list", "qr", "status", "progress", "confirm"]}
        }).encode()
        await asyncio.to_thread(sock.sendall, make_ws_frame(hello_r))

        welcome_r = await asyncio.to_thread(read_ws_frame, sock)
        assert welcome_r is not None and welcome_r.get("type") == "welcome"
        menu_r = await asyncio.to_thread(read_ws_frame, sock)
        assert menu_r is not None and menu_r.get("type") == "menu"
        assert len(menu_r.get("nodes", [])) > 0, "Expected non-empty menu nodes"

        async def invoke_and_wait_result(node_id: str, mid: str):
            await asyncio.to_thread(sock.sendall, make_ws_frame(json.dumps({
                "v": 1, "type": "invoke", "id": mid, "node": node_id
            }).encode()))
            while True:
                f = await asyncio.to_thread(read_ws_frame, sock)
                if f and f.get("type") == "job" and f.get("state") == "result":
                    return f
                if f and f.get("type") == "error":
                    raise RuntimeError(f"Invoke failed with error frame: {f}")

        # Action 1: network.interfaces (table view)
        res1 = await invoke_and_wait_result("network.interfaces", "act:1")
        assert res1 and "blocks" in res1.get("result", {})
        assert any(b.get("type") == "table" for b in res1["result"]["blocks"])
        log.info("Action 1 (network.interfaces) -> table blocks received")

        # Action 2: network.eth0_ipconfig (kv view)
        res2 = await invoke_and_wait_result("network.eth0_ipconfig", "act:2")
        assert res2 and "blocks" in res2.get("result", {})
        assert any(b.get("type") == "kv" for b in res2["result"]["blocks"])
        log.info("Action 2 (network.eth0_ipconfig) -> kv blocks received")

        # Action 3: network.lldp (kv view)
        res3 = await invoke_and_wait_result("network.lldp", "act:3")
        assert res3 and "blocks" in res3.get("result", {})
        assert any(b.get("type") == "kv" for b in res3["result"]["blocks"])
        log.info("Action 3 (network.lldp) -> kv blocks received")

        sock.close()
        results.append(("Test 5: Rich mode invokes 3 actions and gets typed view block results", True))
        log.info("--> Test 5 PASSED!")

        # -------------------------------------------------------------------
        # Test 6: Thin attach mirrors a concurrent local.sock TUI session
        # -------------------------------------------------------------------
        log.info("Running Test 6: Thin attach mirrors concurrent local.sock TUI session...")
        status_tui, _, sock_tui = await asyncio.to_thread(raw_ws_connect_unix, menud_local_sock)
        assert status_tui == 101, f"Expected 101 for TUI, got {status_tui}"
        hello_tui = json.dumps({
            "v": 1, "type": "hello", "id": "h:tui", "mode": "thin", "session": "default", "protocol": [1],
            "client": {"name": "wlanpi-menu-tui"}
        }).encode()
        await asyncio.to_thread(sock_tui.sendall, make_ws_frame(hello_tui))
        welcome_tui = await asyncio.to_thread(read_ws_frame, sock_tui)
        assert welcome_tui and welcome_tui.get("type") == "welcome", f"Bad welcome_tui: {welcome_tui}"
        while True:
            f = await asyncio.to_thread(read_ws_frame, sock_tui)
            if f and f.get("type") == "view":
                break

        # Browser connects in thin mode to mirror panel
        status_web, _, sock_web = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "Cookie": "session=valid_alice_session",
            },
        )
        assert status_web == 101, f"Expected 101 for browser mirror, got {status_web}"
        hello_web = json.dumps({
            "v": 1, "type": "hello", "id": "h:mirror", "mode": "thin", "session": "default", "protocol": [1],
            "client": {"name": "wlanpi-webui"}
        }).encode()
        await asyncio.to_thread(sock_web.sendall, make_ws_frame(hello_web))
        welcome_web = await asyncio.to_thread(read_ws_frame, sock_web)
        assert welcome_web and welcome_web.get("type") == "welcome"
        assert welcome_web.get("session_id") == welcome_tui.get("session_id"), "Sessions must match default session"
        while True:
            f = await asyncio.to_thread(read_ws_frame, sock_web)
            if f and f.get("type") == "view":
                break

        # TUI inputs 'down'
        inp_tui = json.dumps({"v": 1, "type": "input", "id": "tui:down", "key": "down"}).encode()
        await asyncio.to_thread(sock_tui.sendall, make_ws_frame(inp_tui))

        # Both TUI and WebUI should receive the updated view frame
        tui_next = await asyncio.to_thread(read_ws_frame, sock_tui)
        assert tui_next and tui_next.get("type") == "view", f"Expected view on TUI, got {tui_next}"
        web_next = await asyncio.to_thread(read_ws_frame, sock_web)
        assert web_next and web_next.get("type") == "view", f"Expected view on WebUI, got {web_next}"
        assert web_next.get("driven_by", {}).get("self") is False, "WebUI should see driven_by.self == False"

        # WebUI inputs 'up'
        inp_web = json.dumps({"v": 1, "type": "input", "id": "web:up", "key": "up"}).encode()
        await asyncio.to_thread(sock_web.sendall, make_ws_frame(inp_web))

        web_center = await asyncio.to_thread(read_ws_frame, sock_web)
        assert web_center and web_center.get("type") == "view", f"Expected view on WebUI, got {web_center}"
        assert web_center.get("driven_by", {}).get("self") is True, "WebUI should see driven_by.self == True"
        tui_center = await asyncio.to_thread(read_ws_frame, sock_tui)
        assert tui_center and tui_center.get("type") == "view", f"Expected view on TUI, got {tui_center}"
        assert tui_center.get("driven_by", {}).get("self") is False, "TUI should see driven_by.self == False"

        sock_tui.close()
        sock_web.close()
        results.append(("Test 6: Thin attach mirrors a concurrent local.sock TUI session", True))
        log.info("--> Test 6 PASSED!")

        # -------------------------------------------------------------------
        # Test 7: Real-UID operator on local.sock receives FORBIDDEN on revoke
        # -------------------------------------------------------------------
        log.info("Running Test 7: Real-UID operator on local.sock receives FORBIDDEN on revoke...")
        status, _, sock_browser = await asyncio.to_thread(
            raw_ws_connect,
            "127.0.0.1", 443, "/ui/ws",
            {
                "Host": "localhost",
                "Origin": "https://localhost",
                "Cookie": "session=valid_alice_session",
            },
        )
        assert status == 101, f"Expected 101, got {status}"
        hello_b = json.dumps({
            "v": 1, "type": "hello", "id": "h:logout_test", "mode": "rich", "session": "new", "protocol": [1],
            "client": {"name": "wlanpi-webui"}
        }).encode()
        await asyncio.to_thread(sock_browser.sendall, make_ws_frame(hello_b))
        w = await asyncio.to_thread(read_ws_frame, sock_browser)
        assert w and w.get("type") == "welcome"
        m = await asyncio.to_thread(read_ws_frame, sock_browser)
        assert m and m.get("type") == "menu"

        # Operator client runs with genuine UID 1001 via sudo -u / setuid
        w_op, err_op = await asyncio.to_thread(
            run_real_uid_operator_client,
            menud_local_sock,
            OPERATOR_UID,
            OPERATOR_USER,
            "sess-alice-456",
        )
        assert w_op and w_op.get("type") == "welcome", f"Unexpected operator welcome: {w_op}"
        assert "operator" in w_op.get("roles", []), f"Operator role missing in {w_op}"
        assert "admin" not in w_op.get("roles", []), f"Operator must not have admin role: {w_op}"
        assert w_op.get("principal", {}).get("uid") == OPERATOR_UID, f"Real UID mismatch: {w_op}"

        assert err_op and err_op.get("type") == "error", f"Expected error frame, got: {err_op}"
        assert err_op.get("code") == "FORBIDDEN", f"Expected FORBIDDEN, got: {err_op.get('code')}"
        assert "local admin principals" in err_op.get("message", "").lower()
        log.info(
            "Authoritative check passed: Real-UID operator (uid=%d) received FORBIDDEN: %s",
            OPERATOR_UID,
            err_op.get("message"),
        )

        # Crucial check: Alice's browser session was NOT closed by operator's forbidden revoke attempt
        no_frame = await asyncio.to_thread(read_ws_frame, sock_browser, 0.4)
        assert no_frame is None, f"Expected session to remain open, but received: {no_frame}"
        results.append(("Test 7: Real-UID operator on local.sock receives FORBIDDEN on revoke", True))
        log.info("--> Test 7 PASSED!")

        # -------------------------------------------------------------------
        # Test 8: Logout / admin session revocation closes the browser session
        # -------------------------------------------------------------------
        log.info("Running Test 8: Admin revoke closes the browser session with 4401...")
        # Local admin (root) calls revoke_menu_session for alice's session ("sess-alice-456")
        revoked = await asyncio.to_thread(
            revoke_menu_session,
            "sess-alice-456",
            sock_path=str(menud_local_sock),
            timeout=2.0,
        )
        assert revoked is True, "Expected revoke_menu_session to return True"

        # The browser connection must receive error/close frame with 4401
        close_frame = None
        while True:
            f = await asyncio.to_thread(read_ws_frame, sock_browser)
            if not f or f.get("type") == "close":
                close_frame = f
                break

        assert close_frame and close_frame.get("type") == "close", f"Expected close frame, got {close_frame}"
        assert close_frame.get("code") == 4401, f"Expected close code 4401, got {close_frame.get('code')}"
        sock_browser.close()
        results.append(("Test 8: WebUI logout / session revocation closes browser session with 4401", True))
        log.info("--> Test 8 PASSED!")

    finally:
        log.info("Cleaning up services...")
        await server.close()
        fake_webui.stop()
        nginx_proc.terminate()
        nginx_proc.wait(timeout=3)

    log.info("\n" + "="*70)
    log.info("CONTAINER VERIFICATION SUMMARY:")
    for desc, ok in results:
        log.info("  [%s] %s", "PASS" if ok else "FAIL", desc)
    log.info("="*70)

    if not all(ok for _, ok in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
