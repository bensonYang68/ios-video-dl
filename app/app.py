"""ios-video-dl：iPhone 快捷指令用的自建视频下载服务。
接收链接 -> 后台下载（抖音走可选的 Douyin_TikTok_Download_API v5，其余 yt-dlp）-> 返回文件地址。

POST /api        form/json: text=<分享文本或链接>
                 -> {"code":200,"id":"...","msg":"给手机看的提示","reused":false}
GET  /api?id=... -> {"code":200,"status":"running|done|error","files":[url...],"msg":"...",
                     "progress":0-100,"stage":"⏬ 下载中 50%（…）","notify":"有新进展时才有，取走即清空"}
同一个链接（去掉追踪参数后）正在下载就复用该任务，已下载完且文件还在就直接返回，失败了才重下。
请求需带请求头 X-Token。文件由 nginx 直接从 FILES_DIR 提供，VDL_KEEP_MINUTES（默认 60）分钟后自动删除。
"""
import json, os, re, secrets, shutil, subprocess, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["VDL_TOKEN"]
PUBLIC_BASE = os.environ["VDL_PUBLIC_BASE"].rstrip("/")  # 例如 https://example.com/<随机路径>/f
FILES_DIR = "/data/files"
COOKIES_DIR = "/data/cookies"
KEEP_SECONDS = int(os.environ.get("VDL_KEEP_MINUTES", "60")) * 60  # 下载文件保留多久（分钟），过期自动删除
URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、]+")
MEDIA_EXT = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif"}

jobs = {}     # job_id -> 任务状态；以 _ 开头的键只在服务端用
by_key = {}   # 规范化链接 -> job_id（去重）
lock = threading.Lock()


# 站点 -> cookie 文件名（/data/cookies/<名>.txt，Netscape 格式）；x.com 和 twitter.com 共用 twitter.txt
COOKIE_FILES = {"x.com": "twitter", "twitter.com": "twitter", "youtube.com": "youtube",
                "instagram.com": "instagram", "tiktok.com": "tiktok", "bilibili.com": "bilibili"}


def cookie_args(url):
    host = urllib.parse.urlparse(url).hostname or ""
    for domain, name in COOKIE_FILES.items():
        if host == domain or host.endswith("." + domain):
            path = os.path.join(COOKIES_DIR, name + ".txt")
            if os.path.exists(path):
                return ["--cookies", path]
    return []


TWEET_RE = re.compile(r"(?:x|twitter)\.com/[^?#]*status(?:es)?/(\d+)")


def url_key(url):
    """去重用的规范化链接：去掉 ?s=46、utm_* 等分享追踪参数，x.com / twitter.com、www. / m. 视为同一个"""
    m = TWEET_RE.search(url)
    if m:
        return "x.com/status/" + m.group(1)
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    keep = {"v"} if host.endswith("youtube.com") else set()  # YouTube 的视频 ID 在 ?v= 里
    query = urllib.parse.urlencode(sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query) if k in keep))
    return host + p.path.rstrip("/") + ("?" + query if query else "")


# ---------- 进度 ----------

