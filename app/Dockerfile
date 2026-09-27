FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "yt-dlp[default,curl-cffi]"
COPY app.py /app/app.py
# 每次启动先升级 yt-dlp（各网站经常改版，老版本容易失效）
CMD pip install --no-cache-dir -q -U "yt-dlp[default,curl-cffi]" ; exec python -u /app/app.py
