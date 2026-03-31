"""
OpenAI 批量自动注册引擎 V0.6
- Pre-flight Check：启动前全面预检
- Clash API 自动 IP 轮换
- 失败即停策略
"""

import json
import os
import re
import time
import uuid
import random
import string
import secrets
import hashlib
import base64
import threading
import shutil
import http.client
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, quote
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List, Tuple
import urllib.parse
import ssl
import imaplib
import email
import socket
from email.header import decode_header
from email.utils import parsedate_to_datetime
from curl_cffi import requests


# ==========================================
# 日志输出：可插拔回调
# ==========================================

_log_callback: Optional[Callable[[str], None]] = None


def set_log_callback(cb: Optional[Callable[[str], None]]) -> None:
    global _log_callback
    _log_callback = cb


def _print(msg: str) -> None:
    if _log_callback:
        _log_callback(msg)
    else:
        print(msg, flush=True)


# ==========================================
# 线程安全基础设施
# ==========================================

_file_lock = threading.Lock()
_imap_lock = threading.Lock()

CLI_PROXY_AUTHS_DIR = os.environ.get("CLI_PROXY_AUTHS_DIR", "/app/cli-proxy-auths")
MAX_CONSECUTIVE_FAILS = 5

TOKEN_DIR = os.environ.get("TOKEN_OUTPUT_DIR", "tokens")
KEYS_DIR = os.environ.get("KEYS_DIR", "keys")
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "").strip()


def _ssl_verify() -> bool:
    flag = os.environ.get("OPENAI_SSL_VERIFY", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _skip_net_check() -> bool:
    flag = os.environ.get("SKIP_NET_CHECK", "0").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _log_error(msg: str) -> None:
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "errors.log")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _file_lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")


def _copy_token_to_cli_proxy(token_file_path: str) -> None:
    if not os.path.isdir(CLI_PROXY_AUTHS_DIR):
        return
    try:
        dest = os.path.join(CLI_PROXY_AUTHS_DIR, os.path.basename(token_file_path))
        shutil.copy2(token_file_path, dest)
        _print(f"[*] Token 已自动同步至 CLI Proxy")
    except Exception as e:
        _print(f"[!] 同步 Token 到 CLI Proxy 失败: {e}")


class BatchStats:
    def __init__(self, target: int):
        self._lock = threading.Lock()
        self.target = target
        self.success = 0
        self.fail = 0
        self.retry = 0
        self.consecutive_fails = 0
        self.start_time = time.time()
        self._stop = threading.Event()

    def add_success(self) -> int:
        with self._lock:
            self.success += 1
            self.consecutive_fails = 0
            if self.success >= self.target:
                self._stop.set()
            return self.success

    def add_fail(self) -> int:
        with self._lock:
            self.fail += 1
            self.consecutive_fails += 1
            if self.consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                _print(f"[!!!] 连续失败 {self.consecutive_fails} 次，触发熔断")
                _log_error(f"熔断触发: 连续失败 {self.consecutive_fails} 次")
                self._stop.set()
            return self.fail

    def add_retry(self) -> None:
        with self._lock:
            self.retry += 1

    def request_stop(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.target - self.success)

    def to_dict(self) -> dict:
        elapsed = time.time() - self.start_time
        return {
            "target": self.target,
            "success": self.success,
            "fail": self.fail,
            "retry": self.retry,
            "elapsed_seconds": int(elapsed),
            "running": not self._stop.is_set() and self.success < self.target,
        }

    def summary(self) -> str:
        elapsed = time.time() - self.start_time
        mins, secs = divmod(int(elapsed), 60)
        return (
            f"目标: {self.target}, 成功: {self.success}, "
            f"失败: {self.fail}, 重试: {self.retry}, "
            f"耗时: {mins}m{secs}s"
        )


# ==========================================
# Clash API 控制器
# ==========================================

CLASH_API_URL = os.environ.get("CLASH_API_URL", "").strip()
CLASH_API_SECRET = os.environ.get("CLASH_API_SECRET", "").strip()
CLASH_GROUP_NAME = os.environ.get("CLASH_GROUP_NAME", "GLOBAL").strip()

_EXCLUDED_NODE_NAMES = {"DIRECT", "REJECT", "COMPATIBLE", "REJECT-DROP", "PASS"}
_EXCLUDED_NODE_KEYWORDS = ("官网", "禁", "购买", "续费", "到期", "流量", "订阅")
_BLOCKED_REGIONS = ("香港", "HK")


class ClashController:
    """通过 mihomo RESTful API（HTTP）控制 Clash 节点切换"""

    def __init__(self, api_url: str, group_name: str = "GLOBAL", secret: str = ""):
        self._api_url = api_url.rstrip("/")
        self._group = group_name
        self._secret = secret

    def _request(self, method: str, path: str, body: Optional[str] = None) -> Tuple[int, str]:
        parsed = urllib.parse.urlparse(self._api_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5)
        headers: Dict[str, str] = {}
        if body:
            headers["Content-Type"] = "application/json"
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", errors="replace")
        finally:
            conn.close()

    def is_available(self) -> bool:
        if not self._api_url:
            return False
        try:
            status, _ = self._request("GET", "/proxies")
            return status == 200
        except Exception:
            return False

    def get_all_nodes(self) -> Tuple[List[str], str]:
        """返回 (所有节点名列表, 当前选中节点)"""
        status, body = self._request("GET", "/proxies")
        if status != 200:
            return [], ""
        data = json.loads(body)
        group = data.get("proxies", {}).get(self._group, {})
        return group.get("all", []), group.get("now", "")

    def get_usable_nodes(self, filter_pattern: Optional[str] = None) -> List[str]:
        all_nodes, _ = self.get_all_nodes()
        usable = []
        for name in all_nodes:
            if name in _EXCLUDED_NODE_NAMES:
                continue
            if any(kw in name for kw in _EXCLUDED_NODE_KEYWORDS):
                continue
            if any(region in name for region in _BLOCKED_REGIONS):
                continue
            if filter_pattern and filter_pattern not in name:
                continue
            usable.append(name)
        return usable

    def switch_node(self, node_name: str) -> bool:
        encoded_group = urllib.parse.quote(self._group, safe="")
        body = json.dumps({"name": node_name})
        status, _ = self._request("PUT", f"/proxies/{encoded_group}", body)
        return status == 204

    def get_current_node(self) -> str:
        _, now = self.get_all_nodes()
        return now

    def get_current_ip(self, proxy_url: Optional[str] = None) -> Tuple[str, str]:
        """通过 cloudflare trace 获取 (ip, location)"""
        try:
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            resp = requests.get(
                "https://cloudflare.com/cdn-cgi/trace",
                proxies=proxies, impersonate="safari",
                verify=_ssl_verify(), timeout=10,
            )
            ip_match = re.search(r"^ip=(.+)$", resp.text, re.MULTILINE)
            loc_match = re.search(r"^loc=(.+)$", resp.text, re.MULTILINE)
            return (
                ip_match.group(1).strip() if ip_match else "",
                loc_match.group(1).strip() if loc_match else "",
            )
        except Exception:
            return "", ""


# ==========================================
# Pre-flight Check
# ==========================================

