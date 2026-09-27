"""ios-video-dl：iPhone 快捷指令用的自建视频下载服务。
接收链接 -> 后台下载（抖音走可选的 Douyin_TikTok_Download_API v5，其余 yt-dlp）-> 返回文件地址。

POST /api        form/json: text=<分享文本或链接>   -> {"code":200,"id":"..."}
GET  /api?id=... -> {"code":200,"status":"running|done|error","files":[url...],"msg":"..."}
请求需带请求头 X-Token。文件由 nginx 直接从 FILES_DIR 提供，1 小时后自动删除。
"""
import json, os, re, secrets, shutil, subprocess, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["VDL_TOKEN"]
PUBLIC_BASE = os.environ["VDL_PUBLIC_BASE"].rstrip("/")  # 例如 https://example.com/<随机路径>/f
FILES_DIR = "/data/files"
COOKIES_DIR = "/data/cookies"
KEEP_SECONDS = 3600
URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、]+")
MEDIA_EXT = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif"}

jobs = {}
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


def twitter_fallback(url, out_dir):
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
        data = requests.get(src, impersonate="chrome", timeout=120).content
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(data)
        files.append(name)
    return files


DOUYIN_RE = re.compile(r"(?:^|[/.])(?:douyin|iesdouyin)\.com/")
DTK_URL = os.environ.get("DTK_API_URL", "http://dtk-api-1:8000")
DTK_KEY = os.environ.get("DTK_API_KEY", "")
DY_HEADERS = {"Referer": "https://www.douyin.com/"}


def douyin_fetch(url, out_dir):
    """抖音走自建的 Douyin_TikTok_Download_API v5（安装时加 --with-douyin）：它负责签名和身份池，返回无水印地址"""
    from curl_cffi import requests
    if not DTK_KEY:
        raise RuntimeError("未启用抖音解析：安装时加 --with-douyin，并用 vdlctl douyin-key 设置 API Key")
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
        for src in dict.fromkeys(candidates):  # 去重并按顺序尝试备用地址
            try:
                resp = requests.get(src, headers=DY_HEADERS, impersonate="chrome", timeout=180)
                if resp.status_code == 200 and resp.content:
                    break
            except Exception:  # noqa: BLE001
                continue
        else:
            raise RuntimeError(f"抖音文件下载失败（第 {i} 个）")
        name = f"{cid}_{i:02d}{ext}"
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(resp.content)
        files.append(name)
    return files


VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
IOS_CODECS = {"h264", "hevc"}


def ensure_ios_compatible(out_dir, files):
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
        "yt-dlp", "--no-playlist", "--no-progress", "--no-mtime",
        "--impersonate", "chrome",
        # 相册兼容性：优先 H.264 mp4，最高 1080p
        # height<=?：高度未知也放行（Instagram 的 H.264 完整 mp4 不标高度，否则会退到 VP9）
        "-f", "bv*[vcodec^=avc][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=?1080]/bv*[height<=1080]+ba/b",
        "--merge-output-format", "mp4",
        "-o", os.path.join(out_dir, "%(id).60s_%(autonumber)s.%(ext)s"),
        *cookie_args(url), url,
    ]
    try:
        if DOUYIN_RE.search(url):
            p, files = None, douyin_fetch(url, out_dir)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            files = sorted(f for f in os.listdir(out_dir) if os.path.splitext(f)[1].lower() in MEDIA_EXT)
        if not files and TWEET_RE.search(url):
            files = twitter_fallback(url, out_dir)
        files = ensure_ios_compatible(out_dir, files)
        if files:
            urls = [f"{PUBLIC_BASE}/{job_id}/{urllib.parse.quote(f)}" for f in files]
            result = {"status": "done", "files": urls, "msg": "ok"}
            # f1、f2…：快捷指令用计数器按键名逐个取（iOS 的「重复项目」变量导入后会失效）
            result.update({f"f{i}": u for i, u in enumerate(urls, 1)})
        else:
            stderr = p.stderr if p else ""
            err = [l for l in stderr.splitlines() if "ERROR" in l]
            result = {"status": "error", "files": [], "msg": (err[-1] if err else stderr[-300:]) or "下载失败"}
    except subprocess.TimeoutExpired:
        result = {"status": "error", "files": [], "msg": "下载或转码超时"}
    except Exception as e:  # noqa: BLE001
        result = {"status": "error", "files": [], "msg": str(e)}
    with lock:
        jobs[job_id].update(result)


def cleaner():
    while True:
        now = time.time()
        for d in os.listdir(FILES_DIR):
            path = os.path.join(FILES_DIR, d)
            if now - os.path.getmtime(path) > KEEP_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        with lock:
            for k in [k for k, v in jobs.items() if now - v["t"] > KEEP_SECONDS]:
                jobs.pop(k)
        time.sleep(300)


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
        job_id = secrets.token_urlsafe(12)
        with lock:
            jobs[job_id] = {"status": "running", "files": [], "msg": "", "t": time.time(), "url": m.group(0)}
        threading.Thread(target=run_job, args=(job_id, m.group(0)), daemon=True).start()
        self.reply({"code": 200, "id": job_id})

    def do_GET(self):
        if not self.authed():
            return
        job_id = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
        with lock:
            job = dict(jobs.get(job_id) or {})
        if not job:
            return self.reply({"code": 404, "status": "error", "files": [], "msg": "任务不存在"})
        job.pop("t", None)
        self.reply({"code": 200, **job})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.makedirs(FILES_DIR, exist_ok=True)
    threading.Thread(target=cleaner, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
