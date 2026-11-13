#!/usr/bin/env python3
"""MiniMax H3 工作台的本机后台（弹出网页来用）。

  python3 hf.py            开窗口（只在本机 127.0.0.1:8790，浏览器自动打开）
  python3 hf.py --演示     演示模式：不连 RunningHub、不扣费，生成的是测试画面

H3 在 RunningHub（ComfyUI 工作流）上跑，接口见 rh.py；结构模板在 RunningHub工作流/，工作流 ID 存在 工作流库.json。
"""
import hashlib
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import rh

ROOT = Path(__file__).resolve().parent          # 程序所在
DATA = Path(os.environ.get("H3_DATA") or ROOT).resolve()  # 数据（项目、工作流库、缓存、出片）；免安装包里是旁边的「数据」文件夹
WEB = ROOT / "web"
CACHE = DATA / "_缓存"
ASSETS = CACHE / "素材"
TASKS_FILE = CACHE / "任务.json"
UPLOADS_FILE = CACHE / "上传记录.json"
SETTINGS_FILE = CACHE / "设置.json"
DEFAULT_OUT = DATA / "出片"
PROJECTS_FILE = DATA / "项目.json"
WIN, MAC = sys.platform == "win32", sys.platform == "darwin"

PORT = int(os.environ.get("H3_PORT") or 8790)  # 8765 是 05-电影 预演服务器在用；H3_PORT 可以临时换端口
VERSION = "1.0"  # 版本号：每次给别人发新包前加一（1.1、1.2…），界面左下角、使用说明、启动提示都读这里
UA = f"h3-workbench/{VERSION}"
MOCK = "--演示" in sys.argv

# 这些格式直接上传；其他格式先用 ffmpeg / sips 转
UPLOAD_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                ".gif": "image/gif", ".wav": "audio/wav", ".mp4": "video/mp4"}
KIND_BY_EXT = {**{e: "image" for e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".tif", ".tiff", ".bmp")},
               **{e: "video" for e in (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm")},
               **{e: "audio" for e in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff")}}
TERMINAL = {"completed", "failed", "nsfw", "canceled"}
MAX_COUNT = 10  # 一次最多生成几条（Peter 2026-11-12 定 10，原来 4）

lock = threading.RLock()


def _use_roots():
    """给 urllib 配根证书：优先用 certifi（免安装包里自带）；没有就在 Mac 上从系统钥匙串导出一份。"""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        if MAC:
            pem = CACHE / "系统根证书.pem"
            if not pem.exists() or time.time() - pem.stat().st_mtime > 30 * 86400:
                pem.parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(["security", "find-certificate", "-a", "-p",
                                    "/System/Library/Keychains/SystemRootCertificates.keychain"], capture_output=True, text=True)
                pem.write_text(r.stdout, encoding="utf-8")
            ctx.load_verify_locations(cafile=str(pem))
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))


_use_roots()


# ---------- 小工具 ----------

HERE = "<数据>"  # 存盘时数据目录写成这个记号；整个文件夹改名、拷到别的电脑，旧项目和旧任务的路径照样对


def posix(p):
    """路径存成正斜杠的写法（Windows 也认），网页那边取文件名才不会错。"""
    return Path(p).as_posix()


def _swap(x, a, b):
    if isinstance(x, str):
        return b + x[len(a):] if x == a or x.startswith(a + "/") else x
    if isinstance(x, list):
        return [_swap(v, a, b) for v in x]
    if isinstance(x, dict):
        return {k: _swap(v, a, b) for k, v in x.items()}
    return x


def load_json(p, default):
    try:
        return _swap(json.loads(Path(p).read_text(encoding="utf-8")), HERE, posix(DATA))
    except Exception:
        return default


def save_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(_swap(data, posix(DATA), HERE), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 失败：{r.stderr.strip()[-400:]}")
    return r.stdout


def probe(path):
    try:
        j = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_type",
                            "-of", "json", str(path)]))
        v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
        return {"秒": round(float(j.get("format", {}).get("duration", 0) or 0), 2),
                "宽": v.get("width"), "高": v.get("height")}
    except Exception:
        return {}


# ---------- 模型 ----------

