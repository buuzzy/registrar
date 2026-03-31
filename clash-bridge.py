"""
Clash API Bridge: TCP → Unix Socket (macOS / Linux only)
将 Clash Verge 的 Unix Socket API 暴露为 HTTP 端口，供 Docker 容器访问。

Windows 上 Clash Verge 直接通过 TCP 暴露 external-controller，
Docker 容器可通过 host.docker.internal 直接访问，无需此脚本。

用法: python3 clash-bridge.py [port] [socket_path]
"""

import socket
import threading
import sys
import os
import signal
import platform

DEFAULT_PORT = 9090
DEFAULT_SOCK = "/tmp/verge/verge-mihomo.sock"


def forward(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


def handle_client(client_sock, sock_path):
    try:
        unix_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_sock.connect(sock_path)
    except Exception as e:
        try:
            client_sock.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
            )
            client_sock.close()
        except Exception:
            pass
        return

    t1 = threading.Thread(target=forward, args=(client_sock, unix_sock), daemon=True)
    t2 = threading.Thread(target=forward, args=(unix_sock, client_sock), daemon=True)
    t1.start()
    t2.start()


def main():
    if platform.system() == "Windows":
        print("[Clash Bridge] Windows 上不需要此脚本。")
        print("[Clash Bridge] Clash Verge 已通过 TCP 暴露 external-controller，")
        print("[Clash Bridge] Docker 容器可直接通过 host.docker.internal:9090 访问。")
        print("[Clash Bridge] 请确保 Clash Verge 设置中 external-controller 监听地址为 0.0.0.0:9090")
        sys.exit(0)

    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    sock_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SOCK

    if not os.path.exists(sock_path):
        print(f"[Error] Socket 不存在: {sock_path}")
        print("请确保 Clash Verge 正在运行")
        sys.exit(1)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(32)

    print(f"[Clash Bridge] {sock_path} → 0.0.0.0:{port}")
    print(f"[Clash Bridge] Docker 容器可通过 http://host.docker.internal:{port} 访问")
    print("[Clash Bridge] Ctrl+C 停止")

    signal.signal(signal.SIGINT, lambda *_: (print("\n[Clash Bridge] 已停止"), os._exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    while True:
        client, addr = server.accept()
        threading.Thread(target=handle_client, args=(client, sock_path), daemon=True).start()


if __name__ == "__main__":
    main()
