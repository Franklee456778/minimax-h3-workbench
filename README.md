# MiniMax H3 工作台（Mac 版 / Windows 版源码）

当前版本：**v1.0**（2026-11-12）。版本号写在 `app/hf.py` 的 `VERSION`，界面左下角、启动提示、使用说明都显示它。

本机运行的网页工具：在浏览器里放素材、写提示词，把任务提交到 RunningHub 上的 MiniMax H3 工作流，按采下载成片。只听 `127.0.0.1`，不对外提供服务。

Mac 版和 Windows 版的程序代码完全相同（`app/`），只有启动脚本、使用说明和包里带的 FFmpeg 构建不同。

## 这个仓库里有什么

| 路径 | 内容 |
|---|---|
| `app/hf.py` | 后台：本机 HTTP 服务、项目、素材、任务、文件读取 |
| `app/rh.py` | RunningHub 接口：API Key 存取、上传、提交、实时进度、下载 |
| `app/web/index.html` | 界面（单文件，原生 JS，不加载外部资源） |
| `app/RunningHub工作流/` | 工作流结构模板（不含 workflowId） |
| `mac/启动.command` | Mac 启动脚本（Apple 芯片） |
| `windows/启动.bat` | Windows 启动脚本（64 位） |
| `mac/` `windows/` 里的 `使用说明.txt` | 给使用者的说明 |
| `mac/LICENSES.txt` `windows/LICENSES.txt` | 免安装包里带的第三方软件及许可 |
| `打包/打包.py` | 生成两个免安装包的脚本（含运行文件的 SHA-256 校验表） |

免安装包里的运行环境（Python 3.13、FFmpeg、websockets、certifi）和 `data/` 数据文件夹不在仓库里。免安装包的结构是：启动脚本和使用说明在最外层，旁边是 `app/`、`runtime/`、`data/`。

## 两个版本的差别

| | Mac 版 | Windows 版 |
|---|---|---|
| API Key 存放 | 系统钥匙串（`security` 命令，经标准输入写入） | `data/_缓存/api_key.bin`，DPAPI 加密，只有本机当前账户能解开 |
| 系统通知 | `osascript` | PowerShell 气泡通知（标题正文经环境变量传） |
| 选文件夹 | `osascript choose folder` | PowerShell `FolderBrowserDialog` |
| 在文件管理器里显示 | `open` / `open -R`（带扩展名的包文件夹只选中不打开） | `os.startfile` / `explorer /select`（系统程序用完整路径） |
| FFmpeg | ffmpeg.martin-riedl.de，macOS arm64，9.0.2 | BtbN/FFmpeg-Builds，win64-gpl-shared，9.0 |

## 审核时可以先看的地方

- 来源检查：`hf.py` 的 `request_allowed`（Host 和 Origin 必须是本机端口）
- 文件读取和打开范围：`hf.py` 的 `allowed_file`、`revealable`、`reveal`
- 响应头和内容安全策略：`hf.py` 的 `Handler.security_headers`
- 文件名净化：`hf.py` 的 `safe_name`
- Windows 系统程序路径：`hf.py` 的 `win_tool`
- API Key：`rh.py` 的 `_store_read` / `_store_write` / `_dpapi`，不写进任务记录和日志
- 对外连接：`rh.py` 的 `_open`（只连 `www.runninghub.ai`）、`_fetch`（下载只认 https）、`ws_listen`（只认 wss）

## 直接从源码运行

需要 Python 3.10 以上，以及 `ffmpeg`、`websockets`、`certifi`：

```bash
python3 app/hf.py --演示
```

`--演示` 不连 RunningHub、不扣费。