def preflight_check(proxy: Optional[str] = None,
                    clash_api_url: Optional[str] = None,
                    clash_group: Optional[str] = None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    mail_domain = os.environ.get("MAIL_DOMAIN", "").strip()
    imap_user = os.environ.get("IMAP_USER", "").strip()
    imap_password = os.environ.get("IMAP_PASSWORD", "").strip()
    env_ok = bool(mail_domain and imap_user and imap_password)
    results.append({
        "name": "环境变量",
        "ok": env_ok,
        "message": f"域名={mail_domain}" if env_ok else "MAIL_DOMAIN / IMAP_USER / IMAP_PASSWORD 未配置",
    })

    dir_ok = True
    dir_msgs = []
    for d in [TOKEN_DIR, KEYS_DIR, "logs"]:
        try:
            os.makedirs(d, exist_ok=True)
            test_f = os.path.join(d, "_preflight_test")
            with open(test_f, "w") as f:
                f.write("ok")
            os.remove(test_f)
        except Exception as e:
            dir_ok = False
            dir_msgs.append(f"{d}: {e}")
    results.append({
        "name": "目录写权限",
        "ok": dir_ok,
        "message": "tokens/keys/logs 可写" if dir_ok else "; ".join(dir_msgs),
    })

    imap_ok, imap_msg = check_imap_connection() if env_ok else (False, "跳过（环境变量未配置）")
    results.append({"name": "IMAP 邮箱", "ok": imap_ok, "message": imap_msg})

    proxy_ip, proxy_loc = "", ""
    proxy_ok = False
    if proxy:
        try:
            proxies = {"http": proxy, "https": proxy}
            trace = requests.get(
                "https://cloudflare.com/cdn-cgi/trace",
                proxies=proxies, impersonate="safari",
                verify=_ssl_verify(), timeout=10,
            )
            ip_m = re.search(r"^ip=(.+)$", trace.text, re.MULTILINE)
            loc_m = re.search(r"^loc=(.+)$", trace.text, re.MULTILINE)
            proxy_ip = ip_m.group(1).strip() if ip_m else ""
            proxy_loc = loc_m.group(1).strip() if loc_m else ""
            if proxy_loc in ("CN", "HK"):
                proxy_ok = False
            else:
                proxy_ok = bool(proxy_ip)
        except Exception as e:
            proxy_ip = str(e)[:60]
    results.append({
        "name": "代理连通",
        "ok": proxy_ok,
        "message": f"IP={proxy_ip} ({proxy_loc})" if proxy_ok else f"失败: {proxy_ip} {proxy_loc}".strip(),
    })

    api_url = clash_api_url or CLASH_API_URL
    if api_url:
        try:
            secret = CLASH_API_SECRET
            ctrl = ClashController(api_url, clash_group or CLASH_GROUP_NAME, secret)
            c_ok = ctrl.is_available()
            if c_ok:
                nodes = ctrl.get_usable_nodes()
                results.append({
                    "name": "Clash API",
                    "ok": True,
                    "message": f"已连接，可用节点 {len(nodes)} 个",
                })
            else:
                results.append({"name": "Clash API", "ok": False, "message": "API 无法连接"})
        except Exception as e:
            results.append({"name": "Clash API", "ok": False, "message": str(e)[:80]})
    else:
        results.append({"name": "Clash API", "ok": True, "message": "未配置（将使用固定 IP）"})

    return results


# ==========================================
# 邮箱生成
# ==========================================

def get_email_and_token(proxies: Any = None) -> tuple:
    if not MAIL_DOMAIN:
        raise RuntimeError("请配置 MAIL_DOMAIN")
    prefix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    if MAIL_DOMAIN.lower() in {"gmail.com", "googlemail.com"}:
        imap_user = os.environ.get("IMAP_USER", "").strip()
        base = imap_user.split("@")[0] if "@" in imap_user else imap_user
        addr = f"{base}+{prefix}@{MAIL_DOMAIN}"
    else:
        addr = f"{prefix}@{MAIL_DOMAIN}"
    return addr, addr


# ==========================================
# IMAP 收码
# ==========================================

def _extract_otp_code(content: str) -> str:
    if not content:
        return ""
    patterns = [
        r"Your ChatGPT code is\s*(\d{6})",
        r"ChatGPT code is\s*(\d{6})",
        r"your verification code is\s*(\d{6})",
        r"verification code to continue:\s*(\d{6})",
        r"verification code[^0-9]{0,20}(\d{6})",
        r"(?:openai|chatgpt)[^0-9]{0,30}(?:code|otp)[^0-9]{0,20}(\d{6})",
        r"验证码[^0-9]{0,20}(\d{6})",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
    return ""


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    parts: List[str] = []
    try:
        for text, charset in decode_header(value):
            if isinstance(text, bytes):
                parts.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(text)
    except Exception:
        return value
    return "".join(parts)


def _message_to_text(msg: Any) -> str:
    chunks: List[str] = []
    for h in ("Subject", "From", "To", "Date"):
        chunks.append(_decode_header_value(msg.get(h, "")))
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
        else:
            chunks.append(str(msg.get_payload() or ""))
    return "\n".join(filter(None, chunks))


def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(str(raw).strip() or str(default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _iter_imap_folders(imap_folder: str, imap_host: str) -> List[str]:
    folders: List[str] = []
    def add(f: str) -> None:
        f = (f or "").strip()
        if f and f not in folders:
            folders.append(f)
    add(imap_folder)
    add("INBOX")
    host = (imap_host or "").lower()
    if "gmail" in host or "googlemail" in host:
        add("[Gmail]/All Mail")
        add("[Google Mail]/All Mail")
        add("[Gmail]/Spam")
        add("[Google Mail]/Spam")
    extra = os.environ.get("IMAP_EXTRA_FOLDERS", "").strip()
    if extra:
        for item in extra.split(","):
            add(item)
    return folders


def _imap_open_folder(imap: Any, folder_name: str) -> bool:
    try:
        status, _ = imap.select(f'"{folder_name}"')
        if status == "OK":
            return True
    except Exception:
        pass
    try:
        status, _ = imap.select(folder_name)
        return status == "OK"
    except Exception:
        return False


def _imap_search_ids(imap: Any, min_date_ts: Optional[float] = None) -> List[bytes]:
    since_str = ""
    if min_date_ts:
        try:
            since_dt = datetime.fromtimestamp(min_date_ts, timezone.utc)
            since_str = since_dt.strftime("%d-%b-%Y")
        except Exception:
            since_str = ""
    queries: List[tuple] = []
    if since_str:
        queries.append(("UNSEEN", "SINCE", since_str))
    queries.append(("UNSEEN",))
    seen: set = set()
    ordered: List[bytes] = []
    for query in queries:
        try:
            status, data = imap.search(None, *query)
        except Exception:
            continue
        if status != "OK" or not data or not data[0]:
            continue
        for msg_id in data[0].split():
            if msg_id not in seen:
                seen.add(msg_id)
                ordered.append(msg_id)

    def _msgid_key(v: bytes) -> int:
        try:
            return int(v)
        except Exception:
            return 0
    return sorted(ordered, key=_msgid_key)


def _looks_like_openai_otp(msg: Any, content: str) -> bool:
    subject = _decode_header_value(msg.get("Subject", "")).lower()
    sender = " ".join([
        _decode_header_value(msg.get("From", "")),
        _decode_header_value(msg.get("Sender", "")),
        _decode_header_value(msg.get("Return-Path", "")),
    ]).lower()
    merged = f"{subject}\n{sender}\n{content}".lower()
    keywords = (
        "chatgpt code", "your chatgpt code", "your verification code is",
        "verification code to continue", "openai", "chatgpt", "email verification",
    )
    return any(kw in merged for kw in keywords)


def _imap_fetch_otp(email_addr: str, min_date_ts: Optional[float] = None,
                    mark_seen: bool = True, thread_safe: bool = False) -> str:
    imap_host = os.environ.get("IMAP_HOST", "").strip()
    imap_user = os.environ.get("IMAP_USER", "").strip() or email_addr
    imap_password = os.environ.get("IMAP_PASSWORD", "").strip()
    imap_folder = os.environ.get("IMAP_FOLDER", "INBOX").strip() or "INBOX"
    imap_ssl = os.environ.get("IMAP_SSL", "1").strip().lower() not in {"0", "false", "no", "off"}
    imap_timeout = _env_int("IMAP_TIMEOUT_SECONDS", 180, minimum=15)
    imap_poll = _env_int("IMAP_POLL_SECONDS", 1, minimum=1)
    imap_lookback = _env_int("IMAP_LOOKBACK_SECONDS", 900, minimum=60)
    imap_connect_timeout = _env_int("IMAP_CONNECT_TIMEOUT_SECONDS", 20, minimum=5)
    imap_login_retries = _env_int("IMAP_LOGIN_RETRIES", 2, minimum=0)
    imap_fetch_limit = _env_int("IMAP_FETCH_LIMIT", 40, minimum=5)
    imap_debug = os.environ.get("IMAP_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
    imap_strict_rcpt = os.environ.get("IMAP_STRICT_RECIPIENT", "0").strip().lower() in {"1", "true", "yes", "on"}
    if thread_safe:
        imap_strict_rcpt = True

    if not imap_host or not imap_password:
        _print("[*] 未配置 IMAP_HOST / IMAP_PASSWORD，跳过自动收码")
        return ""

    default_port = 993 if imap_ssl else 143
    imap_port = int(os.environ.get("IMAP_PORT", str(default_port)) or str(default_port))
    _print(f"[*] 自动收码...{imap_folder}")
    start = time.time()
    checked_ids: set = set()

    hosts_to_try: List[str] = [imap_host]
    if imap_host.lower() in {"imap.gmail.com", "imap.googlemail.com"}:
        alt = "imap.googlemail.com" if imap_host.lower() == "imap.gmail.com" else "imap.gmail.com"
        hosts_to_try.append(alt)

    imap_conn = None
    last_error: Optional[Exception] = None
    for host in hosts_to_try:
        for attempt in range(imap_login_retries + 1):
            try:
                if imap_ssl:
                    imap_conn = imaplib.IMAP4_SSL(host, imap_port,
                                                  ssl_context=_make_imap_ssl_context(),
                                                  timeout=imap_connect_timeout)
                else:
                    imap_conn = imaplib.IMAP4(host, imap_port, timeout=imap_connect_timeout)
                imap_conn.login(imap_user, imap_password)
                imap_conn.select(imap_folder)
                last_error = None
                break
            except (imaplib.IMAP4.error, socket.timeout, TimeoutError, OSError) as e:
                last_error = e
                _print(f"[Warn] IMAP 连接失败({host}) 第 {attempt + 1}/{imap_login_retries + 1} 次: {e}")
                try:
                    if imap_conn is not None:
                        imap_conn.logout()
                except Exception:
                    pass
                imap_conn = None
                if attempt < imap_login_retries:
                    time.sleep(min(3, 1 + attempt))
        if imap_conn is not None:
            break

    if imap_conn is None:
        _print(f"[Error] IMAP 连接失败: {last_error}")
        return ""

    try:
        folders = _iter_imap_folders(imap_folder, imap_host)
        time.sleep(imap_poll)
        while time.time() - start < imap_timeout:
            for folder_name in folders:
                if not _imap_open_folder(imap_conn, folder_name):
                    continue
                ids = _imap_search_ids(imap_conn, min_date_ts=min_date_ts)
                if imap_debug:
                    _print(f"[*] IMAP [{folder_name}] 未读: {len(ids)}")
                for msg_id in reversed(ids[-imap_fetch_limit:]):
                    cache_key = (folder_name, msg_id)
                    if cache_key in checked_ids:
                        continue
                    checked_ids.add(cache_key)

                    f_status, f_data = imap_conn.fetch(msg_id, "(BODY.PEEK[])")
                    if f_status != "OK" or not f_data:
                        continue
                    raw_bytes = b""
                    for row in f_data:
                        if isinstance(row, tuple) and len(row) > 1 and isinstance(row[1], bytes):
                            raw_bytes = row[1]
                            break
                    if not raw_bytes:
                        continue
                    msg = email.message_from_bytes(raw_bytes)

                    try:
                        date_hdr = msg.get("Date", "")
                        dt = parsedate_to_datetime(date_hdr)
                        if dt is not None:
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            dt_utc = dt.astimezone(timezone.utc)
                            age_seconds = (datetime.now(timezone.utc) - dt_utc).total_seconds()
                            if age_seconds > imap_lookback:
                                continue
                            if min_date_ts is not None and dt_utc.timestamp() < (min_date_ts - 15):
                                continue
                    except Exception:
                        pass

                    rcpt_hdr = " ".join([
                        _decode_header_value(msg.get("To", "")),
                        _decode_header_value(msg.get("Delivered-To", "")),
                        _decode_header_value(msg.get("X-Original-To", "")),
                        _decode_header_value(msg.get("Envelope-To", "")),
                    ]).lower()
                    if imap_strict_rcpt and email_addr and rcpt_hdr and email_addr.lower() not in rcpt_hdr:
                        continue

                    content = _message_to_text(msg)
                    code = _extract_otp_code(content)
                    if not code:
                        continue
                    if not _looks_like_openai_otp(msg, content):
                        continue

                    _print(f"[*] IMAP 自动获取验证码成功: {code} (folder={folder_name})")
                    if mark_seen:
                        try:
                            imap_conn.store(msg_id, "+FLAGS", "\\Seen")
                        except Exception:
                            pass
                    return code
            time.sleep(imap_poll)
    finally:
        try:
            imap_conn.logout()
        except Exception:
            pass

    _print("[*] IMAP 超时，未自动获取到验证码")
    return ""


def _make_imap_ssl_context() -> ssl.SSLContext:
    """创建兼容性更好的 IMAP SSL 上下文，避免 UNEXPECTED_EOF_WHILE_READING"""
    if _ssl_verify():
        return ssl.create_default_context()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


def check_imap_connection() -> tuple:
    """测试 IMAP 连接，返回 (ok: bool, message: str)"""
    imap_host = os.environ.get("IMAP_HOST", "").strip()
    imap_user = os.environ.get("IMAP_USER", "").strip()
    imap_password = os.environ.get("IMAP_PASSWORD", "").strip()
    if not imap_host or not imap_user or not imap_password:
        return False, "IMAP 未配置"
    imap_ssl = os.environ.get("IMAP_SSL", "1").strip().lower() not in {"0", "false", "no", "off"}
    default_port = 993 if imap_ssl else 143
    imap_port = int(os.environ.get("IMAP_PORT", str(default_port)) or str(default_port))
    conn = None
    try:
        if imap_ssl:
            conn = imaplib.IMAP4_SSL(imap_host, imap_port,
                                     ssl_context=_make_imap_ssl_context(), timeout=10)
        else:
            conn = imaplib.IMAP4(imap_host, imap_port, timeout=10)
        conn.login(imap_user, imap_password)
        try:
            conn.logout()
        except Exception:
            pass
        return True, "连接成功"
    except Exception as e:
        try:
            if conn is not None:
                conn.logout()
        except Exception:
            pass
        return False, str(e)


# ==========================================
# OAuth 授权
# ==========================================

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def _sha256_b64url_no_pad(s: str) -> str:
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())

def _random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)

def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _parse_callback_url(callback_url: str) -> Dict[str, Any]:
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}
    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif any(ch in candidate for ch in "/?#") or ":" in candidate:
            candidate = f"http://{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for key, values in fragment.items():
        if key not in query or not query[key] or not (query[key][0] or "").strip():
            query[key] = values
    def get1(k: str) -> str:
        v = query.get(k, [""])
        return (v[0] or "").strip()
    code = get1("code")
    state = get1("state")
    error = get1("error")
    error_description = get1("error_description")
    if code and not state and "#" in code:
        code, state = code.split("#", 1)
    if not error and error_description:
        error, error_description = error_description, ""
    return {"code": code, "state": state, "error": error, "error_description": error_description}


def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _decode_jwt_segment(seg: str) -> Dict[str, Any]:
    raw = (seg or "").strip()
    if not raw:
        return {}
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    try:
        decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _post_form(url: str, data: Dict[str, str], proxies: Any = None,
               timeout: int = 30) -> Dict[str, Any]:
    resp = requests.post(
        url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        proxies=proxies, impersonate="safari",
        verify=_ssl_verify(), timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed: {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _post_with_retry(session: requests.Session, url: str, *, headers: Dict[str, Any],
                     data: Any = None, json_body: Any = None, proxies: Any = None,
                     timeout: int = 30, retries: int = 2) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if json_body is not None:
                return session.post(url, headers=headers, json=json_body, proxies=proxies,
                                    verify=_ssl_verify(), timeout=timeout)
            return session.post(url, headers=headers, data=data, proxies=proxies,
                                verify=_ssl_verify(), timeout=timeout)
        except Exception as e:
            last_error = e
            if attempt >= retries:
                break
            time.sleep(2 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("Request failed without exception")


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def generate_oauth_url(*, redirect_uri: str = DEFAULT_REDIRECT_URI,
                       scope: str = DEFAULT_SCOPE) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": scope, "state": state, "code_challenge": code_challenge,
        "code_challenge_method": "S256", "prompt": "login",
        "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    return OAuthStart(auth_url=auth_url, state=state, code_verifier=code_verifier, redirect_uri=redirect_uri)


def submit_callback_url(*, callback_url: str, expected_state: str,
                        code_verifier: str, redirect_uri: str = DEFAULT_REDIRECT_URI,
                        proxies: Any = None) -> str:
    cb = _parse_callback_url(callback_url)
    if cb["error"]:
        raise RuntimeError(f"oauth error: {cb['error']}: {cb['error_description']}".strip())
    if not cb["code"]:
        raise ValueError("callback url missing ?code=")
    if not cb["state"]:
        raise ValueError("callback url missing ?state=")
    if cb["state"] != expected_state:
        raise ValueError("state mismatch")
    token_resp = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": CLIENT_ID,
        "code": cb["code"], "redirect_uri": redirect_uri, "code_verifier": code_verifier,
    }, proxies=proxies)
    access_token = (token_resp.get("access_token") or "").strip()
    refresh_token = (token_resp.get("refresh_token") or "").strip()
    id_token = (token_resp.get("id_token") or "").strip()
    expires_in = _to_int(token_resp.get("expires_in"))
    claims = _jwt_claims_no_verify(id_token)
    acct_email = str(claims.get("email") or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()
    now = int(time.time())
    expired_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0)))
    now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    config = {
        "id_token": id_token, "access_token": access_token, "refresh_token": refresh_token,
        "account_id": account_id, "last_refresh": now_rfc3339, "email": acct_email,
        "type": "codex", "expired": expired_rfc3339,
    }
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


# ==========================================
# 核心注册逻辑
# ==========================================

def _generate_password(length: int = 16) -> str:
    upper = random.choices(string.ascii_uppercase, k=2)
    lower = random.choices(string.ascii_lowercase, k=2)
    digits = random.choices(string.digits, k=2)
    specials = random.choices("!@#$%&*", k=2)
    rest = random.choices(string.ascii_letters + string.digits + "!@#$%&*", k=length - 8)
    chars = upper + lower + digits + specials + rest
    random.shuffle(chars)
    return "".join(chars)


def run(proxy: Optional[str], thread_safe: bool = False,
        skip_net_check: bool = False) -> tuple:
    proxies: Any = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    s = requests.Session(proxies=proxies, impersonate="safari")

    if not skip_net_check and not _skip_net_check():
        try:
            trace = s.get("https://cloudflare.com/cdn-cgi/trace", proxies=proxies,
                          verify=_ssl_verify(), timeout=10)
            loc_re = re.search(r"^loc=(.+)$", trace.text, re.MULTILINE)
            loc = loc_re.group(1) if loc_re else None
            _print(f"[*] 当前 IP 所在地: {loc}")
            if loc in ("CN", "HK"):
                raise RuntimeError("检查代理 - 所在地不支持")
        except Exception as e:
            _print(f"[Error] 网络连接检查失败: {e}")
            return None, None

    addr, _ = get_email_and_token(proxies)
    if not addr:
        return None, None
    _print(f"[*] 注册邮箱: {addr}")

    oauth = generate_oauth_url()
    url = oauth.auth_url

    try:
        resp = s.get(url, proxies=proxies, verify=_ssl_verify(), timeout=15)
        did = s.cookies.get("oai-did")

        signup_body = f'{{"username":{{"value":"{addr}","kind":"email"}},"screen_hint":"signup"}}'
        sen_req_body = f'{{"p":"","id":"{did}","flow":"authorize_continue"}}'
        sen_resp = requests.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers={"origin": "https://sentinel.openai.com",
                     "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                     "content-type": "text/plain;charset=UTF-8"},
            data=sen_req_body, proxies=proxies, impersonate="safari",
            verify=_ssl_verify(), timeout=15,
        )
        if sen_resp.status_code != 200:
            _print(f"[Error] Sentinel 异常拦截: {sen_resp.status_code}")
            return None, None

        sen_token = sen_resp.json()["token"]
        sentinel = f'{{"p": "", "t": "", "c": "{sen_token}", "id": "{did}", "flow": "authorize_continue"}}'

        signup_resp = s.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers={"referer": "https://auth.openai.com/create-account", "accept": "application/json",
                     "content-type": "application/json", "openai-sentinel-token": sentinel},
            data=signup_body, proxies=proxies, verify=_ssl_verify(),
        )
        _print(f"[*] 提交注册表单状态: {signup_resp.status_code}")
        if signup_resp.status_code == 403:
            return "retry_403", None
        if signup_resp.status_code != 200:
            _print(f"[Error] 注册表单失败: {signup_resp.text[:200]}")
            return None, None

        password = _generate_password()
        register_body = json.dumps({"password": password, "username": addr})
        _print(f"[*] 生成随机密码: {password[:4]}****")

        pwd_resp = s.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers={"referer": "https://auth.openai.com/create-account/password", "accept": "application/json",
                     "content-type": "application/json", "openai-sentinel-token": sentinel},
            data=register_body, proxies=proxies, verify=_ssl_verify(),
        )
        _print(f"[*] 提交注册(密码)状态: {pwd_resp.status_code}")
        if pwd_resp.status_code != 200:
            _print(pwd_resp.text[:300])
            return None, None

        try:
            register_json = pwd_resp.json()
            register_continue = register_json.get("continue_url", "")
            register_page = (register_json.get("page") or {}).get("type", "")
        except Exception:
            register_continue = ""
            register_page = ""

        need_otp = "email-verification" in register_continue or "verify" in register_continue
        if not need_otp and register_page:
            need_otp = "verification" in register_page or "otp" in register_page

        if need_otp:
            _print("[*] 邮箱验证，等待验证码...")
            otp_sent_at = time.time()
            if register_continue:
                otp_send_url = register_continue
                if not otp_send_url.startswith("http"):
                    otp_send_url = f"https://auth.openai.com{otp_send_url}"
                otp_send_resp = _post_with_retry(s, otp_send_url, headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json", "content-type": "application/json",
                    "openai-sentinel-token": sentinel,
                }, json_body={}, proxies=proxies, timeout=30, retries=2)
                _print(f"[*] OTP 状态: {otp_send_resp.status_code}")
                if otp_send_resp.status_code == 200:
                    otp_sent_at = time.time()

            code = _imap_fetch_otp(addr, min_date_ts=otp_sent_at, mark_seen=True, thread_safe=thread_safe)
            if not code:
                _print("[Error] 未获取到验证码")
                return None, None

            _print("[*] 验证码校验...")
            code_resp = _post_with_retry(s, "https://auth.openai.com/api/accounts/email-otp/validate", headers={
                "referer": "https://auth.openai.com/email-verification",
                "accept": "application/json", "content-type": "application/json",
                "openai-sentinel-token": sentinel,
            }, json_body={"code": code}, proxies=proxies, timeout=30, retries=2)
            _print(f"[*] 验证码校验状态: {code_resp.status_code}")
            if code_resp.status_code != 200:
                _print(code_resp.text[:300])

        s.get("https://auth.openai.com/about-you", proxies=proxies, verify=_ssl_verify(), timeout=15)

        ca_did = s.cookies.get("oai-did") or did
        ca_sen_resp = requests.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers={"origin": "https://sentinel.openai.com",
                     "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                     "content-type": "text/plain;charset=UTF-8"},
            data=json.dumps({"p": "", "id": ca_did, "flow": "oauth_create_account"}),
            proxies=proxies, impersonate="safari", verify=_ssl_verify(), timeout=15,
        )
        ca_sentinel = ""
        if ca_sen_resp.status_code == 200:
            ca_token = str((ca_sen_resp.json() or {}).get("token") or "").strip()
            if ca_token:
                ca_sentinel = json.dumps(
                    {"p": "", "t": "", "c": ca_token, "id": ca_did, "flow": "oauth_create_account"},
                    ensure_ascii=False, separators=(",", ":"))

        ca_headers = {"referer": "https://auth.openai.com/about-you",
                      "accept": "application/json", "content-type": "application/json"}
        if ca_sentinel:
            ca_headers["openai-sentinel-token"] = ca_sentinel

        _print("[*] 开始创建账户...")
        create_resp = _post_with_retry(s, "https://auth.openai.com/api/accounts/create_account",
                                       headers=ca_headers, data='{"name":"Neo","birthdate":"2000-02-20"}',
                                       proxies=proxies, timeout=30, retries=2)
        _print(f"[*] 账户创建状态: {create_resp.status_code}")

        user_already_exists = False
        if create_resp.status_code != 200:
            try:
                err_code = (create_resp.json().get("error") or {}).get("code", "")
                user_already_exists = err_code == "user_already_exists"
            except Exception:
                pass
            if not user_already_exists:
                _print(create_resp.text[:300])
                return None, None
            _print("[*] 账号已存在，切换到登录流程...")

        continue_url = ""
        if not user_already_exists:
            try:
                raw_continue = str(create_resp.json().get("continue_url") or "").strip()
            except Exception:
                raw_continue = ""
            is_intermediate = any(kw in raw_continue for kw in [
                "add-phone", "verify-phone", "consent", "about-you", "terms",
            ]) if raw_continue else True
            if raw_continue and not is_intermediate:
                continue_url = raw_continue

        if user_already_exists or not continue_url:
            _print("[*] 完成 OAuth 授权...")
            login_oauth = generate_oauth_url()
            login_s = requests.Session(proxies=proxies, impersonate="safari")
            login_s.get(login_oauth.auth_url, proxies=proxies, verify=_ssl_verify(), timeout=15)
            login_did = login_s.cookies.get("oai-did")

            login_sen_resp = requests.post(
                "https://sentinel.openai.com/backend-api/sentinel/req",
                headers={"origin": "https://sentinel.openai.com",
                         "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                         "content-type": "text/plain;charset=UTF-8"},
                data=f'{{"p":"","id":"{login_did}","flow":"authorize_continue"}}',
                proxies=proxies, impersonate="safari", verify=_ssl_verify(), timeout=15,
            )
            if login_sen_resp.status_code != 200:
                _print(f"[Error] 登录 Sentinel 失败: {login_sen_resp.status_code}")
                return None, None
            login_sen_token = login_sen_resp.json()["token"]
            login_sentinel = f'{{"p": "", "t": "", "c": "{login_sen_token}", "id": "{login_did}", "flow": "authorize_continue"}}'

            login_email_body = json.dumps({"username": {"value": addr, "kind": "email"}})
            login_email_resp = login_s.post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                headers={"referer": "https://auth.openai.com/log-in", "accept": "application/json",
                         "content-type": "application/json", "openai-sentinel-token": login_sentinel},
                data=login_email_body, proxies=proxies, verify=_ssl_verify(),
            )
            _print(f"[*] 提交邮箱状态: {login_email_resp.status_code}")
            if login_email_resp.status_code != 200:
                return None, None

            try:
                pwd_page_url = str(login_email_resp.json().get("continue_url") or "").strip()
            except Exception:
                pwd_page_url = ""
            if pwd_page_url:
                login_s.get(pwd_page_url, proxies=proxies, verify=_ssl_verify(), timeout=15)

            login_did2 = login_s.cookies.get("oai-did") or login_did
            pwd_sen_resp = requests.post(
                "https://sentinel.openai.com/backend-api/sentinel/req",
                headers={"origin": "https://sentinel.openai.com",
                         "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                         "content-type": "text/plain;charset=UTF-8"},
                data=json.dumps({"p": "", "id": login_did2, "flow": "password_verify"}),
                proxies=proxies, impersonate="safari", verify=_ssl_verify(), timeout=15,
            )
            pwd_sentinel = ""
            if pwd_sen_resp.status_code == 200:
                pwd_sen_token = str((pwd_sen_resp.json() or {}).get("token") or "").strip()
                if pwd_sen_token:
                    pwd_sentinel = json.dumps(
                        {"p": "", "t": "", "c": pwd_sen_token, "id": login_did2, "flow": "password_verify"},
                        ensure_ascii=False, separators=(",", ":"))

            pwd_headers = {"referer": "https://auth.openai.com/log-in/password",
                           "accept": "application/json", "content-type": "application/json"}
            if pwd_sentinel:
                pwd_headers["openai-sentinel-token"] = pwd_sentinel
            login_pwd_resp = login_s.post(
                "https://auth.openai.com/api/accounts/password/verify",
                headers=pwd_headers, data=json.dumps({"password": password}),
                proxies=proxies, verify=_ssl_verify(),
            )
            _print(f"[*] 提交密码状态: {login_pwd_resp.status_code}")
            if login_pwd_resp.status_code != 200:
                return None, None

            try:
                login_json = login_pwd_resp.json()
                continue_url = str(login_json.get("continue_url") or "").strip()
                if not continue_url:
                    page_type = str((login_json.get("page") or {}).get("type") or "").strip()
                    page_mapping = {
                        "sign_in_with_chatgpt_codex_consent": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                        "sign_in_with_chatgpt_codex_org": "https://auth.openai.com/sign-in-with-chatgpt/codex/organization",
                        "workspace": "https://auth.openai.com/workspace",
                    }
                    continue_url = page_mapping.get(page_type, "")
                if continue_url:
                    _print(f"[*] 获得 continue_url: {continue_url[:80]}")
            except Exception:
                pass

            if continue_url and "email-verification" in continue_url:
                _print("[*] 等待登录验证码...")
                login_otp_sent_at = time.time()
                otp_send_resp = _post_with_retry(login_s, "https://auth.openai.com/api/accounts/email-otp/send",
                    headers={"referer": "https://auth.openai.com/email-verification",
                             "accept": "application/json", "content-type": "application/json"},
                    json_body={}, proxies=proxies, timeout=15, retries=2)
                _print(f"[*] 登录 OTP 发送状态: {otp_send_resp.status_code}")
                if otp_send_resp.status_code == 200:
                    login_otp_sent_at = time.time()

                # 等待 5 秒确保新 OTP 邮件到达，避免抓到注册阶段的旧验证码
                time.sleep(5)

                login_code = _imap_fetch_otp(addr, min_date_ts=login_otp_sent_at, mark_seen=True, thread_safe=thread_safe)
                if not login_code:
                    _print("[Error] 未获取到登录验证码")
                    return None, None

                _print("[*] 登录验证码校验...")
                login_otp_resp = _post_with_retry(login_s, "https://auth.openai.com/api/accounts/email-otp/validate",
                    headers={"referer": "https://auth.openai.com/email-verification",
                             "accept": "application/json", "content-type": "application/json"},
                    json_body={"code": login_code}, proxies=proxies, timeout=15, retries=2)
                _print(f"[*] 登录验证码校验状态: {login_otp_resp.status_code}")
                if login_otp_resp.status_code != 200:
                    return None, None
                try:
                    otp_json = login_otp_resp.json()
                    continue_url = str(otp_json.get("continue_url") or "").strip()
                    if not continue_url:
                        page_type = str((otp_json.get("page") or {}).get("type") or "").strip()
                        page_mapping = {
                            "sign_in_with_chatgpt_codex_consent": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                            "workspace": "https://auth.openai.com/workspace",
                        }
                        continue_url = page_mapping.get(page_type, "")
                    if continue_url:
                        _print("[*] 登录验证通过")
                except Exception:
                    pass

            if not continue_url:
                auth_cookie = login_s.cookies.get("oai-client-auth-session")
                if auth_cookie:
                    workspace_id = ""
                    for seg in auth_cookie.split("."):
                        decoded = _decode_jwt_segment(seg)
                        ws = (decoded.get("workspaces") or []) if decoded else []
                        if ws:
                            workspace_id = str((ws[0] or {}).get("id") or "").strip()
                            break
                    if workspace_id:
                        select_resp = _post_with_retry(login_s,
                            "https://auth.openai.com/api/accounts/workspace/select",
                            headers={"referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                                     "content-type": "application/json"},
                            data=f'{{"workspace_id":"{workspace_id}"}}',
                            proxies=proxies, timeout=30, retries=2)
                        if select_resp.status_code == 200:
                            continue_url = str((select_resp.json() or {}).get("continue_url") or "").strip()

            if not continue_url:
                _print("[Error] 登录后未获取 continue_url")
                return None, None

            s = login_s
            oauth = login_oauth

        if not continue_url:
            _print("[Error] 未获取 continue_url")
            return None, None

        # ====== 处理中间页面循环 (add-phone / consent / workspace 等) ======
        _intermediate_keywords = ["add-phone", "verify-phone", "consent", "about-you", "terms", "/workspace"]
        for _mid_step in range(5):
            if not continue_url:
                _print("[Error] 中间步骤丢失 continue_url")
                return None, None

            # --- add-phone / verify-phone: 用已认证 session 重新发起 OAuth ---
            if "add-phone" in continue_url or "verify-phone" in continue_url:
                _print("[*] 检测到手机验证页面，使用已认证 session 重新发起 OAuth...")
                # 先 GET add-phone 页面以加载 session 状态
                s.get(continue_url, proxies=proxies, verify=_ssl_verify(), timeout=15)

                # 方案1: 用当前已认证 session 发起新的 OAuth authorize 请求
                # 由于 session cookies 中已有认证信息，OAuth 应该直接 redirect 到 callback
                re_oauth = generate_oauth_url()
                _print(f"[*] 重新发起 OAuth 授权...")
                re_resp = s.get(re_oauth.auth_url, allow_redirects=False,
                                proxies=proxies, verify=_ssl_verify(), timeout=20)

                # 追踪重定向链，寻找 callback URL
                re_url = re_oauth.auth_url
                for re_i in range(15):
                    location = re_resp.headers.get("Location") or ""
                    _print(f"[*] re-OAuth #{re_i+1}: status={re_resp.status_code}, loc={location[:120] if location else '(无)'}")

                    if re_resp.status_code in [301, 302, 303, 307, 308] and location:
                        next_url = urllib.parse.urljoin(re_url, location)
                        if "code=" in next_url and "state=" in next_url:
                            _print("[*] 在 re-OAuth 重定向中捕获到 Callback URL!")
                            token_json = submit_callback_url(
                                callback_url=next_url, code_verifier=re_oauth.code_verifier,
                                redirect_uri=re_oauth.redirect_uri, expected_state=re_oauth.state,
                                proxies=proxies)
                            return token_json, password
                        re_url = next_url
                        re_resp = s.get(re_url, allow_redirects=False,
                                        proxies=proxies, verify=_ssl_verify(), timeout=20)
                        continue

                    # 非重定向响应，检查 body 中是否有 callback
                    if re_resp.status_code == 200:
                        try:
                            re_body = re_resp.text[:2000]
                        except Exception:
                            re_body = ""
                        code_match = re.search(r'["\']?(https?://[^"\'\s]*code=[^"\'\s]*)["\']?', re_body)
                        if code_match:
                            cb_url = code_match.group(1)
                            if "state=" in cb_url:
                                _print("[*] 在 re-OAuth body 中找到 Callback URL!")
                                token_json = submit_callback_url(
                                    callback_url=cb_url, code_verifier=re_oauth.code_verifier,
                                    redirect_uri=re_oauth.redirect_uri, expected_state=re_oauth.state,
                                    proxies=proxies)
                                return token_json, password

                        # 检查返回的是否是中间页面，可能需要重新走
                        try:
                            re_json = re_resp.json()
                            new_url = str(re_json.get("continue_url") or "").strip()
                            if new_url and new_url != continue_url:
                                _print(f"[*] re-OAuth 返回新 continue_url: {new_url[:80]}")
                                continue_url = new_url
                                break
                        except Exception:
                            pass
                    break

                # 如果 re-OAuth 拿到了新的非 add-phone URL，继续中间页面循环
                if continue_url and "add-phone" not in continue_url and "verify-phone" not in continue_url:
                    if not any(kw in continue_url for kw in _intermediate_keywords):
                        break
                    continue

                # 方案2: 尝试 API 跳过
                _print("[*] re-OAuth 未成功，尝试 API 跳过...")
                skip_endpoints = [
                    ("POST", "https://auth.openai.com/api/accounts/phone/skip", {}),
                    ("POST", "https://auth.openai.com/api/accounts/phone/later", {}),
                ]
                for method, skip_url, skip_body in skip_endpoints:
                    try:
                        skip_resp = s.post(skip_url, headers={
                            "referer": "https://auth.openai.com/add-phone",
                            "accept": "application/json", "content-type": "application/json",
                        }, json=skip_body, proxies=proxies, verify=_ssl_verify(), timeout=15)
                        _print(f"[*] 跳过 ({skip_url.split('/')[-1]}): {skip_resp.status_code}")
                        if skip_resp.status_code == 200:
                            try:
                                new_url = str(skip_resp.json().get("continue_url") or "").strip()
                                if new_url and "log-in-or-create" not in new_url:
                                    continue_url = new_url
                                    _print(f"[*] 跳过成功: {continue_url[:80]}")
                                    if not any(kw in continue_url for kw in _intermediate_keywords):
                                        break
                            except Exception:
                                pass
                    except Exception:
                        continue

                if continue_url and not any(kw in continue_url for kw in _intermediate_keywords):
                    if "log-in-or-create" not in continue_url:
                        break

                _print("[Error] OpenAI 要求手机验证且无法跳过")
                return None, None

            # --- codex/consent 或 workspace 选择 ---
            if any(kw in continue_url for kw in ["codex/consent", "/workspace"]):
                _print("[*] 进入 workspace 选择流程...")
                s.get(continue_url, proxies=proxies, verify=_ssl_verify(), timeout=15)
                auth_cookie = s.cookies.get("oai-client-auth-session")
                workspace_id = ""
                if auth_cookie:
                    for seg in auth_cookie.split("."):
                        decoded = _decode_jwt_segment(seg)
                        ws = (decoded.get("workspaces") or []) if decoded else []
                        if ws:
                            workspace_id = str((ws[0] or {}).get("id") or "").strip()
                            break
                if not workspace_id:
                    _print("[Error] Cookie 中无 workspace 信息")
                    return None, None

                select_resp = _post_with_retry(s, "https://auth.openai.com/api/accounts/workspace/select",
                    headers={"referer": continue_url, "accept": "application/json", "content-type": "application/json"},
                    data=json.dumps({"workspace_id": workspace_id}), proxies=proxies, timeout=30, retries=2)
                _print(f"[*] workspace/select 状态: {select_resp.status_code}")
                if select_resp.status_code != 200:
                    return None, None
                try:
                    continue_url = str(select_resp.json().get("continue_url") or "").strip()
                    if continue_url:
                        _print("[*] workspace 选择成功")
                except Exception:
                    pass
                if not continue_url:
                    _print("[Error] workspace/select 缺少 continue_url")
                    return None, None

                if not any(kw in continue_url for kw in _intermediate_keywords):
                    break
                continue

            # --- 其他中间页面 (about-you / terms / consent) ---
            if any(kw in continue_url for kw in ["about-you", "terms"]):
                _print(f"[*] 中间页面: {continue_url.split('/')[-1]}, 尝试推进...")
                s.get(continue_url, proxies=proxies, verify=_ssl_verify(), timeout=15)
                try:
                    skip_resp = s.post("https://auth.openai.com/api/accounts/authorize/continue",
                        headers={"referer": continue_url, "accept": "application/json",
                                 "content-type": "application/json"},
                        json={}, proxies=proxies, verify=_ssl_verify(), timeout=15)
                    if skip_resp.status_code == 200:
                        new_url = str(skip_resp.json().get("continue_url") or "").strip()
                        if new_url:
                            continue_url = new_url
                            if not any(kw in continue_url for kw in _intermediate_keywords):
                                break
                            continue
                except Exception:
                    pass
                break

            # 不是已知中间页面，跳出
            break

        current_url = continue_url
        _print(f"[*] 开始重定向追踪... (起始 URL: {continue_url[:80]})")
        for redir_i in range(15):
            try:
                final_resp = s.get(current_url, allow_redirects=False, proxies=proxies,
                                   verify=_ssl_verify(), timeout=20)
            except Exception as e:
                _print(f"[Warn] 重定向第{redir_i+1}跳请求失败: {e}")
                break
            location = final_resp.headers.get("Location") or ""
            _print(f"[*] 重定向#{redir_i+1}: status={final_resp.status_code}, location={location[:120] if location else '(无)'}")
            if final_resp.status_code not in [301, 302, 303, 307, 308]:
                body_text = ""
                try:
                    body_text = final_resp.text[:2000]
                except Exception:
                    pass
                # 扩展回调URL匹配：同时检查 body 中的 code= 和 meta refresh
                code_match = re.search(r'["\']?(https?://[^"\'\s]*code=[^"\'\s]*)["\']?', body_text)
                if code_match:
                    callback_in_body = code_match.group(1)
                    if "state=" in callback_in_body:
                        _print(f"[*] 在页面 body 中找到 Callback URL")
                        token_json = submit_callback_url(
                            callback_url=callback_in_body, code_verifier=oauth.code_verifier,
                            redirect_uri=oauth.redirect_uri, expected_state=oauth.state,
                            proxies=proxies)
                        return token_json, password
                # 检查 meta refresh redirect
                meta_match = re.search(r'<meta[^>]*url=(https?://[^"\'\s>]+)', body_text, re.IGNORECASE)
                if meta_match:
                    meta_url = meta_match.group(1)
                    _print(f"[*] 发现 meta refresh: {meta_url[:100]}")
                    if "code=" in meta_url and "state=" in meta_url:
                        token_json = submit_callback_url(
                            callback_url=meta_url, code_verifier=oauth.code_verifier,
                            redirect_uri=oauth.redirect_uri, expected_state=oauth.state,
                            proxies=proxies)
                        return token_json, password
                    current_url = meta_url
                    continue
                # 检查 JavaScript redirect
                js_match = re.search(r'(?:window\.location|location\.href)\s*=\s*["\']([^"\']+)["\']', body_text, re.IGNORECASE)
                if js_match:
                    js_url = js_match.group(1)
                    _print(f"[*] 发现 JS redirect: {js_url[:100]}")
                    if "code=" in js_url and "state=" in js_url:
                        token_json = submit_callback_url(
                            callback_url=js_url, code_verifier=oauth.code_verifier,
                            redirect_uri=oauth.redirect_uri, expected_state=oauth.state,
                            proxies=proxies)
                        return token_json, password
                    if js_url.startswith("http"):
                        current_url = js_url
                        continue
                if redir_i == 0 and final_resp.status_code == 200:
                    _print(f"[Warn] 首跳返回 200 非重定向，body 前200字符: {body_text[:200]}")
                break
            if not location:
                break
            next_url = urllib.parse.urljoin(current_url, location)
            if "code=" in next_url and "state=" in next_url:
                _print(f"[*] 在 Location header 中捕获到 Callback URL")
                token_json = submit_callback_url(
                    callback_url=next_url, code_verifier=oauth.code_verifier,
                    redirect_uri=oauth.redirect_uri, expected_state=oauth.state,
                    proxies=proxies)
                return token_json, password
            current_url = next_url

        _print(f"[Error] 未能在重定向链中捕获到 Callback URL (最后 URL: {current_url[:120]})")
        return None, None

    except Exception as e:
        _print(f"[Error] 运行时发生错误: {e}")
        return None, None


# ==========================================
# 保存结果 & Worker
# ==========================================

def _save_result(token_json: str, password: str) -> Optional[str]:
    """保存 Token 和账号密码，返回保存的文件路径"""
    try:
        t_data = json.loads(token_json)
        fname_email = t_data.get("email", "unknown").replace("@", "_")
        account_email = t_data.get("email", "")
    except Exception:
        fname_email = "unknown"
        account_email = ""

    file_name = f"token_{fname_email}_{int(time.time())}_{secrets.token_hex(2)}.json"
    os.makedirs(TOKEN_DIR, exist_ok=True)
    file_path = os.path.join(TOKEN_DIR, file_name)

    with _file_lock:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(token_json)
        if account_email and password:
            os.makedirs(KEYS_DIR, exist_ok=True)
            accounts_file = os.path.join(KEYS_DIR, "accounts.txt")
            with open(accounts_file, "a", encoding="utf-8") as af:
                af.write(f"{account_email}----{password}\n")

    _print(f"[*] Token 已保存: {file_name}")
    _copy_token_to_cli_proxy(file_path)
    return file_path


def worker_loop(stats: BatchStats, proxy: str, sleep_min: int = 30, sleep_max: int = 60,
                clash_api_url: Optional[str] = None, clash_group: Optional[str] = None,
                node_filter: Optional[str] = None) -> None:
    """单线程 Worker 循环：preflight → 节点轮换 → 注册 → 失败即停"""

    _print("[*] ===== Pre-flight Check =====")
    checks = preflight_check(proxy, clash_api_url, clash_group)
    all_ok = True
    for c in checks:
        tag = "OK" if c["ok"] else "FAIL"
        _print(f"  [{tag}] {c['name']}: {c['message']}")
        if not c["ok"]:
            all_ok = False
    if not all_ok:
        _print("[Error] Pre-flight 检查未通过，终止注册")
        stats.request_stop()
        return
    _print("[*] Pre-flight 全部通过\n")

    controller: Optional[ClashController] = None
    nodes: List[str] = []
    api_url = clash_api_url or CLASH_API_URL
    if api_url:
        secret = CLASH_API_SECRET
        controller = ClashController(api_url, clash_group or CLASH_GROUP_NAME, secret)
        nf = node_filter or os.environ.get("CLASH_NODE_FILTER", "").strip() or None
        nodes = controller.get_usable_nodes(nf)
        random.shuffle(nodes)
        _print(f"[*] Clash 可用节点: {len(nodes)} 个" + (f" (过滤: {nf})" if nf else ""))
        if not nodes:
            _print("[Error] 没有可用的 Clash 节点")
            stats.request_stop()
            return

    use_clash = bool(controller and nodes)
    node_idx = 0
    attempt = 0
    used_ips: set = set()

    while not stats.should_stop() and stats.remaining() > 0:
        attempt += 1
        _print(f"\n[*] {datetime.now().strftime('%H:%M:%S')} >>> 第 {attempt} 轮 (已成功 {stats.success}/{stats.target}) <<<")

        if use_clash:
            node = nodes[node_idx % len(nodes)]
            if not controller.switch_node(node):
                _print(f"[Error] 切换节点失败: {node}")
                stats.request_stop()
                break
            time.sleep(1)
            ip, loc = controller.get_current_ip(proxy)
            _print(f"[*] 节点: {node} → IP: {ip} ({loc})")
            if loc in ("CN", "HK"):
                _print(f"[Error] IP 地区不支持: {loc}，跳过此节点")
                node_idx += 1
                stats.add_fail()
                if stats.should_stop():
                    break
                continue
            used_ips.add(ip)
            node_idx += 1

        try:
            token_json, password = run(proxy or None, thread_safe=False,
                                       skip_net_check=use_clash)
            if stats.should_stop():
                break
            if token_json == "retry_403":
                stats.add_retry()
                _print("[*] 检测到 403，等待 10 秒后重试...")
                time.sleep(10)
                continue
            if token_json:
                _save_result(token_json, password)
                n = stats.add_success()
                _print(f"[*] 注册成功! ({n}/{stats.target})")
            else:
                stats.add_fail()
                _log_error(f"第{attempt}轮注册失败 (proxy={proxy})")
                _print("[*] 本次注册失败")
        except Exception as e:
            stats.add_fail()
            _log_error(f"第{attempt}轮未捕获异常: {e}")
            _print(f"[Error] 未捕获异常: {e}")

        if stats.should_stop():
            break

        base_wait = random.randint(sleep_min, sleep_max)
        jitter = random.uniform(0, 10)
        wait_time = base_wait + jitter
        _print(f"[*] 休息 {int(wait_time)} 秒...")
        time.sleep(wait_time)

    summary = stats.summary()
    if use_clash:
        summary += f", 使用IP数: {len(used_ips)}"
    _print(f"\n[*] 批量注册结束: {summary}")


def get_accounts() -> List[dict]:
    """读取已注册账号列表"""
    accounts_file = os.path.join(KEYS_DIR, "accounts.txt")
    if not os.path.exists(accounts_file):
        return []
    results = []
    try:
        with open(accounts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "----" not in line:
                    continue
                parts = line.split("----", 1)
                results.append({"email": parts[0], "password": parts[1] if len(parts) > 1 else ""})
    except Exception:
        pass
    return results
