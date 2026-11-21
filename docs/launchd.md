# 用 launchd 开机自启（可选）

如果你希望 **登录 macOS 后自动启动后端**，不再双击脚本，用 `launchd`。

> 这会常驻一个 uvicorn 进程，并绑定 `127.0.0.1:8000`。  
> 开发时改代码后需要重启（见文末），所以日常开发更推荐 VS Code 的 `F5` / `dev: start`。

## 1. 后端 plist

创建：

```bash
mkdir -p ~/Library/LaunchAgents "$HOME/Desktop/YPCX/.run"
```

文件 `~/Library/LaunchAgents/com.ypcx.backend.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ypcx.backend</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/xiaoxu/Desktop/YPCX/venv/bin/uvicorn</string>
    <string>main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/xiaoxu/Desktop/YPCX</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/xiaoxu/Desktop/YPCX/.run/backend.launchd.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/xiaoxu/Desktop/YPCX/.run/backend.launchd.err.log</string>
</dict>
</plist>
```

加载并启动：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.ypcx.backend.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ypcx.backend.plist
launchctl kickstart -k gui/$(id -u)/com.ypcx.backend
```

检查：

```bash
launchctl print gui/$(id -u)/com.ypcx.backend | head -30
curl -sS http://127.0.0.1:8000/docs -o /dev/null -w '%{http_code}\n'
```

停止 / 取消自启：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.ypcx.backend.plist
```

## 2. 前端

`index.html` 不需要服务器，直接打开即可。  
如果希望登录后自动打开：

```bash
osascript -e 'tell application "Safari" to open POSIX file "/Users/xiaoxu/Desktop/YPCX/index.html"'
```

或者把 `index.html` 拖到 Dock / 浏览器书签，点一下即可。

## 3. 开发时重启

launchd 版本不带 `--reload`，改完代码要：

```bash
launchctl kickstart -k gui/$(id -u)/com.ypcx.backend
```

看日志：

```bash
tail -f /Users/xiaoxu/Desktop/YPCX/.run/backend.launchd.log
tail -f /Users/xiaoxu/Desktop/YPCX/.run/backend.launchd.err.log
```

## 4. 注意

- plist 里的路径是绝对路径；换目录或重建 venv 后要改。
- `KeepAlive=true` 会在进程退出后自动拉起；调试崩溃时先 `bootout` 停止。
- 如果 8000 被占用，launchd 会反复重启；先确认端口空闲。
- 不想开机自启就用 VS Code 的 `F5` / `dev: start`，不要加载 plist。
