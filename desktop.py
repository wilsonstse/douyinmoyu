"""Local WebSocket control window for Douyin. Python 3, no packages needed."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
import urllib.parse
import urllib.request
import shutil

HOST, PORT = "127.0.0.1", 17853
MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
clients = {}
lock = threading.Lock()
stopping = threading.Event()
DOWNLOAD_DIR = Path(r"D:\下载")
download_status = ""
download_finished = False
download_busy = False
pending_move = None


def send_frame(conn, payload):
    data = payload.encode("utf-8")
    size = len(data)
    header = bytes([0x81, size]) if size < 126 else bytes([0x81, 126]) + size.to_bytes(2, "big")
    conn.sendall(header + data)


def run_download(video_id, resources):
    global download_status, download_finished, download_busy
    last_progress_update = 0.0

    def progress_hook(progress):
        nonlocal last_progress_update
        global download_status
        if progress.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last_progress_update < 0.2:
            return
        last_progress_update = now
        downloaded = progress.get("downloaded_bytes") or 0
        total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
        speed = progress.get("speed")
        speed_text = f"  {speed / 1048576:.1f} MB/s" if speed else ""
        if total:
            download_status = f"下载中 {min(downloaded / total * 100, 100):.1f}%{speed_text}"
        else:
            download_status = f"下载中 {downloaded / 1048576:.1f} MB{speed_text}"

    try:
        if not video_id.isdigit() or len(video_id) > 25:
            raise ValueError("无法识别当前视频")
        allowed_hosts = ("douyinvod.com", "douyin.com", "bytecdn.cn")
        candidates = []
        for url in resources:
            if not isinstance(url, str):
                continue
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and any(host == name or host.endswith("." + name)
                                                  for name in allowed_hosts):
                candidates.append(url)
        if not candidates:
            raise ValueError("页面未找到可下载的媒体地址")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = DOWNLOAD_DIR / f"{video_id}.mp4"
        temp_target = DOWNLOAD_DIR / f"{video_id}.mp4.part"
        last_error = None
        for url in candidates:
            try:
                request = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "video" not in content_type and "octet-stream" not in content_type:
                        raise ValueError("资源不是视频文件")
                    total = int(response.headers.get("Content-Length", "0") or 0)
                    downloaded = 0
                    start = time.monotonic()
                    with open(temp_target, "wb") as output:
                        while chunk := response.read(256 * 1024):
                            output.write(chunk)
                            downloaded += len(chunk)
                            elapsed = max(time.monotonic() - start, 0.001)
                            progress_hook({"status": "downloading", "downloaded_bytes": downloaded,
                                           "total_bytes": total, "speed": downloaded / elapsed})
                if downloaded == 0:
                    raise ValueError("视频文件为空")
                os.replace(temp_target, target)
                download_status = f"已保存：{video_id}"
                break
            except Exception as error:
                last_error = error
                try:
                    temp_target.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            raise ValueError(f"媒体地址不可用：{last_error}")
    except Exception as error:
        detail = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
        if "HTTP Error 403" in detail:
            download_status = "媒体地址被拒绝 (403)"
        else:
            download_status = f"下载失败：{detail[:38]}"
    finally:
        download_finished = True
        download_busy = False


def finish_browser_download(video_id, filename):
    global download_status, download_finished, download_busy
    try:
        source = Path(filename).resolve(strict=True)
        if not video_id.isdigit() or source.suffix.lower() != ".mp4":
            raise ValueError("下载文件无效")
        with source.open("rb") as downloaded_file:
            header = downloaded_file.read(32)
        if b"ftyp" not in header and b"styp" not in header:
            raise ValueError("收到的不是 MP4 视频")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = DOWNLOAD_DIR / f"{video_id}.mp4"
        if source != target:
            if target.exists():
                target = DOWNLOAD_DIR / f"{video_id}_{int(time.time())}.mp4"
            shutil.move(str(source), str(target))
        download_status = f"已保存：{video_id}"
    except Exception as error:
        download_status = f"保存失败：{str(error)[:38]}"
    finally:
        download_finished = True
        download_busy = False


def client_loop(conn):
    global download_status, download_finished, download_busy
    try:
        conn.settimeout(5)
        request = b""
        while b"\r\n\r\n" not in request and len(request) < 8192:
            request += conn.recv(1024)
        lines = request.decode("latin1").split("\r\n")
        headers = {name.strip().lower(): value.strip() for line in lines[1:]
                   if ":" in line for name, value in [line.split(":", 1)]}
        if not lines[0].startswith("GET / HTTP/1.1") or not headers.get("sec-websocket-key"):
            return
        origin = headers.get("origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            return
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + MAGIC).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        conn.settimeout(1)
        with lock:
            clients[conn] = 1
        buffer = bytearray()
        while not stopping.is_set():
            try:
                data = conn.recv(1024)
                if not data:
                    break
                # Chrome sends short masked text frames containing download metadata.
                buffer.extend(data)
                while len(buffer) >= 6:
                    size = buffer[1] & 0x7f
                    offset = 2
                    if size == 126:
                        if len(buffer) < 8:
                            break
                        size = int.from_bytes(buffer[2:4], "big")
                        offset = 4
                    elif size == 127:
                        if len(buffer) < 14:
                            break
                        size = int.from_bytes(buffer[2:10], "big")
                        offset = 10
                    if size > 2_000_000:
                        return
                    if len(buffer) < offset + 4 + size:
                        break
                    mask = buffer[offset:offset + 4]
                    payload = bytes(buffer[offset + 4 + i] ^ mask[i % 4] for i in range(size))
                    del buffer[:offset + 4 + size]
                    if payload == b"ping":
                        continue
                    try:
                        message = json.loads(payload)
                        if message.get("type") == "hello":
                            with lock:
                                clients[conn] = int(message.get("protocol", 1))
                        elif message.get("type") == "download" and not download_busy:
                            download_busy = True
                            download_finished = False
                            download_status = "准备下载…"
                            threading.Thread(target=run_download,
                                args=(message.get("videoId", ""), message.get("resources", [])),
                                daemon=True).start()
                        elif message.get("type") == "download" and download_busy:
                            download_status = "已有下载任务进行中"
                            download_finished = True
                        elif message.get("type") == "download-error":
                            download_status = "下载失败：" + str(message.get("reason", "未知错误"))[:50]
                            download_finished = True
                            download_busy = False
                        elif message.get("type") == "browser-progress":
                            download_busy = True
                            download_finished = False
                            received = int(message.get("received", 0))
                            total = int(message.get("total", 0))
                            download_status = (f"下载中 {received / total * 100:.1f}%" if total
                                               else f"下载中 {received / 1048576:.1f} MB")
                        elif message.get("type") == "browser-complete":
                            download_status = "正在保存…"
                            threading.Thread(target=finish_browser_download,
                                args=(message.get("videoId", ""), message.get("filename", "")),
                                daemon=True).start()
                    except (ValueError, TypeError):
                        pass
            except socket.timeout:
                continue
    except (OSError, ValueError, KeyError):
        pass
    finally:
        with lock:
            clients.pop(conn, None)
        conn.close()


def serve(listener):
    listener.settimeout(1)
    while not stopping.is_set():
        try:
            conn, _ = listener.accept()
            threading.Thread(target=client_loop, args=(conn,), daemon=True).start()
        except socket.timeout:
            continue
        except OSError:
            break


def main():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, PORT))
    listener.listen(5)
    threading.Thread(target=serve, args=(listener,), daemon=True).start()

    root = tk.Tk()
    root.title("remote")
    root.geometry("248x88")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    status = tk.StringVar(value="未连接")
    clear_timer = None

    def show_sent(label):
        nonlocal clear_timer
        if clear_timer is not None:
            root.after_cancel(clear_timer)
        status.set(f"已发送：{label}")
        clear_timer = root.after(2000, clear_sent)

    def clear_sent():
        nonlocal clear_timer
        clear_timer = None
        with lock:
            connected = bool(clients)
        status.set("" if connected else "未连接")

    def send(command, label):
        payload = json.dumps({"command": command})
        with lock:
            targets = list(clients.items())
        if command == "download" and targets and not any(version >= 3 for _, version in targets):
            status.set("请重载 Chrome 扩展")
            return
        alive = 0
        for conn, _version in targets:
            try:
                send_frame(conn, payload)
                alive += 1
            except OSError:
                with lock:
                    clients.pop(conn, None)
        if alive:
            show_sent(label)
        else:
            status.set("未连接")

    frame = ttk.Frame(root, padding=4)
    frame.pack(fill="both", expand=True)
    for col, (symbol, label, command) in enumerate((("⏮", "上一个", "previous"),
                                                   ("⏯", "暂停/继续", "toggle"),
                                                   ("⏭", "下一个", "next"),
                                                   ("⬇", "下载", "download"))):
        ttk.Button(frame, text=symbol, width=5,
                   command=lambda c=command, n=label: send(c, n)).grid(
            row=0, column=col, padx=2, pady=3)
    ttk.Label(frame, textvariable=status, anchor="center").grid(
        row=1, column=0, columnspan=4, sticky="ew", pady=2)

    def refresh_connection():
        global download_status, download_finished
        nonlocal clear_timer
        with lock:
            connected = bool(clients)
            supports_download = any(version >= 3 for version in clients.values())
        current = status.get()
        if download_status:
            if clear_timer is not None:
                root.after_cancel(clear_timer)
                clear_timer = None
            status.set(download_status)
            download_status = ""
            if download_finished:
                clear_timer = root.after(2000, clear_sent)
        elif connected and current.startswith("未连接"):
            status.set("")
        elif connected and not supports_download and current == "":
            status.set("请重载 Chrome 扩展")
        elif connected and supports_download and current == "请重载 Chrome 扩展":
            status.set("")
        elif not connected and not current.startswith("已发送"):
            status.set("未连接")
        root.after(250, refresh_connection)

    root.after(250, refresh_connection)

    def close():
        stopping.set()
        listener.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
