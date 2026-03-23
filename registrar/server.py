"""
OpenAI 批量注册 Web 控制台 - FastAPI 后端
"""

import asyncio
import os
import queue
import threading
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import openai_reg

app = FastAPI(title="OpenAI 批量注册控制台")

# SSE 日志广播
_log_queue: queue.Queue = queue.Queue(maxsize=5000)
_current_stats: Optional[openai_reg.BatchStats] = None
_worker_thread: Optional[threading.Thread] = None


def _log_sink(msg: str) -> None:
    try:
        _log_queue.put_nowait(msg)
    except queue.Full:
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            pass
        _log_queue.put_nowait(msg)


openai_reg.set_log_callback(_log_sink)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/status")
async def status():
    mail_domain = os.environ.get("MAIL_DOMAIN", "").strip()
    imap_user = os.environ.get("IMAP_USER", "").strip()
    proxy_port = os.environ.get("PROXY_PORT", "7897").strip()
    proxy_url = os.environ.get("PROXY_URL", "").strip()

    env_ok = bool(mail_domain and imap_user)

    imap_ok = False
    imap_msg = "未检查"
    if env_ok:
        imap_ok, imap_msg = openai_reg.check_imap_connection()

    proxy_ip = ""
    proxy_loc = ""
    proxy_ok = False
    if proxy_url:
        try:
            from curl_cffi import requests as cf_requests
            proxies = {"http": proxy_url, "https": proxy_url}
            resp = cf_requests.get("https://ipinfo.io/json", proxies=proxies,
                                   impersonate="safari", verify=False, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                proxy_ip = data.get("ip", "")
                proxy_loc = f"{data.get('country', '')} {data.get('city', '')}"
                proxy_ok = True
        except Exception:
            pass

    cli_proxy_ok = False
    cli_proxy_clients = 0
    try:
        import urllib.request
        with urllib.request.urlopen("http://cli-proxy:8317/", timeout=3) as r:
            cli_proxy_ok = r.status == 200
    except Exception:
        pass

    accounts = openai_reg.get_accounts()

    running = _current_stats is not None and not _current_stats.should_stop() and _current_stats.success < _current_stats.target

    return JSONResponse({
        "env_ok": env_ok,
        "mail_domain": mail_domain,
        "imap_user": imap_user,
        "imap_ok": imap_ok,
        "imap_msg": imap_msg,
        "proxy_ok": proxy_ok,
        "proxy_ip": proxy_ip,
        "proxy_loc": proxy_loc,
        "cli_proxy_ok": cli_proxy_ok,
        "account_count": len(accounts),
        "registering": running,
        "stats": _current_stats.to_dict() if _current_stats else None,
    })


@app.post("/api/register")
async def start_register(request: Request):
    global _current_stats, _worker_thread

    if _worker_thread and _worker_thread.is_alive():
        return JSONResponse({"error": "注册任务正在进行中"}, status_code=409)

    body = await request.json()
    count = max(1, min(int(body.get("count", 5)), 50))

    while not _log_queue.empty():
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            break

    proxy_url = os.environ.get("PROXY_URL", "").strip()
    stats = openai_reg.BatchStats(count)
    _current_stats = stats

    def _run():
        openai_reg.worker_loop(stats, proxy=proxy_url, sleep_min=30, sleep_max=60)

    _worker_thread = threading.Thread(target=_run, daemon=True)
    _worker_thread.start()

    return JSONResponse({"message": f"开始注册 {count} 个账号", "target": count})


@app.post("/api/register/stop")
async def stop_register():
    if _current_stats:
        _current_stats.request_stop()
        return JSONResponse({"message": "已发送停止信号"})
    return JSONResponse({"message": "没有正在运行的任务"})


@app.get("/api/register/stream")
async def register_stream(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = _log_queue.get_nowait()
                yield {"event": "log", "data": msg}
            except queue.Empty:
                if _current_stats:
                    import json
                    yield {"event": "progress", "data": json.dumps(_current_stats.to_dict())}
                await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@app.get("/api/accounts")
async def list_accounts():
    accounts = openai_reg.get_accounts()
    return JSONResponse({"accounts": accounts, "total": len(accounts)})