def set_progress(job_id, stage, pct=None, done=0, total=0, milestones=True):
    """更新进度文字；到 25/50/75% 或进入新阶段时留一条 notify，快捷指令取走后清空（避免通知刷屏）"""
    text = stage
    if pct is not None:
        pct = max(0, min(100, int(pct)))
        text += f" {pct}%"
        if total:
            text += f"（{done / 1e6:.1f}/{total / 1e6:.1f}MB）"
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["stage"] = text
        if pct is not None:
            job["progress"] = pct
        if not milestones:
            return
        mark = (stage, pct // 25 * 25 if pct is not None else -1)
        if pct is not None and (pct < 25 or pct >= 100):
            return  # 0% 和 100% 不单独提醒：开头有「已提交」，结尾有「服务器已下完」
        if mark != job.get("_mark"):
            job["_mark"] = mark
            job["notify"] = text


def fetch_file(job_id, src, path, label, headers=None):
    """流式下载一个文件并汇报进度（抖音、推特兜底用；yt-dlp 自己有进度）"""
    from curl_cffi import requests
    r = requests.get(src, headers=headers, impersonate="chrome", timeout=300, stream=True)
    try:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        total, done, last = int(r.headers.get("content-length") or 0), 0, 0.0
        with open(path, "wb") as f:
            for chunk in r.iter_content():
                f.write(chunk)
                done += len(chunk)
                if total and time.time() - last > 1:
                    set_progress(job_id, label, done * 100 / total, done, total)
                    last = time.time()
    finally:
        r.close()
    if not done:
        raise RuntimeError("下载到的文件是空的")


def twitter_fallback(job_id, url, out_dir):
    """yt-dlp 只管视频；纯图片推文用公开的 syndication 接口取原图（视频也一并取最高码率 mp4）"""
    from curl_cffi import requests
    tid = TWEET_RE.search(url).group(1)
    r = requests.get("https://cdn.syndication.twimg.com/tweet-result",
                     params={"id": tid, "token": "a"}, impersonate="chrome", timeout=20)
    media = r.json().get("mediaDetails") or []
    files = []
    for i, m in enumerate(media, 1):
        if m.get("type") == "photo":
            src, ext = m["media_url_https"] + "?name=orig", os.path.splitext(m["media_url_https"])[1] or ".jpg"
        else:
            mp4 = [v for v in m.get("video_info", {}).get("variants", []) if v.get("content_type") == "video/mp4"]
            if not mp4:
                continue
            src, ext = max(mp4, key=lambda v: v.get("bitrate", 0))["url"], ".mp4"
        name = f"{tid}_{i:02d}{ext}"
        label = "⏬ 下载中" if len(media) == 1 else f"⏬ 下载第 {i}/{len(media)} 个"
        fetch_file(job_id, src, os.path.join(out_dir, name), label)
        files.append(name)
    return files


DOUYIN_RE = re.compile(r"(?:^|[/.])(?:douyin|iesdouyin)\.com/")
DTK_URL = os.environ.get("DTK_API_URL", "http://dtk-api-1:8000")
DTK_KEY = os.environ.get("DTK_API_KEY", "")
DY_HEADERS = {"Referer": "https://www.douyin.com/"}


def douyin_fetch(job_id, url, out_dir):
    """抖音走自建的 Douyin_TikTok_Download_API v5（安装时加 --with-douyin）：它负责签名和身份池，返回无水印地址"""
    from curl_cffi import requests
    if not DTK_KEY:
        raise RuntimeError("未启用抖音解析：安装时加 --with-douyin，并用 vdlctl douyin-key 设置 API Key")
    set_progress(job_id, "🔍 解析抖音链接", milestones=False)
    h = {"X-API-Key": DTK_KEY}
    r = requests.post(f"{DTK_URL}/api/v1/parse", json={"url": url}, headers=h, timeout=30).json()
    if not r.get("success"):
        raise RuntimeError("抖音解析失败：" + str((r.get("error") or {}).get("message") or r))
    task_url = f"{DTK_URL}/api/v1/tasks/{r['data']['task_id']}"
    for _ in range(60):
        task = requests.get(task_url, headers=h, timeout=20).json()["data"]
        if task["state"] not in ("queued", "running"):
            break
        time.sleep(1)
    if task["state"] != "done":
        err = task.get("error") or {}
        raise RuntimeError(f"抖音解析失败：{err.get('code', task['state'])} {err.get('message', '')}".strip())
    data = task["data"]
    media = data.get("media") or {}
    # 图集下原图，否则下视频（media.video 为无水印 1080p mp4）
    if media.get("images"):
        items = [([m["url"], *m.get("urls", [])], "." + (m.get("format") or "jpg").lower()) for m in media["images"]]
    elif media.get("video"):
        v = media["video"]
        items = [([v["url"], *v.get("urls", [])], "." + (v.get("format") or "mp4").lower())]
    else:
        raise RuntimeError("抖音解析成功但没有可下载的媒体")
    cid = data.get("content_id") or "douyin"
    files = []
    for i, (candidates, ext) in enumerate(items, 1):
        name = f"{cid}_{i:02d}{ext}"
        label = "⏬ 下载中" if len(items) == 1 else f"⏬ 下载第 {i}/{len(items)} 个"
        for src in dict.fromkeys(candidates):  # 去重并按顺序尝试备用地址
            try:
                fetch_file(job_id, src, os.path.join(out_dir, name), label, DY_HEADERS)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            raise RuntimeError(f"抖音文件下载失败（第 {i} 个）")
        files.append(name)
    return files


# yt-dlp 每行进度：已下字节 总字节 估计总字节 分片序号 分片总数 视频编码（音频流为 none）
PROGRESS_TPL = ("download:VDLP %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                "%(progress.total_bytes_estimate)s %(progress.fragment_index)s "
                "%(progress.fragment_count)s %(info.vcodec)s")


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def run_ytdlp(job_id, cmd, timeout=900):
    """运行 yt-dlp 并解析进度行；返回 (退出码, 日志)。超时抛 TimeoutExpired"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    timed_out = []
    killer = threading.Timer(timeout, lambda: (timed_out.append(1), proc.kill()))
    killer.start()
    log, last = [], 0.0
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("VDLP "):
                parts = (line.split() + ["NA"] * 7)[1:7]
                done, total, est, frag_i, frag_n = (_num(x) for x in parts[:5])
                total = total or est
                pct = done * 100 / total if done and total else (frag_i * 100 / frag_n if frag_i and frag_n else None)
                if pct is not None and time.time() - last > 1:
                    audio = parts[5] == "none"
                    set_progress(job_id, "⏬ 下载音频" if audio else "⏬ 下载中", pct,
                                 done or 0, total or 0, milestones=not audio)
                    last = time.time()
                continue
            log.append(line)
            if line.startswith("[Merger]"):
                set_progress(job_id, "🎞 合并音视频…")
        proc.wait()
    finally:
        killer.cancel()
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return proc.returncode, "\n".join(log[-200:])


VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
IOS_CODECS = {"h264", "hevc"}


def ensure_ios_compatible(job_id, out_dir, files):
    """iPhone 相册只认 H.264 / HEVC；VP9、AV1 等转成 H.264 mp4，否则「存储到相簿」会失败"""
    result = []
    for name in files:
        path = os.path.join(out_dir, name)
        if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
            result.append(name)
            continue
        codec = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=60).stdout.strip()
        if codec in IOS_CODECS and name.lower().endswith(".mp4"):
            result.append(name)
            continue
        set_progress(job_id, "🔄 转码中（原格式 iPhone 不支持，视频越长越慢）…")
        out_name = os.path.splitext(name)[0] + "_ios.mp4"
        vcodec = ["-c:v", "copy"] if codec in IOS_CODECS else \
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, *vcodec, "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", os.path.join(out_dir, out_name)],
                       check=True, capture_output=True, timeout=1800)
        os.remove(path)
        result.append(out_name)
    return result


def run_job(job_id, url):
    out_dir = os.path.join(FILES_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "yt-dlp", "--no-playlist", "--no-mtime", "--no-colors",
        "--newline", "--progress", "--progress-template", PROGRESS_TPL,
        "--impersonate", "chrome",
        # 相册兼容性：优先 H.264 mp4，最高 1080p
        # height<=?：高度未知也放行（Instagram 的 H.264 完整 mp4 不标高度，否则会退到 VP9）
        "-f", "bv*[vcodec^=avc][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=?1080]/bv*[height<=1080]+ba/b",
        "--merge-output-format", "mp4",
        "-o", os.path.join(out_dir, "%(id).60s_%(autonumber)s.%(ext)s"),
        *cookie_args(url), url,
    ]
    log = ""
    try:
        if DOUYIN_RE.search(url):
            files = douyin_fetch(job_id, url, out_dir)
        else:
            set_progress(job_id, "🔍 解析链接", milestones=False)
            _, log = run_ytdlp(job_id, cmd)
            files = sorted(f for f in os.listdir(out_dir) if os.path.splitext(f)[1].lower() in MEDIA_EXT)
        if not files and TWEET_RE.search(url):
            files = twitter_fallback(job_id, url, out_dir)
        files = ensure_ios_compatible(job_id, out_dir, files)
        if files:
            urls = [f"{PUBLIC_BASE}/{job_id}/{urllib.parse.quote(f)}" for f in files]
            size = sum(os.path.getsize(os.path.join(out_dir, f)) for f in files) / 1e6
            result = {"status": "done", "files": urls, "msg": "ok", "progress": 100,
                      "stage": "服务器已下完", "notify": f"📲 服务器已下完（{size:.1f}MB），正在传到手机…"}
            # f1、f2…：快捷指令用计数器按键名逐个取（iOS 的「重复项目」变量导入后会失效）
            result.update({f"f{i}": u for i, u in enumerate(urls, 1)})
        else:
            err = [l for l in log.splitlines() if "ERROR" in l]
            result = {"status": "error", "files": [], "msg": (err[-1] if err else log[-300:]) or "下载失败"}
    except subprocess.TimeoutExpired:
        result = {"status": "error", "files": [], "msg": "下载或转码超时"}
    except Exception as e:  # noqa: BLE001
        result = {"status": "error", "files": [], "msg": str(e)}
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job.update(result)
        if result["status"] == "error":
            job.pop("notify", None)
            if by_key.get(job.get("_key")) == job_id:
                by_key.pop(job["_key"])  # 失败的不参与去重，下次重新下载


def cleaner():
    while True:
        now = time.time()
        for d in os.listdir(FILES_DIR):
            path = os.path.join(FILES_DIR, d)
            if now - os.path.getmtime(path) > KEEP_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        with lock:
            for k in [k for k, v in jobs.items() if now - v["t"] > KEEP_SECONDS]:
                job = jobs.pop(k)
                if by_key.get(job.get("_key")) == k:
                    by_key.pop(job["_key"])
        time.sleep(min(300, max(60, KEEP_SECONDS // 6)))  # 检查间隔：保留时长的 1/6，1～5 分钟


class Handler(BaseHTTPRequestHandler):
    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return True
        self.reply({"code": 403, "msg": "forbidden"}, 403)
        return False

    def do_POST(self):
        if not self.authed():
            return
        raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)).decode("utf-8", "replace")
        if "json" in (self.headers.get("Content-Type") or ""):
            text = str(json.loads(raw or "{}").get("text", ""))
        else:
            text = urllib.parse.parse_qs(raw).get("text", [raw])[0]
        m = URL_RE.search(text)
        if not m:
            return self.reply({"code": 400, "msg": "没找到链接，请先复制视频链接"})
        url, key = m.group(0), url_key(m.group(0))
        with lock:
            old_id = by_key.get(key)
            old = jobs.get(old_id or "")
            if old and old["status"] == "running":
                return self.reply({"code": 200, "id": old_id, "reused": True,
                                   "msg": f"⏳ 这个链接已经在下载了（{old.get('stage', '准备中')}），接着等它"})
            if old and old["status"] == "done" and os.path.isdir(os.path.join(FILES_DIR, old_id)):
                return self.reply({"code": 200, "id": old_id, "reused": True,
                                   "msg": "♻️ 这个链接刚下载过，直接取文件"})
            job_id = secrets.token_urlsafe(12)
            jobs[job_id] = {"status": "running", "files": [], "msg": "", "progress": 0, "stage": "排队中",
                            "t": time.time(), "url": url, "_key": key}
            by_key[key] = job_id
        threading.Thread(target=run_job, args=(job_id, url), daemon=True).start()
        self.reply({"code": 200, "id": job_id, "reused": False, "msg": "📥 已提交，服务器开始下载…"})

    def do_GET(self):
        if not self.authed():
            return
        job_id = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
        with lock:
            job = jobs.get(job_id)
            if job is None:
                return self.reply({"code": 404, "status": "error", "files": [], "msg": "任务不存在"})
            out = {k: v for k, v in job.items() if k != "t" and not k.startswith("_") and v != ""}
            job.pop("notify", None)  # 通知只推一次
        self.reply({"code": 200, **out})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.makedirs(FILES_DIR, exist_ok=True)
    threading.Thread(target=cleaner, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