def models():
    """窗口里只有 H3：工作流库里每一条是一种「方式」。"""
    return {"models": [rh.as_model()]}


# ---------- 素材 ----------

def ingest(name, src):
    """收下一个本地素材：按内容取编号，必要时转成能上传的格式，做缩略图。"""
    ext = Path(name).suffix.lower()
    kind = KIND_BY_EXT.get(ext)
    if not kind:
        raise RuntimeError(f"不支持的文件类型：{ext or name}")
    aid = sha256(src)[:16]
    meta_p = ASSETS / f"{aid}.json"
    if meta_p.exists():
        src.unlink(missing_ok=True)
        meta = load_json(meta_p, {})
        meta["名字"] = name
        return meta
    orig = ASSETS / f"{aid}{ext}"
    shutil.move(str(src), orig)
    up = orig
    if ext not in UPLOAD_TYPES:
        if kind == "video":
            up = ASSETS / f"{aid}_上传.mp4"
            run(["ffmpeg", "-y", "-v", "error", "-i", str(orig), "-c:v", "libx264", "-crf", "12", "-preset", "medium",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(up)])
        elif kind == "audio":
            up = ASSETS / f"{aid}_上传.wav"
            run(["ffmpeg", "-y", "-v", "error", "-i", str(orig), "-c:a", "pcm_s16le", "-ar", "48000", str(up)])
        else:
            up = ASSETS / f"{aid}_上传.png"
            if MAC:
                run(["sips", "-s", "format", "png", str(orig), "--out", str(up)])
            else:
                run(["ffmpeg", "-y", "-v", "error", "-i", str(orig), "-frames:v", "1", str(up)])
    thumb = None
    if kind == "image":
        thumb = up.name
    elif kind == "video":
        t = ASSETS / f"{aid}_缩略图.jpg"
        try:
            run(["ffmpeg", "-y", "-v", "error", "-ss", "0.3", "-i", str(up), "-frames:v", "1",
                 "-vf", "scale=360:-2", str(t)])
            thumb = t.name
        except RuntimeError:
            pass
    meta = {"id": aid, "名字": name, "类型": kind, "原文件": orig.name, "上传文件": up.name,
            "缩略图": thumb, "转换": up != orig, "大小": up.stat().st_size, **probe(up)}
    save_json(meta_p, meta)
    return meta


# ---------- 任务 ----------

tasks = {}  # 本地任务号 → 任务


def fav_files(t):
    """收藏按文件记（一采、二采各自收藏）。旧版只记整条任务收藏，算成收藏了它的成片。"""
    if "收藏文件" in t:
        return list(t["收藏文件"])
    return [t["视频"]] if t.get("收藏") and t.get("视频") else []


def save_tasks():
    with lock:
        save_json(TASKS_FILE, list(tasks.values()))


# ---------- 项目 ----------
# 项目只是一组任务和一个出片文件夹；记在 项目.json（进 git），删项目不删任何视频文件。

def load_projects():
    return load_json(PROJECTS_FILE, [])


def find_project(pid):
    return next((p for p in load_projects() if p["id"] == pid), None)


def project_out(pid):
    p = find_project(pid) if pid else None
    if p:
        return p["输出目录"]
    return load_json(SETTINGS_FILE, {}).get("输出目录", posix(DEFAULT_OUT))


def set_out_dir(pid, out_dir):
    if MOCK:  # 演示用的临时文件夹不记下来，免得正式出片跑到那里去
        return
    with lock:
        if pid:
            ps = load_projects()
            for p in ps:
                if p["id"] == pid:
                    p["输出目录"] = posix(out_dir)
            save_json(PROJECTS_FILE, ps)
        else:
            s = load_json(SETTINGS_FILE, {})
            s["输出目录"] = posix(out_dir)
            save_json(SETTINGS_FILE, s)


MAX_DEPTH = 4  # 项目最多四层，比如 《信》 → 第3场 → 15镜 → 试验
COLORS = ("lime", "cyan", "slate", "violet", "purple", "pink", "yellow")  # 文件夹颜色，和 Higgsfield 的选项一样


