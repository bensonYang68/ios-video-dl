# ios-video-dl

在自己的 VPS 上搭一个视频下载服务，配合 iPhone 快捷指令使用：在 App 里点「分享」→ 选「视频下载」，或者复制链接后运行快捷指令，服务器下载好后自动存进手机相册。

不用给第三方付月费，下载记录也只在你自己的服务器上。

## 能下什么

| 平台 | 说明 |
|---|---|
| 推特 / X | 视频、纯图片帖（原图） |
| 抖音 | 无水印视频、图集原图（需要开启抖音模块，见下文） |
| TikTok、Instagram、YouTube | 通过 yt-dlp |
| 其他 | yt-dlp 支持的上千个网站，公开视频基本都能下 |

iPhone 相册只认 H.264 / HEVC 编码：服务器会优先选这两种格式，拿到 VP9、AV1 等不兼容格式时自动转码成 H.264。

## 需要准备

- 一台 VPS：Debian 或 Ubuntu，1 核 1G 就够（开抖音模块建议 2G 内存 + 10G 以上空闲硬盘）
- 一个域名（或子域名），**解析到这台 VPS**
  - 用 Cloudflare 的话，先把这条记录设成「仅 DNS」（灰色云朵），否则自动申请证书会失败
- 一台 iPhone（iOS 17 及以上）

## 一键安装

用 root 登录 VPS，执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bensonYang68/ios-video-dl/main/install.sh)
```

脚本会问你域名，然后自动完成：安装 Docker、生成密钥和随机访问路径、申请 HTTPS 证书、启动服务。结束时会显示两样东西，**导入快捷指令时要填**：

```
服务器地址：https://你的域名/随机路径
密钥：      一串 48 位字符
```

以后忘了，在服务器上执行 `vdlctl info` 就能再看到。

### 两种 HTTPS 方式

- **caddy（默认）**：服务器的 80/443 端口没被占用时使用，自带 Caddy 自动申请和续期证书，什么都不用管
- **nginx**：服务器上已经有 nginx 在用 443 时使用。脚本会生成 `/etc/nginx/snippets/ios-video-dl.conf`，你在对应域名的 `server { }` 块里加一行：

  ```nginx
  include snippets/ios-video-dl.conf;
  ```

  然后执行 `nginx -t && systemctl reload nginx`

也可以用参数跳过提问：

```bash
bash install.sh --domain dl.example.com --mode caddy --yes
```

## 导入快捷指令

1. 用 iPhone 的 Safari 打开本仓库里的 [`shortcut/视频下载.shortcut`](shortcut/视频下载.shortcut)，点「下载」，然后在弹出的页面里点「添加快捷指令」（或者在「文件」App 里点开它）
2. 导入时会弹出两个问题：分别填服务器地址和密钥（就是安装结束时显示的那两项）
3. 在快捷指令列表里长按「视频下载」→「详细信息」，确认打开了「在共享表单中显示」

使用方法：

- 在推特、抖音等 App 里点「分享」，在分享菜单里选「视频下载」
- 或者先复制链接，再在快捷指令 App（或桌面、控制中心）里运行「视频下载」

第一次运行时，iOS 会询问是否允许访问你的域名、是否允许保存到相册，都点允许。

## 抖音模块（可选）

抖音的网页接口有签名和风控，yt-dlp 下不了。这里借助开源项目 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)（v5）解析：只部署它的数据库、Redis、api、worker，不装浏览器容器。

```bash
bash install.sh --with-douyin
```

- **资源**：多占约 4.5G 硬盘（主要是数据库镜像）、约 300M 内存；内存小于 3.5G 时脚本会自动把 swap 加到 2G
- **一次性配置**：装完后还要配置一次（创建管理员、导入抖音 cookie、创建 API Key），执行 `vdlctl douyin-guide` 查看详细步骤
- **cookie 会过期**：大约两个月，过期后在控制台重新导入就行
- **cookie 的风险**：它等同于你的抖音登录凭证，**建议用小号**

## 管理命令

```
vdlctl info               显示快捷指令要填的服务器地址和密钥
vdlctl test <链接>        在服务器上测试下载
vdlctl status             查看服务状态和硬盘
vdlctl logs / restart     日志 / 重启
vdlctl update             更新代码并重建（yt-dlp 每次启动也会自动升级）
vdlctl douyin-guide       抖音模块配置步骤
vdlctl douyin-key <key>   设置抖音解析的 API Key
vdlctl uninstall          卸载
```

## 工作原理

```
iPhone 快捷指令 ──POST 链接──▶ /随机路径/api（需带 X-Token 请求头）
                                  │ 后台下载：抖音 → Douyin_TikTok_Download_API
                                  │           其他 → yt-dlp（+ 推特图片兜底、iPhone 兼容转码）
快捷指令轮询任务状态 ◀────────────┘
快捷指令下载 /随机路径/f/... 的文件 → 存到相册
```

- 服务只接受带正确密钥的请求；下载好的文件放在随机路径下，**1 小时后自动删除**
- 服务端是一个只依赖 Python 标准库的小程序：[`app/app.py`](app/app.py)
- 快捷指令由 [`shortcut/make_shortcut.py`](shortcut/make_shortcut.py) 生成，在 macOS 上用 `shortcuts sign` 签名

## 常见问题

**快捷指令提示「下载失败」**
弹窗里就是服务器返回的原因。可以在服务器上执行 `vdlctl test <同一个链接>` 复现问题。网站改版导致 yt-dlp 失效时，执行 `vdlctl restart` 就会自动升级 yt-dlp。

**推特报「No video could be found in this tweet」**
如果推文确实有视频，一般是因为它被标成了敏感内容，或者只有登录用户才能看，服务器以游客身份拿不到。解决办法是给服务器放一份推特 cookie（**建议用小号**）：在电脑浏览器登录 x.com，按 F12 → Application → Cookies，复制 `auth_token` 和 `ct0` 两个值，然后在服务器上执行（把中文换成对应的值）：

```bash
printf '# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2000000000\tauth_token\t%s\n.x.com\tTRUE\t/\tTRUE\t2000000000\tct0\t%s\n' '粘贴auth_token' '粘贴ct0' > /opt/ios-video-dl/data/cookies/twitter.txt && chmod 600 /opt/ios-video-dl/data/cookies/twitter.txt
```

另外，还要在 X 的设置里打开「显示可能包含敏感内容的媒体」。其他需要登录的网站同理：cookie 按 Netscape 格式保存到 `data/cookies/<站点>.txt`，支持 `twitter`、`youtube`、`instagram`、`tiktok`、`bilibili`。

**下载成功，但存不进相册**
一般是编码不兼容。服务器会自动转码；如果还是不行，请提 issue，并附上链接。

**长视频等待超时**
快捷指令最多等约 5 分钟。可以稍后再运行一次，或者在服务器上用 `vdlctl test` 下载。

**Telegram**
只支持公开频道的公开帖子。开了「限制保存内容」的频道或群组不支持，也不会支持。

## 声明

本项目仅供个人学习和备份自己有权保存的内容。请遵守各平台的服务条款和当地法律，尊重创作者的版权，不要把下载的内容用于二次分发或商业用途。

## 致谢

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
- [Caddy](https://caddyserver.com/)

## 许可证

[MIT](LICENSE)
