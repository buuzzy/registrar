"""
OpenAI 批量自动注册引擎 V0.5
从 V0.4 迁移，适配 Web 控制台：
- print() 输出重定向到可配置的日志回调
- 移除 argparse，暴露 start_batch() 供 server.py 调用
- 路径适配 Docker 共享卷
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
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, quote
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List
import urllib.parse
import ssl
import urllib.request
import urllib.error
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
                    ctx = None if _ssl_verify() else ssl._create_unverified_context()
                    imap_conn = imaplib.IMAP4_SSL(host, imap_port, ssl_context=ctx, timeout=imap_connect_timeout)
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
    try:
        if imap_ssl:
            ctx = None if _ssl_verify() else ssl._create_unverified_context()
            conn = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx, timeout=10)
        else:
            conn = imaplib.IMAP4(imap_host, imap_port, timeout=10)
        conn.login(imap_user, imap_password)
        conn.logout()
        return True, "连接成功"
    except Exception as e:
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


def _post_form(url: str, data: Dict[str, str], timeout: int = 30) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    try:
        context = None
        if not _ssl_verify():
            context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"token exchange failed: {resp.status}: {raw.decode('utf-8', 'replace')}")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(f"token exchange failed: {exc.code}: {raw.decode('utf-8', 'replace')}") from exc


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
                        code_verifier: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
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
    })
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


def run(proxy: Optional[str], thread_safe: bool = False) -> tuple:
    proxies: Any = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    s = requests.Session(proxies=proxies, impersonate="safari")

    if not _skip_net_check():
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

        current_url = continue_url
        _print(f"[*] 开始重定向追踪...")
        for redir_i in range(10):
            final_resp = s.get(current_url, allow_redirects=False, proxies=proxies,
                               verify=_ssl_verify(), timeout=15)
            location = final_resp.headers.get("Location") or ""
            if final_resp.status_code not in [301, 302, 303, 307, 308]:
                body_text = ""
                try:
                    body_text = final_resp.text[:500]
                except Exception:
                    pass
                code_match = re.search(r'["\']?(https?://[^"\'\s]*code=[^"\'\s]*)["\']?', body_text)
                if code_match:
                    callback_in_body = code_match.group(1)
                    if "state=" in callback_in_body:
                        token_json = submit_callback_url(
                            callback_url=callback_in_body, code_verifier=oauth.code_verifier,
                            redirect_uri=oauth.redirect_uri, expected_state=oauth.state)
                        return token_json, password
                break
            if not location:
                break
            next_url = urllib.parse.urljoin(current_url, location)
            if "code=" in next_url and "state=" in next_url:
                token_json = submit_callback_url(
                    callback_url=next_url, code_verifier=oauth.code_verifier,
                    redirect_uri=oauth.redirect_uri, expected_state=oauth.state)
                return token_json, password
            current_url = next_url

        _print("[Error] 未能在重定向链中捕获到 Callback URL")
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


def worker_loop(stats: BatchStats, proxy: str, sleep_min: int = 30, sleep_max: int = 60) -> None:
    """单线程 Worker 循环，注册直到达标或熔断"""
    attempt = 0
    while not stats.should_stop() and stats.remaining() > 0:
        attempt += 1
        _print(f"\n[*] {datetime.now().strftime('%H:%M:%S')} >>> 第 {attempt} 轮 (已成功 {stats.success}/{stats.target}) <<<")

        try:
            token_json, password = run(proxy or None, thread_safe=False)
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
                _print("[*] 本次注册失败。")
        except Exception as e:
            stats.add_fail()
            _log_error(f"第{attempt}轮未捕获异常: {e}")
            _print(f"[*] 未捕获异常: {e}")

        if stats.should_stop():
            break

        base_wait = random.randint(sleep_min, sleep_max)
        backoff = min(stats.consecutive_fails * 15, 120)
        jitter = random.uniform(0, 10)
        wait_time = base_wait + backoff + jitter
        _print(f"[*] 休息 {int(wait_time)} 秒...")
        time.sleep(wait_time)

    _print(f"\n[*] 批量注册结束: {stats.summary()}")


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
