import asyncio
import concurrent.futures
import mimetypes
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote, urlparse

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from playwright.async_api import async_playwright

MAX_RETRIES = 3
RETRY_DELAY = 1.5
DEFAULT_WORKERS = 6


def _no_window_flags():
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


class SecurityHeadersHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        local_fs_path = self.translate_path(self.path)
        if not os.path.exists(local_fs_path) and self.path != "/":
            relative_part = self.path.lstrip("/")
            target_url = f"{self.server.base_url.rstrip('/')}/{relative_part}"
            self.server.app.log(f"[404] {self.path}")
            self.server.app.log(f"下载: {target_url}")
            os.makedirs(os.path.dirname(local_fs_path) or ".", exist_ok=True)
            app = self.server.app
            exe = getattr(self.server, "executor", None)
            if exe is None or not app.is_running:
                return super().do_GET()
            with app.lock:
                app.dbg_total += 1
                app.dbg_pending += 1
            app.after(0, app.update_debug_progress)
            try:
                future = exe.submit(self._download_with_retry, target_url, local_fs_path)
                success = future.result(timeout=120)
            except Exception as e:
                app.log(f"下载提交失败: {e}")
                success = False
            if success:
                size = os.path.getsize(local_fs_path) if os.path.exists(local_fs_path) else 0
                app.log(f"完成: {os.path.basename(local_fs_path)}  ({app._fmt(size)})")
            else:
                app.log(f"失败: {target_url}")
            with app.lock:
                app.dbg_done += 1
                app.dbg_pending = max(0, app.dbg_pending - 1)
            app.after(0, app.update_debug_progress)
        return super().do_GET()

    def _download_with_retry(self, url, local_path):
        app = getattr(self.server, "app", None)
        if app is None or not app.is_running:
            return False
        filename = os.path.basename(local_path) or "file"
        for attempt in range(1, MAX_RETRIES + 1):
            if not app.is_running:
                return False
            try:
                with requests.get(url, stream=True, timeout=20) as response:
                    if response.status_code == 200:
                        downloaded = 0
                        last_report = 0
                        with open(local_path, "wb") as f:
                            for chunk in response.iter_content(chunk_size=65536):
                                if not app.is_running:
                                    return False
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    with app.lock:
                                        app.dbg_bytes += len(chunk)
                                    if downloaded - last_report >= 256 * 1024 or last_report == 0:
                                        app.log(f"下载中: {filename}  {app._fmt(downloaded)}")
                                        last_report = downloaded
                                        app.after(0, app.update_debug_progress)
                        with app.lock:
                            app.dbg_bytes_final += downloaded
                        return True
                    else:
                        app.log(f"重试 {attempt}/{MAX_RETRIES}: HTTP {response.status_code}")
            except Exception as e:
                if app.is_running:
                    app.log(f"重试 {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WebDL  ·  Resource Patch Monitor")
        self.geometry("860x720")
        self.configure(bg="#f0f2f5")
        self.minsize(720, 580)

        self.mode = tk.StringVar(value="full")
        self.root_dir = None
        self.base_url = None
        self.port = None
        self.server = None
        self.server_thread = None
        self.browser_thread = None
        self.executor = None
        self.is_running = False

        self.log_queue = queue.Queue()
        self.download_queue = queue.Queue()
        self.captured_urls = set()
        self.pending_downloads = 0
        self.completed_downloads = 0
        self.total_queued = 0
        self.lock = threading.Lock()
        self.running = False
        self._context = None
        self._crawler_thread = None
        self.curl_available = shutil.which("curl") is not None
        self._stop_event = threading.Event()
        self._dbg_busy = False
        self._full_busy = False

        self.dbg_total = 0
        self.dbg_done = 0
        self.dbg_pending = 0
        self.dbg_bytes = 0
        self.dbg_bytes_final = 0

        self._setup_style()
        self._build_ui()
        self.switch_mode()
        self.after(80, self.process_log_queue)
        if not self.curl_available:
            self.log("提示: 未检测到 curl，全量模式将回退到 requests")

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        BG = "#f0f2f5"
        CARD = "#ffffff"
        ACCENT = "#2563eb"
        TEXT = "#1e293b"
        MUTED = "#64748b"
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 11))
        style.configure("TLabelframe", background=CARD, bordercolor="#e2e8f0", relief="solid")
        style.configure("TLabelframe.Label", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TRadiobutton", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.map("TRadiobutton", background=[("active", BG)])
        style.configure("TCheckbutton", background=CARD, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", CARD), ("selected", CARD)])
        style.configure("TCombobox", fieldbackground="#fff", background="#e2e8f0", foreground=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", "#fff")])
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10))
        style.configure(
            "green.Horizontal.TProgressbar",
            troughcolor="#e2e8f0",
            background="#22c55e",
            thickness=8,
        )
        style.configure(
            "blue.Horizontal.TProgressbar",
            troughcolor="#e2e8f0",
            background=ACCENT,
            thickness=8,
        )

    def _build_ui(self):
        outer = ttk.Frame(self, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="运行模式", style="Title.TLabel").pack(side="left")
        mode_box = ttk.Frame(header)
        mode_box.pack(side="right")
        ttk.Radiobutton(
            mode_box, text="Debug 模式", variable=self.mode, value="debug", command=self.switch_mode
        ).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(
            mode_box, text="全量抓取", variable=self.mode, value="full", command=self.switch_mode
        ).pack(side="left")

        self.debug_frame = ttk.Frame(outer)
        self.full_frame = ttk.Frame(outer)
        self._build_debug_ui()
        self._build_full_ui()

        prog_card = ttk.Frame(outer, style="Card.TFrame", padding=(10, 8))
        prog_card.pack(fill="x", pady=(0, 8))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            prog_card,
            orient="horizontal",
            mode="determinate",
            variable=self.progress_var,
            style="blue.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill="x", side="left", expand=True)
        self.progress_label = ttk.Label(prog_card, text="0 / 0", width=22, style="Card.TLabel")
        self.progress_label.pack(side="left", padx=(10, 0))
        self.bytes_label = ttk.Label(prog_card, text="", width=14, style="Card.TLabel")
        self.bytes_label.pack(side="left", padx=(4, 0))

        ttk.Label(outer, text="运行日志", style="Muted.TLabel").pack(anchor="w", pady=(2, 2))
        log_frame = ttk.Frame(outer, style="Card.TFrame")
        log_frame.pack(fill="both", expand=True)
        self.logbox = scrolledtext.ScrolledText(
            log_frame,
            font=("Consolas", 9),
            bg="#ffffff",
            fg="#1e293b",
            insertbackground="#1e293b",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#e2e8f0",
            highlightcolor="#94a3b8",
            padx=8,
            pady=6,
        )
        self.logbox.pack(fill="both", expand=True, padx=1, pady=1)

    def _build_debug_ui(self):
        f = self.debug_frame
        card = ttk.LabelFrame(f, text="  资源补丁监控  ", padding=(12, 10))
        card.pack(fill="x")

        row0 = ttk.Frame(card, style="Card.TFrame")
        row0.pack(fill="x", pady=(0, 6))
        ttk.Label(row0, text="本地根目录", style="Card.TLabel", width=10).pack(side="left")
        self.dir_var_debug = tk.StringVar()
        ttk.Entry(row0, textvariable=self.dir_var_debug, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(4, 6)
        )
        tk.Button(
            row0, text="选择", command=self.choose_directory_debug, width=6,
            bg="#e2e8f0", fg="#1e293b", relief="flat", bd=0, font=("Segoe UI", 9), cursor="hand2"
        ).pack(side="left")

        row1 = ttk.Frame(card, style="Card.TFrame")
        row1.pack(fill="x", pady=(0, 6))
        ttk.Label(row1, text="URL 源", style="Card.TLabel", width=10).pack(side="left")
        self.url_var_debug = tk.StringVar()
        ttk.Entry(row1, textvariable=self.url_var_debug).pack(
            side="left", fill="x", expand=True, padx=(4, 0)
        )

        row2 = ttk.Frame(card, style="Card.TFrame")
        row2.pack(fill="x", pady=(0, 8))
        ttk.Label(row2, text="端口", style="Card.TLabel", width=10).pack(side="left")
        self.port_var = tk.StringVar(value="1234")
        ttk.Entry(row2, textvariable=self.port_var, width=8).pack(side="left", padx=(4, 16))
        ttk.Label(row2, text="并发数", style="Card.TLabel").pack(side="left")
        self.dbg_workers_var = tk.StringVar(value=str(DEFAULT_WORKERS))
        ttk.Combobox(
            row2, textvariable=self.dbg_workers_var,
            values=["2", "4", "6", "8", "12", "16"], state="readonly", width=5
        ).pack(side="left", padx=(4, 0))

        btn_row = ttk.Frame(card, style="Card.TFrame")
        btn_row.pack(fill="x")
        self.btn_debug = tk.Button(
            btn_row, text="启动服务 + 浏览器", command=self.toggle_debug,
            bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8", activeforeground="#fff",
            relief="flat", bd=0, font=("Segoe UI Semibold", 10), cursor="hand2", height=1, padx=14
        )
        self.btn_debug.pack(side="left")

    def _build_full_ui(self):
        f = self.full_frame
        card = ttk.LabelFrame(f, text="  全量抓取设置  ", padding=(12, 10))
        card.pack(fill="x")

        ttk.Label(card, text="目标网址", style="Card.TLabel").pack(anchor="w")
        self.url_entry = ttk.Entry(card, font=("Consolas", 10))
        self.url_entry.pack(fill="x", pady=(2, 8))
        self.url_entry.bind("<KeyRelease>", self.limit_url_length)

        ttk.Label(card, text="保存目录", style="Card.TLabel").pack(anchor="w")
        dir_row = ttk.Frame(card, style="Card.TFrame")
        dir_row.pack(fill="x", pady=(2, 8))
        self.dir_var_full = tk.StringVar()
        ttk.Entry(dir_row, textvariable=self.dir_var_full).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        tk.Button(
            dir_row, text="选择", command=self.choose_dir_full, width=6,
            bg="#e2e8f0", fg="#1e293b", relief="flat", bd=0, font=("Segoe UI", 9), cursor="hand2"
        ).pack(side="left")

        cfg = ttk.Frame(card, style="Card.TFrame")
        cfg.pack(fill="x", pady=(0, 8))
        ttk.Label(cfg, text="浏览器", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.browser_var = tk.StringVar(value="chrome")
        ttk.Combobox(
            cfg, textvariable=self.browser_var, values=["chrome", "edge"],
            state="readonly", width=8
        ).grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(cfg, text="语言", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        self.lang_var = tk.StringVar(value="zh-CN")
        ttk.Combobox(
            cfg, textvariable=self.lang_var,
            values=["zh-CN", "en-US", "ru-RU", "ja-JP", "ko-KR", "de-DE", "fr-FR"],
            state="readonly", width=8
        ).grid(row=0, column=3, sticky="w", padx=(4, 12))
        self.incognito_var = tk.BooleanVar(value=False)
        inc_wrap = self._make_check(cfg, "无痕", self.incognito_var)
        inc_wrap.grid(row=0, column=4, sticky="w")

        cfg2 = ttk.Frame(card, style="Card.TFrame")
        cfg2.pack(fill="x", pady=(0, 8))
        ttk.Label(cfg2, text="并发数", style="Card.TLabel").pack(side="left")
        self.workers_var = tk.StringVar(value=str(DEFAULT_WORKERS))
        ttk.Combobox(
            cfg2, textvariable=self.workers_var,
            values=["2", "4", "6", "8", "12", "16"], state="readonly", width=5
        ).pack(side="left", padx=(4, 16))
        self.check_complete_var = tk.BooleanVar(value=True)
        self._make_check(
            cfg2, "检查完备性并覆盖不完整文件", self.check_complete_var,
            side="left", padx=(0, 12)
        )
        self.scroll_var = tk.BooleanVar(value=True)
        self._make_check(
            cfg2, "自动滚动触发懒加载", self.scroll_var, side="left"
        )

        btn_row = ttk.Frame(card, style="Card.TFrame")
        btn_row.pack(fill="x")
        self.btn_full = tk.Button(
            btn_row, text="开始全量抓取", command=self.toggle_full,
            bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8", activeforeground="#fff",
            relief="flat", bd=0, font=("Segoe UI Semibold", 10), cursor="hand2", height=1, padx=14
        )
        self.btn_full.pack(side="left")

    def switch_mode(self):
        self.debug_frame.pack_forget()
        self.full_frame.pack_forget()
        if self.mode.get() == "debug":
            self.debug_frame.pack(fill="x", pady=(0, 8))
            self.title("Debug  ·  Resource Patch Monitor")
            self.progress_bar.configure(style="blue.Horizontal.TProgressbar")
            self.update_debug_progress()
        elif self.mode.get() == "full":
            self.full_frame.pack(fill="x", pady=(0, 8))
            self.title("WebDL3.1  ·  全量抓取")
            self.progress_bar.configure(style="green.Horizontal.TProgressbar")
            self.update_progress()
            if self.executor is None or (
                hasattr(self.executor, "_shutdown") and self.executor._shutdown
            ):
                self._start_download_workers()

    def _fmt(self, nbytes):
        if nbytes < 1024:
            return f"{nbytes} B"
        if nbytes < 1024 * 1024:
            return f"{nbytes / 1024:.1f} KB"
        if nbytes < 1024 * 1024 * 1024:
            return f"{nbytes / (1024 * 1024):.2f} MB"
        return f"{nbytes / (1024 * 1024 * 1024):.2f} GB"

    def _make_check(self, parent, text, variable, **pack_kwargs):
        """自定义对勾复选框（选中 ✓，未选中 ☐）"""
        wrap = tk.Frame(parent, bg="#ffffff")
        lbl = tk.Label(
            wrap,
            text="✓" if variable.get() else "☐",
            font=("Segoe UI", 12),
            fg="#16a34a" if variable.get() else "#94a3b8",
            bg="#ffffff",
            cursor="hand2",
            width=2,
        )
        lbl.pack(side="left")
        txt = tk.Label(
            wrap,
            text=text,
            font=("Segoe UI", 10),
            fg="#1e293b",
            bg="#ffffff",
            cursor="hand2",
        )
        txt.pack(side="left")

        def toggle(event=None):
            variable.set(not variable.get())
            if variable.get():
                lbl.config(text="✓", fg="#16a34a")
            else:
                lbl.config(text="☐", fg="#94a3b8")

        def on_var_write(*_):
            if variable.get():
                lbl.config(text="✓", fg="#16a34a")
            else:
                lbl.config(text="☐", fg="#94a3b8")

        try:
            variable.trace_add("write", on_var_write)
        except Exception:
            variable.trace("w", on_var_write)

        lbl.bind("<Button-1>", toggle)
        txt.bind("<Button-1>", toggle)
        wrap.bind("<Button-1>", toggle)
        if pack_kwargs:
            wrap.pack(**pack_kwargs)
        return wrap

    def log(self, text):
        self.log_queue.put(text)

    def log_message(self, message: str):
        self.log(message)

    def process_log_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self.logbox.insert("end", text + "\n")
                self.logbox.see("end")
        except queue.Empty:
            pass
        self.after(80, self.process_log_queue)

    def update_debug_progress(self):
        with self.lock:
            total = self.dbg_total
            done = self.dbg_done
            bytes_now = self.dbg_bytes
        if total > 0:
            self.progress_var.set((done / total) * 100)
            self.progress_label.config(text=f"{done} / {total}")
        else:
            self.progress_var.set(0)
            self.progress_label.config(text="0 / 0")
        self.bytes_label.config(text=self._fmt(bytes_now) if bytes_now else "")

    def update_progress(self):
        with self.lock:
            total = self.total_queued
            done = self.completed_downloads
        if total > 0:
            self.progress_var.set((done / total) * 100)
            self.progress_label.config(text=f"{done} / {total}")
        else:
            self.progress_var.set(0)
            self.progress_label.config(text="0 / 0")
        self.bytes_label.config(text="")

    def choose_directory_debug(self):
        path = filedialog.askdirectory(title="选择本地根目录")
        if path:
            self.root_dir = path
            self.dir_var_debug.set(path)
            self.log(f"根目录: {path}")

    def choose_dir_full(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_var_full.set(d)

    def limit_url_length(self, event=None):
        text = self.url_entry.get()
        if len(text) > 200:
            self.url_entry.delete(200, "end")

    def toggle_debug(self):
        if getattr(self, "_dbg_busy", False):
            return
        if self.is_running:
            self._stop_debug()
        else:
            self._start_debug()

    def _set_debug_btn_running(self, running):
        if running:
            self.btn_debug.config(
                text="停止服务", bg="#ef4444", fg="#fff",
                activebackground="#dc2626", activeforeground="#fff", state=tk.NORMAL
            )
        else:
            self.btn_debug.config(
                text="启动服务 + 浏览器", bg="#2563eb", fg="#fff",
                activebackground="#1d4ed8", activeforeground="#fff", state=tk.NORMAL
            )

    def _start_debug(self):
        if self.is_running or getattr(self, "_dbg_busy", False):
            return
        dir_path = self.dir_var_debug.get().strip()
        url = self.url_var_debug.get().strip()
        port_str = self.port_var.get().strip()
        if not dir_path:
            messagebox.showwarning("提示", "请先选择本地根目录")
            return
        if not url:
            messagebox.showwarning("提示", "请输入 URL 源")
            return
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("提示", "URL 源必须以 http:// 或 https:// 开头")
            return
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            messagebox.showwarning("提示", "端口必须是 1-65535 之间的数字")
            return
        try:
            n = int(self.dbg_workers_var.get())
        except Exception:
            n = DEFAULT_WORKERS
        n = max(1, min(n, 32))

        self._dbg_busy = True
        self.btn_debug.config(state=tk.DISABLED)

        self.root_dir = dir_path
        self.base_url = url.rstrip("/")
        self.port = int(port_str)
        try:
            os.chdir(self.root_dir)
        except Exception as e:
            self.log(f"切换目录失败: {e}")
            self._dbg_busy = False
            self._set_debug_btn_running(False)
            return

        with self.lock:
            self.dbg_total = 0
            self.dbg_done = 0
            self.dbg_pending = 0
            self.dbg_bytes = 0
            self.dbg_bytes_final = 0
        self.update_debug_progress()

        if self.executor:
            try:
                self.executor.shutdown(wait=False)
            except Exception:
                pass
            self.executor = None

        self.executor = ThreadPoolExecutor(max_workers=n)
        self.is_running = True
        self._set_debug_btn_running(True)
        self._dbg_busy = False

        self.log(f"根目录: {self.root_dir}")
        self.log(f"URL 源: {self.base_url}")
        self.log(f"端口: {self.port}  |  并发: {n}  |  重试: {MAX_RETRIES}")

        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()
        self.browser_thread = threading.Thread(
            target=lambda: asyncio.run(self._start_browser()), daemon=True
        )
        self.browser_thread.start()

    def _run_server(self):
        try:
            self.server = HTTPServer(("localhost", self.port), SecurityHeadersHandler)
            self.server.base_url = self.base_url
            self.server.app = self
            self.server.executor = self.executor
            self.log(f"服务器已启动  →  http://localhost:{self.port}")
            self.server.serve_forever()
        except OSError as e:
            self.log(f"服务器错误(端口可能被占用): {e}")
            self.after(0, self._stop_debug)
        except Exception as e:
            self.log(f"服务器错误: {e}")
            self.after(0, self._stop_debug)

    async def _start_browser(self):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(
                    locale="en-US",
                    timezone_id="America/New_York",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                await page.goto(f"http://localhost:{self.port}")
                self.log("浏览器已打开")
                while self.is_running:
                    await asyncio.sleep(0.5)
                try:
                    await browser.close()
                except Exception:
                    pass
        except Exception as e:
            if self.is_running:
                self.log(f"浏览器启动失败: {e}")

    def _stop_debug(self):
        if getattr(self, "_dbg_busy", False):
            return
        if not self.is_running and self.server is None:
            self._set_debug_btn_running(False)
            return
        self._dbg_busy = True
        self.btn_debug.config(state=tk.DISABLED)
        self.is_running = False

        srv = self.server
        self.server = None
        if srv is not None:
            def _shutdown_server():
                try:
                    srv.shutdown()
                except Exception:
                    pass
                try:
                    srv.server_close()
                except Exception:
                    pass
            threading.Thread(target=_shutdown_server, daemon=True).start()
            self.log("服务器已停止")

        exe = self.executor
        self.executor = None
        if exe is not None:
            try:
                exe.shutdown(wait=False)
            except Exception:
                pass

        with self.lock:
            total = self.dbg_total
            done = self.dbg_done
            b = self.dbg_bytes
        self.log(f"已停止  |  完成 {done}/{total}  |  累计下载 {self._fmt(b)}")
        self.log("浏览器窗口将自动关闭（若未关闭请手动关闭）")
        self._set_debug_btn_running(False)
        self._dbg_busy = False

    def _start_download_workers(self):
        try:
            n = int(self.workers_var.get())
        except Exception:
            n = DEFAULT_WORKERS
        n = max(1, min(n, 32))
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=n, thread_name_prefix="dl"
        )
        for _ in range(n):
            self.executor.submit(self._download_worker_loop)
        self.log(f"下载线程池已启动，并发数: {n}")

    def _download_worker_loop(self):
        while True:
            try:
                item = self.download_queue.get(timeout=1.0)
            except queue.Empty:
                if self._stop_event.is_set() and self.download_queue.empty():
                    break
                continue
            if item is None:
                self.download_queue.task_done()
                break
            try:
                self._process_one_download(item)
            except Exception as e:
                self.log(f"下载线程异常: {e}")
            finally:
                self.download_queue.task_done()

    def get_remote_size(self, url, headers):
        try:
            if self.curl_available:
                cmd = ["curl", "-sI", "-L", "--connect-timeout", "12", "--max-time", "25"]
                ua = headers.get("User-Agent", "Mozilla/5.0")
                cmd.extend(["-A", ua])
                if headers.get("Cookie"):
                    cmd.extend(["-H", f"Cookie: {headers['Cookie']}"])
                if headers.get("Referer"):
                    cmd.extend(["-e", headers["Referer"]])
                cmd.append(url)
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=_no_window_flags()
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if line.lower().startswith("content-length:"):
                            return int(line.split(":", 1)[1].strip())
            else:
                r = requests.head(url, headers=headers, timeout=12, allow_redirects=True)
                if r.status_code == 200 and "content-length" in r.headers:
                    return int(r.headers["content-length"])
        except Exception:
            pass
        return None

    def is_file_complete(self, path, expected_size):
        if not os.path.exists(path):
            return False
        actual = os.path.getsize(path)
        if actual == 0:
            return False
        if expected_size is not None and expected_size > 0:
            return actual == expected_size
        return actual > 0

    def _process_one_download(self, item):
        url, save_path, headers = item
        check_complete = self.check_complete_var.get()
        expected_size = None
        filename = os.path.basename(save_path)
        if check_complete:
            expected_size = self.get_remote_size(url, headers)
        if os.path.exists(save_path):
            if check_complete:
                if self.is_file_complete(save_path, expected_size):
                    self.log(f"完备跳过: {filename}")
                    with self.lock:
                        self.completed_downloads += 1
                        self.pending_downloads = max(0, self.pending_downloads - 1)
                    self.after(0, self.update_progress)
                    return
                else:
                    self.log(f"不完整重下: {filename}")
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass
            else:
                if os.path.getsize(save_path) > 0:
                    self.log(f"已存在跳过: {filename}")
                    with self.lock:
                        self.completed_downloads += 1
                        self.pending_downloads = max(0, self.pending_downloads - 1)
                    self.after(0, self.update_progress)
                    return
        self.log(f"下载: {filename}")
        success = False
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if self._stop_event.is_set() and not self.running:
                break
            try:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                if self.curl_available:
                    success = self._download_with_curl(
                        url, save_path, headers, expected_size, filename
                    )
                else:
                    success = self._download_with_requests(
                        url, save_path, headers, expected_size, filename
                    )
                if success:
                    break
                if attempt < max_retries:
                    wait = min(2 ** attempt, 20)
                    self.log(f"失败重试 ({attempt}/{max_retries}) {wait}s: {filename}")
                    time.sleep(wait)
                else:
                    self.log(f"最终失败: {filename}")
            except Exception as e:
                if attempt < max_retries:
                    wait = min(2 ** attempt, 20)
                    self.log(f"异常重试 ({attempt}/{max_retries}) {wait}s: {e}")
                    time.sleep(wait)
                else:
                    self.log(f"异常最终失败: {filename} - {e}")
        if success:
            final_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            self.log(f"保存: {filename}  {self._fmt(final_size)}")
        with self.lock:
            self.completed_downloads += 1
            self.pending_downloads = max(0, self.pending_downloads - 1)
            remaining = self.pending_downloads
        self.after(0, self.update_progress)
        if remaining == 0 and not self.running:
            self.log("所有资源已下载完毕。")

    def _download_with_curl(self, url, save_path, headers, expected_size=None, filename=""):
        cmd = [
            "curl", "-L", "--fail", "--retry", "2", "--retry-delay", "2",
            "--connect-timeout", "20", "--max-time", "180",
            "-C", "-", "-o", save_path, "--silent", "--show-error", "-w", "%{size_download}",
        ]
        ua = headers.get(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        cmd.extend(["-A", ua])
        if headers.get("Cookie"):
            cmd.extend(["-H", f"Cookie: {headers['Cookie']}"])
        if headers.get("Referer"):
            cmd.extend(["-e", headers["Referer"]])
        cmd.append(url)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=_no_window_flags()
            )
            last_report = 0
            start_t = time.time()
            while proc.poll() is None:
                if self._stop_event.is_set() and not self.running:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return False
                if os.path.exists(save_path):
                    cur = os.path.getsize(save_path)
                    if cur - last_report >= 256 * 1024 or (cur > 0 and last_report == 0):
                        self.log(f"下载中: {filename}  {self._fmt(cur)}")
                        last_report = cur
                if time.time() - start_t > 90 and last_report == 0:
                    self.log(f"无进度超时: {filename}")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                time.sleep(0.5)
            try:
                stdout, stderr = proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout, stderr = "", "communicate timeout"
            if proc.returncode == 0 and os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                final = os.path.getsize(save_path)
                self.log(f"下载中: {filename}  {self._fmt(final)}")
                if expected_size is not None and expected_size > 0 and final != expected_size:
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass
                    return False
                return True
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
            if stderr:
                self.log(f"curl: {stderr.strip()[:180]}")
            return False
        except Exception as e:
            self.log(f"curl 异常: {e}")
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
            return False

    def _download_with_requests(self, url, save_path, headers, expected_size=None, filename=""):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(20, 180)) as r:
                r.raise_for_status()
                downloaded = 0
                last_report = 0
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if self._stop_event.is_set() and not self.running:
                            return False
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if downloaded - last_report >= 256 * 1024:
                                self.log(f"下载中: {filename}  {self._fmt(downloaded)}")
                                last_report = downloaded
            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                final = os.path.getsize(save_path)
                self.log(f"下载中: {filename}  {self._fmt(final)}")
                if expected_size is not None and expected_size > 0 and final != expected_size:
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass
                    return False
                return True
            return False
        except Exception as e:
            self.log(f"requests: {e}")
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
            return False

    def clear_download_queue(self):
        while not self.download_queue.empty():
            try:
                self.download_queue.get_nowait()
                self.download_queue.task_done()
            except queue.Empty:
                break

    def toggle_full(self):
        if getattr(self, "_full_busy", False):
            return
        if self.running:
            self._stop_full()
        else:
            self._start_full()

    def _set_full_btn_running(self, running):
        if running:
            self.btn_full.config(
                text="停止抓取", bg="#ef4444", fg="#fff",
                activebackground="#dc2626", activeforeground="#fff", state=tk.NORMAL
            )
        else:
            self.btn_full.config(
                text="开始全量抓取", bg="#2563eb", fg="#fff",
                activebackground="#1d4ed8", activeforeground="#fff", state=tk.NORMAL
            )

    def _start_full(self):
        if self.running or getattr(self, "_full_busy", False):
            return
        url = self.url_entry.get().strip()
        save_dir = self.dir_var_full.get().strip()
        if not url or not save_dir:
            messagebox.showwarning("提示", "请填写网址并选择目录")
            return
        self._full_busy = True
        self.btn_full.config(state=tk.DISABLED)

        self.running = True
        self._stop_event.clear()
        self.captured_urls.clear()
        self.clear_download_queue()
        with self.lock:
            self.pending_downloads = 0
            self.completed_downloads = 0
            self.total_queued = 0
        self.progress_var.set(0)
        self.progress_label.config(text="0 / 0")
        self.bytes_label.config(text="")

        if self.executor is None or (
            hasattr(self.executor, "_shutdown") and self.executor._shutdown
        ):
            self._start_download_workers()

        self._set_full_btn_running(True)
        self._full_busy = False
        self._crawler_thread = threading.Thread(
            target=lambda: asyncio.run(self.run_crawler()), daemon=True
        )
        self._crawler_thread.start()

    def _stop_full(self):
        if getattr(self, "_full_busy", False):
            return
        if not self.running:
            self._set_full_btn_running(False)
            return
        self._full_busy = True
        self.btn_full.config(state=tk.DISABLED)
        self.running = False
        self._stop_event.set()
        self.log("正在停止（已排队下载会继续完成）...")
        self._set_full_btn_running(False)
        self._full_busy = False

    def get_timezone_by_lang(self, lang):
        mapping = {
            "ja": "Asia/Tokyo",
            "en": "America/New_York",
            "ru": "Europe/Moscow",
            "ko": "Asia/Seoul",
            "fr": "Europe/Paris",
            "de": "Europe/Berlin",
        }
        for k, v in mapping.items():
            if lang.startswith(k):
                return v
        return "Asia/Shanghai"

    async def run_crawler(self):
        url = self.url_entry.get().strip()
        save_dir = self.dir_var_full.get().strip()
        selected_lang = self.lang_var.get().strip() or "zh-CN"
        is_incognito = self.incognito_var.get()
        selected_browser = self.browser_var.get()
        do_scroll = self.scroll_var.get()
        if not url or not save_dir:
            self.log("错误: 请填写网址并选择目录")
            self.after(0, self._reset_buttons_after_error)
            return
        async with async_playwright() as p:
            user_data_dir = (
                tempfile.mkdtemp()
                if is_incognito
                else os.path.join(os.getcwd(), "browser_profile")
            )
            launch_args = [
                f"--lang={selected_lang}",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            channel = "chrome" if selected_browser == "chrome" else "msedge"
            context = None
            used_browser = None
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel=channel,
                    headless=False,
                    args=launch_args,
                    locale=selected_lang,
                    timezone_id=self.get_timezone_by_lang(selected_lang),
                    ignore_https_errors=True,
                    extra_http_headers={
                        "Accept-Language": f"{selected_lang},en;q=0.9,en-US;q=0.8"
                    },
                )
                used_browser = selected_browser
                self.log(f"浏览器启动成功: {used_browser}")
            except Exception as e:
                self.log(f"启动 {selected_browser} 失败: {e}")
                try:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        headless=False,
                        args=launch_args,
                        locale=selected_lang,
                        timezone_id=self.get_timezone_by_lang(selected_lang),
                        ignore_https_errors=True,
                        extra_http_headers={
                            "Accept-Language": f"{selected_lang},en;q=0.9,en-US;q=0.8"
                        },
                    )
                    used_browser = "Playwright Chromium"
                    self.log("已回退到 Playwright Chromium")
                except Exception as e2:
                    self.log(f"所有浏览器启动均失败: {e2}")
                    self.after(0, self._reset_buttons_after_error)
                    return
            self._context = context

            async def queue_resource(r_url, content_type="", status=200):
                if not self.running:
                    return
                if not r_url.startswith("http") or r_url in self.captured_urls:
                    return
                if r_url.startswith("data:") or r_url.startswith("blob:"):
                    return
                self.captured_urls.add(r_url)
                try:
                    parsed = urlparse(r_url)
                    path = unquote(parsed.path.lstrip("/"))
                    ext = (
                        mimetypes.guess_extension(content_type.split(";")[0].strip())
                        if content_type
                        else ""
                    )
                    if not path or path.endswith("/"):
                        path += "index" + (ext or ".html")
                    elif not os.path.splitext(path)[1]:
                        path += ext or ""
                    path = path.replace("..", "_").replace(":", "_")
                    if parsed.query:
                        qsafe = (
                            parsed.query[:80]
                            .replace("/", "_")
                            .replace("?", "_")
                            .replace("&", "_")
                        )
                        base, e = os.path.splitext(path)
                        path = f"{base}_{qsafe}{e}" if e else f"{path}_{qsafe}"
                    local_path = os.path.join(
                        save_dir, "dump", parsed.netloc or "root", path
                    )
                    try:
                        cookies = await context.cookies()
                        cookie_str = "; ".join(
                            [f"{c['name']}={c['value']}" for c in cookies]
                        )
                    except Exception:
                        cookie_str = ""
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Cookie": cookie_str,
                        "Referer": url,
                    }
                    with self.lock:
                        self.pending_downloads += 1
                        self.total_queued += 1
                    self.after(0, self.update_progress)
                    self.download_queue.put((r_url, local_path, headers))
                except Exception as e:
                    self.log(f"处理资源异常: {e}")

            async def on_response(response):
                try:
                    if response.status not in (200, 206):
                        return
                    ct = response.headers.get("content-type", "")
                    await queue_resource(response.url, ct, response.status)
                except Exception as e:
                    self.log(f"response 异常: {e}")

            context.on("response", on_response)
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            page.on("response", on_response)
            self.log(
                f"监听中  [语言:{selected_lang}  无痕:{is_incognito}  浏览器:{used_browser}]"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            except Exception as e:
                self.log(f"页面加载异常: {e}")
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            if do_scroll and self.running:
                self.log("自动滚动触发懒加载...")
                try:
                    for _ in range(8):
                        if not self.running:
                            break
                        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
                        await asyncio.sleep(0.8)
                    await page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(1.0)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                except Exception as e:
                    self.log(f"滚动异常: {e}")
            self.log("页面监听中，可随时停止...")
            while self.running:
                await asyncio.sleep(0.4)
            try:
                await context.close()
            except Exception:
                pass
            self._context = None
            self.log("抓取循环已停止。")
            self.after(0, lambda: self._set_full_btn_running(False))

    def _reset_buttons_after_error(self):
        self.running = False
        self._stop_event.set()
        self._full_busy = False
        self._set_full_btn_running(False)


if __name__ == "__main__":
    app = App()
    app.mainloop()
