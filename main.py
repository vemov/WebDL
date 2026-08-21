import asyncio
import threading
import queue
import tkinter as tk
from tkinter import filedialog, ttk
import os
import tempfile
import requests
import mimetypes
from urllib.parse import urlparse, unquote
from tkinter.scrolledtext import ScrolledText
from playwright.async_api import async_playwright

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("WebDL2.0")
        self.root.geometry("900x760")
        self.root.configure(bg="#e8e8e8")
        self.root.minsize(780, 620)
        try:
            self.root.iconbitmap(default="")
        except:
            pass

        self.log_queue = queue.Queue()
        self.download_queue = queue.Queue()
        self.captured_urls = set()
        self.pending_downloads = 0
        self.lock = threading.Lock()
        self.running = False

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#e8e8e8", foreground="#222222", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#e8e8e8")
        style.configure("TLabel", background="#e8e8e8", foreground="#333333")
        style.configure("TLabelframe", background="#e8e8e8", bordercolor="#c0c0c0")
        style.configure("TLabelframe.Label", background="#e8e8e8", foreground="#444444", font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", background="#e8e8e8", foreground="#333333")
        style.configure("TCombobox", fieldbackground="#ffffff", background="#d0d0d0", foreground="#222222")
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")])

        main = ttk.Frame(root, padding=16)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="目标网址").pack(anchor="w")
        self.url_entry = ttk.Entry(main, font=("Consolas", 11))
        self.url_entry.pack(fill="x", pady=(3, 10))
        self.url_entry.bind("<KeyRelease>", self.limit_url_length)

        ttk.Label(main, text="保存目录").pack(anchor="w")
        dir_row = ttk.Frame(main)
        dir_row.pack(fill="x", pady=(3, 12))
        self.dir_var = tk.StringVar()
        ttk.Entry(dir_row, textvariable=self.dir_var, font=("Consolas", 10)).pack(side="left", fill="x", expand=True)
        self.dir_btn = tk.Button(
            dir_row, text="选择目录", command=self.choose_dir, width=10,
            bg="#c8c8c8", fg="#222222", activebackground="#b0b0b0", activeforeground="#111111",
            relief="flat", bd=0, font=("Microsoft YaHei UI", 9), cursor="hand2"
        )
        self.dir_btn.pack(side="left", padx=(8, 0), ipady=2)

        cfg = ttk.LabelFrame(main, text="浏览器设置", padding=10)
        cfg.pack(fill="x", pady=(0, 12))

        ttk.Label(cfg, text="浏览器").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.browser_var = tk.StringVar(value="chrome")
        ttk.Combobox(cfg, textvariable=self.browser_var, values=["chrome", "edge"], state="readonly", width=10).grid(row=0, column=1, sticky="w")

        ttk.Label(cfg, text="语言").grid(row=0, column=2, sticky="w", padx=(18, 6))
        self.lang_var = tk.StringVar(value="zh-CN")
        ttk.Combobox(cfg, textvariable=self.lang_var, values=["zh-CN", "en-US", "ru-RU", "ja-JP", "ko-KR", "de-DE", "fr-FR"], state="readonly", width=10).grid(row=0, column=3, sticky="w")

        self.incognito_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg, text="无痕模式", variable=self.incognito_var).grid(row=0, column=4, sticky="w", padx=(18, 0))

        btn_row = ttk.Frame(main)
        btn_row.pack(fill="x", pady=(0, 12))

        self.start_btn = tk.Button(
            btn_row, text="开始全量抓取", command=self.start_app, height=2,
            bg="#c0c0c0", fg="#222222", activebackground="#a8a8a8", activeforeground="#111111",
            relief="flat", bd=0, font=("Microsoft YaHei UI", 10), cursor="hand2"
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.action_btn = tk.Button(
            btn_row, text="停止抓取", command=self.toggle_action, height=2, state="disabled",
            bg="#c0c0c0", fg="#222222", activebackground="#a8a8a8", activeforeground="#111111",
            relief="flat", bd=0, font=("Microsoft YaHei UI", 10), cursor="hand2"
        )
        self.action_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.action_mode = "stop"

        ttk.Label(main, text="运行日志").pack(anchor="w", pady=(2, 3))
        self.logbox = ScrolledText(
            main, font=("Consolas", 10), bg="#f5f5f5", fg="#222222",
            insertbackground="#222222", relief="flat", bd=0, highlightthickness=1,
            highlightbackground="#c0c0c0", highlightcolor="#a0a0a0"
        )
        self.logbox.pack(fill="both", expand=True)

        self.root.after(100, self.process_log_queue)
        threading.Thread(target=self.downloader_worker, daemon=True).start()

    def limit_url_length(self, event=None):
        text = self.url_entry.get()
        if len(text) > 100:
            self.url_entry.delete(100, "end")

    def choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_var.set(d)

    def log(self, text):
        self.log_queue.put(text)

    def process_log_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self.logbox.insert("end", text + "\n")
                self.logbox.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self.process_log_queue)

    def downloader_worker(self):
        while True:
            url, save_path, headers = self.download_queue.get()
            if os.path.exists(save_path):
                self.log(f"文件已存在，跳过: {os.path.basename(save_path)}")
                self.download_queue.task_done()
                continue
            self.log(f"正在下载: {os.path.basename(save_path)}")
            try:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with requests.get(url, headers=headers, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(save_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            f.write(chunk)
                self.log(f"保存成功: {os.path.basename(save_path)}")
            except Exception as e:
                self.log(f"下载失败: {os.path.basename(save_path)} - {e}")
            finally:
                with self.lock:
                    self.pending_downloads -= 1
                    if self.pending_downloads == 0 and not self.running:
                        self.log("所有资源已下载完毕。")
                self.download_queue.task_done()

    def start_app(self):
        self.running = True
        self.start_btn.config(state="disabled", bg="#d8d8d8")
        self.action_btn.config(state="normal", text="停止抓取", bg="#c0c0c0")
        self.action_mode = "stop"
        threading.Thread(target=lambda: asyncio.run(self.run_crawler()), daemon=True).start()

    def toggle_action(self):
        if self.action_mode == "stop":
            self.running = False
            self.log("正在停止抓取任务...")
            self.start_btn.config(state="normal", bg="#c0c0c0")
            self.action_btn.config(text="重新运行", bg="#c0c0c0")
            self.action_mode = "restart"
        else:
            self.log("正在重新运行...")
            self.running = False
            self.captured_urls.clear()
            self.root.after(500, self.start_app)

    def get_timezone_by_lang(self, lang):
        if lang.startswith("ja"):
            return "Asia/Tokyo"
        elif lang.startswith("en"):
            return "America/New_York"
        elif lang.startswith("ru"):
            return "Europe/Moscow"
        elif lang.startswith("ko"):
            return "Asia/Seoul"
        elif lang.startswith("fr"):
            return "Europe/Paris"
        elif lang.startswith("de"):
            return "Europe/Berlin"
        return "Asia/Shanghai"

    async def run_crawler(self):
        url = self.url_entry.get().strip()
        save_dir = self.dir_var.get().strip()
        selected_lang = self.lang_var.get().strip() or "zh-CN"
        is_incognito = self.incognito_var.get()
        selected_browser = self.browser_var.get()

        if not url or not save_dir:
            self.log("错误: 请填写网址并选择目录")
            self.toggle_action()
            return

        async with async_playwright() as p:
            user_data_dir = tempfile.mkdtemp() if is_incognito else os.path.join(os.getcwd(), "browser_profile")
            launch_args = [f"--lang={selected_lang}"]
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
                    }
                )
                used_browser = selected_browser
                self.log(f"成功启动浏览器: {used_browser}")
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
                        }
                    )
                    used_browser = "Playwright Chromium"
                    self.log("已回退到 Playwright Chromium")
                except Exception as e2:
                    self.log(f"所有浏览器启动均失败: {e2}")
                    self.toggle_action()
                    return

            async def on_response(response):
                if not self.running or response.status != 200:
                    return
                r_url = response.url
                if r_url.startswith("http") and r_url not in self.captured_urls:
                    self.captured_urls.add(r_url)
                    parsed = urlparse(r_url)
                    path = parsed.path.lstrip("/")
                    path = unquote(path)
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    ext = mimetypes.guess_extension(content_type) or ""
                    if not path or path.endswith("/"):
                        path += "index" + ext
                    elif not os.path.splitext(path)[1]:
                        path += ext
                    local_path = os.path.join(save_dir, "dump", parsed.netloc or "root", path.replace("..", "_"))
                    cookies = await context.cookies()
                    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                    headers = {"User-Agent": "Mozilla/5.0", "Cookie": cookie_str}
                    with self.lock:
                        self.pending_downloads += 1
                    self.download_queue.put((r_url, local_path, headers))

            context.on("response", on_response)

            pages = context.pages
            page = pages[0] if pages else await context.new_page()

            self.log(f"已启动浏览器 [语言: {selected_lang} | 无痕模式: {is_incognito} | 浏览器: {used_browser}]")
            self.log("正在访问并监听资源...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            except Exception as e:
                self.log(f"页面加载异常: {e}")

            while self.running:
                await asyncio.sleep(1)

            await context.close()
            self.log("抓取循环已安全停止。")
            if self.action_mode == "stop":
                self.start_btn.config(state="normal", bg="#c0c0c0")
                self.action_btn.config(text="重新运行", bg="#c0c0c0")
                self.action_mode = "restart"

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()