def _depth(ps, pid):
    """这个项目在第几层（顶层 = 1）。"""
    by, n = {p["id"]: p for p in ps}, 0
    while pid and pid in by and n < 10:
        n, pid = n + 1, by[pid].get("上级")
    return n


def _height(ps, pid):
    """这个项目连同下面的子项目一共几层。"""
    kids = [p["id"] for p in ps if p.get("上级") == pid]
    return 1 + max((_height(ps, k) for k in kids), default=0)


def _path_names(ps, pid):
    by, out = {p["id"]: p for p in ps}, []
    while pid and pid in by and len(out) < 10:
        out.insert(0, by[pid]["名称"])
        pid = by[pid].get("上级")
    return out


def _check_name(name):
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name:
        raise RuntimeError("项目名不能为空，也不能有 / 或 \\")
    return name


def create_project(name, parent=None, color=None):
    name = _check_name(name)
    with lock:
        ps = load_projects()
        if parent:
            if not any(p["id"] == parent for p in ps):
                raise RuntimeError("上一级项目已经不在了")
            if _depth(ps, parent) >= MAX_DEPTH:
                raise RuntimeError(f"项目最多 {MAX_DEPTH} 层，这里不能再往下建")
        out = DEFAULT_OUT.joinpath(*_path_names(ps, parent), name)
        p = {"id": uuid.uuid4().hex[:10], "名称": name, "上级": parent or None, "输出目录": posix(out),
             "创建": datetime.now().isoformat(timespec="seconds")}
        if color in COLORS:
            p["颜色"] = color
        ps.append(p)
        save_json(PROJECTS_FILE, ps)
    return p


def update_project(pid, changes):
    with lock:
        ps = load_projects()
        p = next((x for x in ps if x["id"] == pid), None)
        if not p:
            raise RuntimeError("没有这个项目")
        if "名称" in changes:
            p["名称"] = _check_name(changes["名称"])
        if changes.get("输出目录"):
            p["输出目录"] = posix(changes["输出目录"])
        if changes.get("颜色") in COLORS:
            p["颜色"] = changes["颜色"]
        if "上级" in changes:  # 拖到别的项目下面 / 拖回顶层。出片文件夹不跟着动，已出的片不搬
            new = changes["上级"] or None
            if new:
                if not any(x["id"] == new for x in ps):
                    raise RuntimeError("目标项目已经不在了")
                up, n = new, 0
                while up and n < 10:  # 不能放进自己或自己的子项目里
                    if up == pid:
                        raise RuntimeError("不能把项目放进它自己里面")
                    up, n = next((x.get("上级") for x in ps if x["id"] == up), None), n + 1
                if _depth(ps, new) + _height(ps, pid) > MAX_DEPTH:
                    raise RuntimeError(f"项目最多 {MAX_DEPTH} 层，放进去就超了")
            p["上级"] = new
        save_json(PROJECTS_FILE, ps)
    return p


def delete_project(pid):
    with lock:
        if any(t.get("项目") == pid and t["状态"] not in TERMINAL for t in tasks.values()):
            raise RuntimeError("这个项目还有任务在生成，等完成后再删")
        ps = load_projects()
        me = next((p for p in ps if p["id"] == pid), None)
        for p in ps:  # 里面的子项目升一级，不跟着删
            if me and p.get("上级") == pid:
                p["上级"] = me.get("上级")
        save_json(PROJECTS_FILE, [p for p in ps if p["id"] != pid])
        for t in tasks.values():
            if t.get("项目") == pid:
                t["项目"] = None
    save_tasks()


# ---------- 提示词片段 ----------
# 常用的写法存成片段，写提示词时点一下插到光标处。存在 数据/提示词片段.json。

SNIPPETS_FILE = DATA / "提示词片段.json"


def save_snippet(sid, b):
    with lock:
        sn = load_json(SNIPPETS_FILE, [])
        if sid:
            x = next((i for i in sn if i["id"] == sid), None)
            if not x:
                raise RuntimeError("没有这个片段")
        else:
            x = {"id": uuid.uuid4().hex[:8], "创建": datetime.now().isoformat(timespec="seconds")}
            sn.insert(0, x)
        text = (b.get("内容") if "内容" in b else x.get("内容", "")).strip()
        if not text:
            raise RuntimeError("片段内容是空的")
        name = (b.get("名称") if "名称" in b else x.get("名称", "")).strip() or text.replace("\n", " ")[:16]
        x.update(名称=name, 内容=text)
        save_json(SNIPPETS_FILE, sn)
    return x


def delete_snippet(sid):
    with lock:
        save_json(SNIPPETS_FILE, [i for i in load_json(SNIPPETS_FILE, []) if i["id"] != sid])


# ---------- 素材库 ----------

def list_assets():
    out = [load_json(p, None) for p in ASSETS.glob("*.json")]
    out = [a for a in out if a and a.get("id")]
    for a in out:
        a["时间"] = (ASSETS / f"{a['id']}.json").stat().st_mtime
    return sorted(out, key=lambda a: a["时间"], reverse=True)


def delete_asset(aid):
    if not re.fullmatch(r"[0-9a-f]{16}", aid):
        raise RuntimeError("素材编号不对")
    if any(aid in sum(t["素材"].values(), []) for t in tasks.values() if t["状态"] == "uploading"):
        raise RuntimeError("这个素材正在上传，等提交完再删")
    meta = load_json(ASSETS / f"{aid}.json", None)
    if not meta:
        return
    for key in ("原文件", "上传文件", "缩略图"):
        name = meta.get(key)
        if name and name.startswith(aid):
            (ASSETS / name).unlink(missing_ok=True)
    (ASSETS / f"{aid}.json").unlink(missing_ok=True)


def find_model(endpoint):
    for m in models()["models"]:
        for w in m["方式"]:
            if w["endpoint"] == endpoint:
                return m, w
    raise RuntimeError("这个工作流已经不在工作流库里了，换一个再交")


def submit(req):
    """网页点「确认提交」后进来：建任务，后台线程上传素材并提交 N 次。"""
    m, w = find_model(req["endpoint"])
    count = max(1, min(MAX_COUNT, int(req.get("数量", 1))))
    pid = req.get("项目") or None
    if pid and not find_project(pid):
        raise RuntimeError("这个项目已经删掉了")
    out_dir = Path(req.get("输出目录") or project_out(pid)).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    set_out_dir(pid, out_dir)
    batch = uuid.uuid4().hex[:8]
    ids = []
    now = datetime.now()
    for i in range(count):
        tid = f"{now:%m%d%H%M%S}-{batch}-{i + 1}"
        tasks[tid] = {
            "id": tid, "批次": batch, "序号": i + 1, "数量": count,
            "模型": m["名称"], "简称": m["简称"], "方式": w["名称"], "endpoint": w["endpoint"],
            "参数": req["参数"], "素材": req["素材"], "表单": req.get("表单"),
            "前缀": safe_name((req.get("前缀") or "").strip()), "输出目录": posix(out_dir),
            "估价": req.get("估价"), "状态": "uploading", "创建": now.isoformat(timespec="seconds"),
            "幂等键": str(uuid.uuid4()), "演示": MOCK, "项目": pid, "收藏": False, "收藏文件": [],
        }
        ids.append(tid)
    try:
        rh.prepare(req, m, w, [tasks[i] for i in ids])
    except Exception:
        for i in ids:
            tasks.pop(i)
        raise
    save_tasks()
    threading.Thread(target=rh.send_batch, args=(ids,), daemon=True).start()
    return ids


def _fail(tid, msg):
    t = tasks[tid]
    t["状态"] = "failed"
    t["错误"] = msg
    t["完成"] = datetime.now().isoformat(timespec="seconds")
    save_tasks()


def safe_name(s, fallback="未命名"):
    """拿来当文件名的文字：去掉路径分隔符、Windows 不允许的字符和控制字符，不让它变成「../」跑到别的文件夹。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(s or "")).strip(" .")
    return s[:120] or fallback


def out_stem(t):
    return f"{safe_name(t['前缀'])}_{safe_name(t['简称'], 'RH')}_{datetime.fromisoformat(t['创建']):%m%d-%H%M}_{t['序号']:02d}"


def unique(p):
    """同名文件已经有了就加 _2、_3…"""
    q, n = p, 2
    while q.exists():
        q, n = p.with_name(f"{p.stem}_{n}{p.suffix}"), n + 1
    return q


NOTIFY_SCRIPT = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"\nend run'
NOTIFY_PS = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
             "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
             "$n.ShowBalloonTip(6000, $env:H3_T, $env:H3_B, 'Info'); Start-Sleep -Seconds 7; $n.Dispose()")


def notify(t, stage=None):
    """任务跑完（成功或失败）弹一条系统通知，切到别的窗口也知道；一采这类中间结果先下载好了也发一条（stage = 那一采的名字）。
    设置里「完成通知」= false 可关。"""
    if not load_json(SETTINGS_FILE, {}).get("完成通知", True):
        return
    ok = t["状态"] == "completed"
    if stage:
        title = f"✓ {stage} 已下载，可以先看"
    else:
        title = f"{'✓ 生成完成' if ok else '✗ ' + {'failed': '生成失败', 'nsfw': '被内容审核拦下', 'canceled': '已取消'}.get(t['状态'], t['状态'])}"
    body = f"{t.get('前缀', '')} · {t.get('方式', '')}" + (f"（第 {t['序号']}/{t['数量']} 条）" if t.get("数量", 1) > 1 else "")
    if t.get("演示"):
        title += "（演示）"
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if MAC:
            subprocess.Popen(["osascript", "-e", NOTIFY_SCRIPT, title, body], **quiet)
        elif WIN:  # 标题和正文经环境变量传，不拼进命令里
            subprocess.Popen([win_tool("powershell"), "-NoProfile", "-WindowStyle", "Hidden", "-Command", NOTIFY_PS],
                             env={**os.environ, "H3_T": title, "H3_B": body}, creationflags=0x08000000, **quiet)
    except Exception:
        pass


def win_tool(name):
    """Windows 自带程序用完整路径调用：只写名字的话，系统会先在程序自己的文件夹和当前文件夹里找同名 .exe。"""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return {"powershell": os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
            "explorer": os.path.join(root, "explorer.exe")}[name]


def reveal(p):
    """在访达 / 资源管理器里打开文件夹，或选中文件。"""
    p = Path(p)
    if MAC:
        # 带扩展名的文件夹（.app、.pkg、.prefPane 这类「包」）只在访达里选中，不「打开」：打开它们等于运行或安装
        subprocess.run(["open", str(p)] if p.is_dir() and not p.suffix else ["open", "-R", str(p)])
    elif WIN:
        if p.is_dir():
            os.startfile(str(p))
        else:
            subprocess.run([win_tool("explorer"), "/select,", str(p)])
    else:
        subprocess.run(["xdg-open", str(p if p.is_dir() else p.parent)])


PICK_PS = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
           "$d.Description = '选择出片文件夹'; $f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; "
           "if ($d.ShowDialog($f) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Output $d.SelectedPath }")


def pick_folder():
    if MAC:
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "选择出片文件夹")'],
                           capture_output=True, text=True)
    elif WIN:
        r = subprocess.run([win_tool("powershell"), "-NoProfile", "-STA", "-Command", PICK_PS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=0x08000000)
    else:
        return None
    out = r.stdout.strip().rstrip("/\\")
    return posix(out) if out else None


def poller():
    while True:
        time.sleep(2)
        for t in list(tasks.values()):
            if t["状态"] not in ("queued", "in_progress") or time.time() < t.get("下次查", 0):
                continue
            if t.get("演示", False) != MOCK:  # 演示模式不碰真任务，正式模式不碰演示任务
                continue
            _poll_one(t)
            if t["状态"] in TERMINAL and t["状态"] != "canceled":
                notify(t)


def _poll_one(t):
    if t.get("平台") == "runninghub":
        try:
            rh.poll(t)
            t["下次查"] = time.time() + (1 if MOCK else 15)
        except Exception as e:
            t["下次查"] = time.time() + 30
            t["提示"] = f"查状态时出错，稍后重试：{e}"
        save_tasks()
        return
    # 以前经 Higgsfield 跑的任务（Seedance / Kling）：接口已经去掉，不再查
    _fail(t["id"], "这条是旧版经 Higgsfield 提交的任务，现在的工作台不再查它的状态")


def cancel(tid):
    t = tasks[tid]
    if t.get("平台") == "runninghub":
        if t["状态"] not in ("uploading", "queued", "in_progress"):
            raise RuntimeError("这条任务已经结束了")
        rh.cancel(t)
        save_tasks()
        return
    raise RuntimeError("这条是旧版经 Higgsfield 提交的任务，取消不了")


# ---------- 本机网页服务 ----------

def allowed_file(p):
    """网页只能读素材缓存和任务输出的视频。"""
    p = p.resolve()
    roots = [ASSETS.resolve()] + list({Path(t["输出目录"]).resolve() for t in tasks.values()})
    return any(p.is_relative_to(r) for r in roots) and p.is_file()


PICKED = set()


def revealable(p):
    """「在访达里显示」只认出片文件夹（首页的、各项目的、任务记录里的）和素材缓存里的东西。
    不然网页传什么路径就打开什么：在 Mac 上「打开」一个 .app 文件夹等于运行它。"""
    try:
        p = Path(p).resolve()
    except OSError:
        return False
    roots = {project_out(None)} | {x["输出目录"] for x in load_projects()} | {t["输出目录"] for t in tasks.values()} | PICKED
    roots = [Path(r).resolve() for r in roots if r] + [ASSETS.resolve()]
    if not any(p == r or p.is_relative_to(r) for r in roots):
        return False
    return not any(part.lower().endswith((".app", ".command", ".bat", ".cmd", ".exe", ".lnk", ".workflow", ".scpt")) for part in p.parts)


def request_allowed(host, origin):
    """只认本机页面发来的请求。
    服务只听 127.0.0.1 还不够：任何网页都能往这里发 POST（提交生成会花钱），
    DNS 重绑定还能让外部网页读到任务和素材。所以 Host 和 Origin 都要是本机这个端口。"""
    allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    if host not in allowed:
        return False
    return not origin or origin in {f"http://{h}" for h in allowed}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reject_foreign(self):
        if request_allowed(self.headers.get("Host", ""), self.headers.get("Origin", "")):
            return False
        self.json({"错误": "只接受本机页面的请求"}, 403)
        return True

    def send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers(ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self, ctype=""):
        """不让别的网页把工作台嵌进框里骗点击；不让浏览器猜文件类型；页面只能连本机、不能加载外面的脚本。"""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
                             "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode())

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def serve_file(self, p, ctype):
        size = p.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else size - 1
            end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_GET(self):
        if self.reject_foreign():
            return
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                s = load_json(SETTINGS_FILE, {})
                return self.json({"有RH密钥": rh.has_key(), "密钥存放": rh.key_place(), "演示": MOCK,
                                  "输出目录": s.get("输出目录", posix(DEFAULT_OUT)), "系统": "win" if WIN else "mac" if MAC else "other", "版本": VERSION})
            if u.path == "/api/snippets":
                return self.json(load_json(SNIPPETS_FILE, []))
            if u.path == "/api/account":
                return self.json(rh.account() if rh.has_key() and not MOCK else None)
            if u.path == "/api/workflows":
                return self.json(rh.lib_list())
            if u.path == "/api/models":
                return self.json(models())
            if u.path == "/api/projects":
                return self.json(load_projects())
            if u.path == "/api/assets":
                return self.json(list_assets())
            if u.path == "/api/tasks":
                with lock:
                    out = sorted(tasks.values(), key=lambda t: t["id"], reverse=True)
                return self.json([{k: v for k, v in t.items() if k not in ("请求体", "幂等键")} for t in out])
            if u.path.startswith("/api/assets/"):
                return self.json(load_json(ASSETS / f"{Path(u.path).name}.json", None) or {})
            if u.path == "/api/file":
                p = Path(unquote(parse_qs(u.query).get("p", [""])[0]))
                if not p.is_absolute():
                    p = ASSETS / p
                if not allowed_file(p):
                    return self.json({"错误": "不能读这个文件"}, 403)
                ctype = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                         ".webp": "image/webp", ".gif": "image/gif", ".wav": "audio/wav", ".mov": "video/quicktime"
                         }.get(p.suffix.lower(), "application/octet-stream")
                return self.serve_file(p, ctype)
            self.json({"错误": "没有这个地址"}, 404)
        except Exception as e:
            self.json({"错误": str(e)}, 500)

    def do_POST(self):
        if self.reject_foreign():
            return
        u = urlparse(self.path)
        try:
            if u.path == "/api/assets":
                name = unquote(self.headers.get("X-Name", "file"))
                n = int(self.headers.get("Content-Length") or 0)
                ASSETS.mkdir(parents=True, exist_ok=True)
                tmp = ASSETS / f"_收件_{uuid.uuid4().hex}"
                with open(tmp, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                try:
                    return self.json(ingest(name, tmp))
                finally:
                    tmp.unlink(missing_ok=True)
            if u.path == "/api/submit":
                return self.json({"任务": submit(self.body())})
            m = re.fullmatch(r"/api/tasks/([^/]+)/cancel", u.path)
            if m:
                cancel(m.group(1))
                return self.json({"ok": True})
            if u.path == "/api/tasks/clear":  # 清空「我的生成」：只删记录，视频文件不动；在跑的和收藏过的留着
                keep_fav = self.body().get("留收藏", True)
                with lock:
                    gone = [t["id"] for t in tasks.values() if t["状态"] in TERMINAL and bool(t.get("演示")) == MOCK
                            and not (keep_fav and fav_files(t))]
                    for tid in gone:
                        tasks.pop(tid)
                save_tasks()
                return self.json({"删了": len(gone)})
            if u.path == "/api/assets/clear":  # 清空「我的元素」：删工作台里存的副本，电脑上的原文件不动
                n, kept = 0, 0
                for aid in self.body().get("ids") or []:
                    try:
                        delete_asset(aid)
                        n += 1
                    except RuntimeError:
                        kept += 1
                return self.json({"删了": n, "留下": kept})
            m = re.fullmatch(r"/api/tasks/([^/]+)/remove", u.path)
            if m:
                with lock:
                    t = tasks.get(m.group(1))
                    if t and t["状态"] in TERMINAL:
                        tasks.pop(t["id"])
                save_tasks()
                return self.json({"ok": True})
            m = re.fullmatch(r"/api/tasks/([^/]+)/(fav|move)", u.path)
            if m:
                t = tasks.get(m.group(1))
                if not t:
                    raise RuntimeError("没有这个任务")
                b = self.body()
                if m.group(2) == "fav":
                    favs = fav_files(t)
                    f = b.get("文件") or t.get("视频")
                    if f and f not in favs and f not in [x.get("视频") for x in t.get("产出", [])] + [t.get("视频")]:
                        raise RuntimeError("这条任务里没有这个文件")
                    favs = [x for x in favs if x != f] + ([f] if b.get("收藏") and f else [])
                    t["收藏文件"] = favs
                    t["收藏"] = bool(favs)
                else:
                    if b.get("项目") and not find_project(b["项目"]):
                        raise RuntimeError("没有这个项目")
                    t["项目"] = b.get("项目") or None
                save_tasks()
                return self.json({"ok": True})
            if u.path == "/api/projects":
                b = self.body()
                return self.json(create_project(b.get("名称"), b.get("上级"), b.get("颜色")))
            m = re.fullmatch(r"/api/projects/([0-9a-f]+)(/delete)?", u.path)
            if m:
                if m.group(2):
                    delete_project(m.group(1))
                    return self.json({"ok": True})
                return self.json(update_project(m.group(1), self.body()))
            m = re.fullmatch(r"/api/assets/([0-9a-f]+)/delete", u.path)
            if m:
                delete_asset(m.group(1))
                return self.json({"ok": True})
            if u.path == "/api/reveal":
                p = Path(self.body()["p"])
                if not revealable(p):
                    raise RuntimeError("只能打开工作台自己的出片文件夹和素材")
                known = {project_out(None)} | {x["输出目录"] for x in load_projects()}
                if posix(p) in known and not p.exists():  # 出片文件夹还没出过片时先建出来
                    p.mkdir(parents=True, exist_ok=True)
                if p.exists():
                    reveal(p)
                return self.json({"ok": True})
            if u.path == "/api/quit":  # 网页上的「退出工作台」：回完话再停，停了就不再连 RunningHub
                with lock:
                    save_tasks()
                self.json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if u.path == "/api/pick-folder":
                picked = pick_folder()
                if picked:
                    PICKED.add(picked)  # 用户自己在系统对话框里选的文件夹，还没出过片也可以打开看
                return self.json({"路径": picked})
            m = re.fullmatch(r"/api/snippets(?:/([0-9a-f]+))?(/delete)?", u.path)
            if m:
                if m.group(2):
                    delete_snippet(m.group(1))
                    return self.json({"ok": True})
                return self.json(save_snippet(m.group(1), self.body()))
            if u.path == "/api/key":
                return self.json(rh.set_key(self.body().get("key")))
            if u.path == "/api/key/delete":
                rh.delete_key()
                return self.json({"ok": True})
            if u.path == "/api/workflows":
                return self.json(rh.lib_add(self.body()))
            m = re.fullmatch(r"/api/workflows/([0-9a-f]+)(/delete)?", u.path)
            if m:
                if m.group(2):
                    rh.lib_delete(m.group(1))
                    return self.json({"ok": True})
                return self.json(rh.lib_update(m.group(1), self.body()))
            self.json({"错误": "没有这个地址"}, 404)
        except Exception as e:
            self.json({"错误": str(e)}, 400)


def main():
    rh.bind(sys.modules[__name__])
    ASSETS.mkdir(parents=True, exist_ok=True)
    try:
        where = rh.migrate_keyfile()
        if where:
            print(f"已把 ~/.runninghub_key 里的 API Key 抄进{where}")
    except Exception as e:
        print(f"API Key 没能存进加密存储：{e}")
    for t in load_json(TASKS_FILE, []):
        if t["状态"] == "uploading":  # 上次关窗口时还没提交出去的，算失败，不自动重交（避免重复扣费）
            t["状态"], t["错误"] = "failed", "上次关闭时还在上传，没有提交"
        if "收藏文件" not in t:
            t["收藏文件"] = fav_files(t)
        if t.get("平台") == "runninghub" and t["状态"] in ("queued", "in_progress") and not t.get("中途取件"):
            # 实时连接地址只在内存里，重启后接不回去：一采只能等整条跑完再下，卡片上说清楚
            t["实时连接"] = "窗口重启后没有"
            t["提示"] = "窗口重启过，实时连接接不回来：一采要等整条跑完才下"
        tasks[t["id"]] = t
    by_wid = {e["workflowId"]: e for e in rh.load_library()}
    for t in tasks.values():  # 老任务的 endpoint 没带库里的编号：按当时的 workflowId 对上，「换种子再来」「用这组设置」才认得
        e = by_wid.get(str(t.get("workflowId") or ""))
        if e and str(t.get("endpoint", "")).startswith("rh/") and "@" not in t["endpoint"]:
            t["endpoint"] = f"rh/{e['模板']}@{e['id']}"
    save_tasks()
    for f, empty in ((PROJECTS_FILE, []), (SETTINGS_FILE, {})):  # 存一遍，把里面的路径换成记号
        if f.exists():
            save_json(f, load_json(f, empty))
    threading.Thread(target=poller, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    import socket
    probe_sock = socket.socket()
    probe_sock.settimeout(1)
    running = probe_sock.connect_ex(("127.0.0.1", PORT)) == 0  # Windows 上重复绑定同一个端口不报错，所以先连一下试试
    probe_sock.close()
    try:
        srv = None if running else ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        srv = None
    if srv is None:  # 已经开着一个了：直接打开网页
        print(f"工作台已经在运行：{url}")
        webbrowser.open(url)
        return
    print(f"MiniMax H3 工作台 v{VERSION}：{url}{'（演示模式，不扣费）' if MOCK else ''}  关掉这个终端窗口就停止")
    if "--不开浏览器" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    print("工作台已退出。这个窗口可以关掉了。")


if __name__ == "__main__":
    main()
