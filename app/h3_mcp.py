#!/usr/bin/env python3
"""MiniMax H3 工作台的 MCP 服务：让 Claude 桌面版、ChatGPT 桌面版（Codex）这类本机 AI 助手直接用工作台——
看工作流和参数、添加素材、算分段时间、检查一条生成、把设置放进网页的生成框、提交、查进度、取消。

两种接法，工具完全一样：
  - stdio（推荐）：桌面软件按配置启动这个文件，经标准输入输出说话。工作台网页上的「MCP」按钮能一键写入
    Claude 桌面版的配置、复制给别的软件的配置。
  - 网址（Streamable HTTP）：工作台自己在 http://127.0.0.1:端口/mcp 上提供，要在「MCP」里打开，带随机口令。
    hf.py 调这里的 handle() 处理，不另起程序。

它不自己连 RunningHub，只调本机正在运行的工作台（http://127.0.0.1:8790，端口跟 H3_PORT）：
API Key、上传、排队、下载、记录都还是工作台在做。工作台没开就报「先双击 启动」，不会自己在后台启动它。

扣费的只有 h3_submit。默认（工作台「MCP」里「自动提交」关着）：必须先 h3_check 拿到确认码，确认码对应的那一组设置
一个字都不能变；或者用 h3_put_in_composer 把设置放进网页生成框，用户自己点「生成」。
用户在工作台网页上打开「自动提交」后，h3_submit 也可以直接带设置提交（交之前照样检查一遍）。这个开关 MCP 改不了。
不管开关开没开，助手都要先把条数、预计时间告诉用户、等用户明确同意再交。

协议：同时认老式（initialize 握手，2024-11-05 … 2025-11-25）和新式（每个请求自带 _meta，2026-07-28）。
工具参数名用英文（Anthropic 接口要求参数名只能是英文字母、数字和 _.-，中文参数名的工具会被整个丢掉）；老的中文参数名照样认。
只用 Python 自带库。
"""
import hashlib
import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

VERSION = "1.4"
PORT = int(os.environ.get("H3_PORT") or 8790)
MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_V, META_CLIENT = "io.modelcontextprotocol/protocolVersion", "io.modelcontextprotocol/clientInfo"
CHECKED = {}  # 确认码 → (请求体, 时间)
CHECK_TTL = 1800
_lock = threading.Lock()
_ctx = threading.local()  # 这一次调用来自哪个客户端、哪种接法（告诉工作台，网页「MCP」里显示最近连接）
EXT_KIND = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image", ".heic": "image",
            ".tif": "image", ".tiff": "image", ".mp4": "video", ".mov": "video", ".m4v": "video", ".webm": "video",
            ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".aac": "audio", ".flac": "audio"}
INSTRUCTIONS = ("MiniMax H3 工作台（RunningHub）。写分段提示词前用 h3_segments 算时间；交之前用 h3_check。"
                "h3_submit 会扣费：只在用户明确同意这一次（条数、预计时间）后调用；不确定就用 h3_put_in_composer 让用户自己点生成。"
                "一个参考素材都不放 = 文生视频（只用 7 段 v3 工作流时锁定声音也能不放）。")


class Fail(Exception):
    pass


# ---------- 连工作台 ----------

def call(method, path, body=None, raw=None, headers=None, timeout=120):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        h = dict(headers or {})
        h["X-H3-MCP"] = quote(f"{getattr(_ctx, 'transport', 'stdio')}|{getattr(_ctx, 'client', '') or ''}"[:120])
        data = raw
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        txt = r.read()
    except (ConnectionRefusedError, OSError) as e:
        if isinstance(e, ConnectionRefusedError) or "refused" in str(e).lower() or getattr(e, "errno", None) in (61, 111, 10061):
            raise Fail("工作台没开：先双击「启动.command」（Windows 是「启动.bat」）打开工作台，再试一次")
        raise Fail(f"连不上工作台：{e}")
    finally:
        c.close()
    try:
        j = json.loads(txt or b"null")
    except ValueError:
        raise Fail(f"工作台回了看不懂的内容（{r.status}）")
    if r.status == 404 and path.startswith(("/api/check", "/api/handoff")):
        raise Fail("工作台是旧版本（v1.2 以前），没有这个功能：关掉工作台的终端窗口，再双击「启动.command」重开")
    if r.status >= 400 or (isinstance(j, dict) and j.get("错误")):
        raise Fail((j or {}).get("错误") or f"工作台出错（{r.status}）")
    return j


get = lambda p: call("GET", p)
post = lambda p, b: call("POST", p, b)


def ways():
    return get("/api/models")["models"][0]["方式"]


def pick_way(name):
    ws = ways()
    if not name:
        ok = [w for w in ws if w["workflowId"] and not w["问题"]]
        if len(ok) == 1:
            return ok[0]
        raise Fail("要指定工作流（h3_workflows 列出的编号或名称）。现在有：" + "；".join(f"{w['endpoint']}｜{w['名称']}" for w in ws))
    hit = [w for w in ws if name in (w["endpoint"], w["库id"], w["名称"])] or [w for w in ws if name in w["名称"] or name in w["endpoint"]]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise Fail(f"没有叫「{name}」的工作流。现在有：" + "；".join(f"{w['endpoint']}｜{w['名称']}" for w in ws))
    raise Fail(f"「{name}」对上了好几个工作流，写完整编号：" + "；".join(w["endpoint"] for w in hit))


def pick_project(name):
    if not name:
        return None
    ps = get("/api/projects")
    hit = [p for p in ps if name == p["id"]] or [p for p in ps if p["名称"] == name]
    if not hit and "/" in name:  # 《信》/第3场/15镜 这种层级写法
        parts = [x for x in name.split("/") if x]
        cand = [p for p in ps if p["名称"] == parts[-1]]
        byid = {p["id"]: p for p in ps}

        def chain(p):
            out = []
            while p:
                out.append(p["名称"])
                p = byid.get(p.get("上级"))
            return out[::-1]
        hit = [p for p in cand if chain(p)[-len(parts):] == parts]
    if len(hit) == 1:
        return hit[0]["id"]
    if not hit:
        raise Fail(f"没有叫「{name}」的项目（h3_projects 看全部；要新建请在工作台里建）")
    raise Fail(f"有 {len(hit)} 个项目都叫「{name}」，用编号：" + "、".join(p["id"] for p in hit))


def schema_of(w):
    return w["参数表"]["schema"]


def media_fields(w):
    props = schema_of(w)["properties"]
    req = schema_of(w).get("required", [])
    out = []
    for k, v in props.items():
        multi = v.get("type") == "array" and (v.get("items") or {}).get("format") == "uri"
        single = v.get("type") == "string" and v.get("format") == "uri"
        if multi or single:
            out.append({"字段": k, "名字": w["字段名"].get(k) or {"image_urls": "图像", "video_urls": "参考视频", "audio_urls": "参考音频"}.get(k, k),
                        "类型": w["格子类型"].get(k), "最多": v.get("maxItems", 1) if multi else 1, "必填": k in req,
                        "不放时": (w.get("不放时") or {}).get(k)})
    return out


def add_asset(path):
    p = Path(os.path.expanduser(str(path)))
    if not p.is_file():
        raise Fail(f"找不到文件：{p}")
    if p.suffix.lower() not in EXT_KIND:
        raise Fail(f"不支持的文件类型：{p.suffix or p.name}（图片 png/jpg/webp/heic/tiff，视频 mp4/mov，音频 wav/mp3/m4a）")
    size = p.stat().st_size
    with open(p, "rb") as f:
        return call("POST", "/api/assets", raw=f, timeout=1800,
                    headers={"X-Name": quote(p.name), "Content-Length": str(size), "Content-Type": "application/octet-stream"})


KIND_CN = {"image": "图片", "video": "视频", "audio": "音频"}


def asset_ids(items, kind, label):
    if isinstance(items, str):
        items = [items]
    out = []
    for x in items or []:
        x = str(x).strip()
        if not x:
            continue
        if len(x) == 16 and all(c in "0123456789abcdef" for c in x) and not os.path.exists(x):
            a = get("/api/assets/" + x)
            if not a.get("id"):
                raise Fail(f"{label}：工作台里没有素材 {x}")
        else:
            a = add_asset(x)
        if kind and a["类型"] != kind:
            raise Fail(f"{label} 要放{KIND_CN[kind]}，{a['名字']} 是{KIND_CN.get(a['类型'], a['类型'])}")
        out.append(a["id"])
    return out


# 老版本（v1.2）的中文参数名 → 现在的英文参数名。老的写法照样认
ALIAS = {"工作流": "workflow", "提示词": "prompt", "总时长": "duration", "参数": "params", "图像": "images",
         "参考视频": "videos", "参考音频": "audios", "锁定声音": "lock_audio", "固定格": "fixed_slots", "项目": "project",
         "数量": "count", "前缀": "prefix", "输出目录": "out_dir", "说明": "note", "确认码": "code", "任务": "task",
         "状态": "status", "条数": "limit", "路径": "path", "分段时长": "segment_length", "种子": "seed"}


def norm(a):
    a = dict(a or {})
    for cn, en in ALIAS.items():
        if cn in a and en not in a:
            a[en] = a.pop(cn)
    return a


def build(a):
    """工具参数 → 工作台 /api/submit 的请求体（和网页点「确认提交」交的一样）。"""
    w = pick_way(a.get("workflow"))
    props = schema_of(w)["properties"]
    fields = {f["字段"]: f for f in media_fields(w)}
    given = dict(a.get("params") or {})
    if not isinstance(given, dict):
        raise Fail("params 要是一个对象，如 {\"分段时长\": \"自动\"}")
    for k, pk in (("segment_length", "分段时长"), ("seed", "种子")):
        if a.get(k) not in (None, "") and pk in props:
            given[pk] = a[k]
    params = {}
    for k, v in props.items():
        if k == "prompt" or k in fields:
            continue
        if k in given:
            val = given.pop(k)
        elif v.get("title") in given:
            val = given.pop(v["title"])
        elif v.get("default") is not None:
            val = v["default"]
        else:
            continue
        if k == "duration" and a.get("duration") is not None:
            val = a["duration"]
        if "enum" in v and val not in v["enum"]:
            raise Fail(f"{v.get('title') or k} 只能选：{'、'.join(map(str, v['enum']))}（给的是 {val}）")
        if v.get("type") in ("integer", "number"):
            try:
                val = int(val) if v["type"] == "integer" else float(val)
            except (TypeError, ValueError):
                raise Fail(f"{v.get('title') or k} 要填数字（给的是 {val}）")
            if "minimum" in v and val < v["minimum"] or "maximum" in v and val > v["maximum"]:
                raise Fail(f"{v.get('title') or k} 要在 {v.get('minimum')}–{v.get('maximum')} 之间（给的是 {val}）")
        if v.get("type") == "string" and "enum" not in v:
            val = "" if val is None else str(val)
        params[k] = val
    if "duration" in props and "duration" not in params and a.get("duration") is not None:
        params["duration"] = a["duration"]
    if given:
        names = [f"{k}（{v.get('title')}）" if v.get("title") else k for k, v in props.items() if k != "prompt" and k not in fields]
        raise Fail(f"这些参数这个工作流没有：{'、'.join(given)}。能改的有：{'、'.join(names)}")
    prompt = str(a.get("prompt") or "").strip()
    if prompt:
        params["prompt"] = prompt
    media = {}
    for key, field, label in (("images", "image_urls", "图像"), ("videos", "video_urls", "参考视频"), ("audios", "audio_urls", "参考音频")):
        if a.get(key):
            if field not in fields:
                raise Fail(f"这个工作流没有{label}格子")
            media[field] = asset_ids(a[key], fields[field]["类型"], label)
    fixed = {f["名字"]: k for k, f in fields.items() if k.startswith("rh_")}
    extra = dict(a.get("fixed_slots") or {})
    if a.get("lock_audio"):
        extra["锁定声音"] = a["lock_audio"]
    for label, x in extra.items():
        k = fixed.get(label) or next((v for n, v in fixed.items() if label in n), None)
        if not k:
            raise Fail(f"这个工作流没有「{label}」格子" + (f"（有：{'、'.join(fixed)}）" if fixed else ""))
        media[k] = asset_ids([x], fields[k]["类型"], label)
    pid = pick_project(a.get("project"))
    try:
        count = int(a.get("count") or 1)
    except (TypeError, ValueError):
        raise Fail("count 是 1–10 的整数")
    if not 1 <= count <= 10:
        raise Fail("数量是 1–10 条")
    body = {"endpoint": w["endpoint"], "参数": params, "素材": media, "数量": count, "前缀": str(a.get("prefix") or ""),
            "项目": pid, "估价": None}
    if a.get("out_dir"):
        body["输出目录"] = str(a["out_dir"])
    body["表单"] = {"endpoint": w["endpoint"], "prompt": prompt, "paramsBy": {w["endpoint"]: {k: v for k, v in params.items() if k != "prompt"}},
                  "assign": media, "prefix": body["前缀"], "count": count}
    return w, body


def code_of(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]


def check_report(w, body, r):
    lines = [f"工作流：{w['名称']}（{w['endpoint']}）" + ("　【演示模式，不扣费】" if r.get("演示") else "")]
    p = body["参数"]
    shown = [f"{k}={v}" for k, v in p.items() if k != "prompt"]
    lines.append("参数：" + "，".join(shown))
    lines.append(f"数量：{body['数量']} 条；机器：{r.get('机器')}；段数：{r.get('段数')}")
    for s in r.get("分段") or []:
        lines.append(f"  第{s['段']}段 {s['起点秒']}–{s['终点秒']} 秒" + (f"（开头到 {s['新内容起点秒']} 秒是上一段末尾）" if s["段"] > 1 else ""))
    lines.append("格子：" + ("；".join(f"{k} {v}" for k, v in (r.get("用到的格子") or {}).items()) or "（一个素材都没放：文生视频）"))
    for n in r.get("提醒") or []:
        lines.append("提醒：" + n)
    if r.get("问题"):
        lines.append("问题（不改交不了）：")
        lines += [f"  - {x}" for x in r["问题"]]
    return "\n".join(lines)


def auto_on():
    return bool(get("/api/state").get("MCP自动提交"))


# ---------- 工具 ----------

GEN_PROPS = {
    "workflow": {"type": "string", "description": "工作流：h3_workflows 列出的编号（rh/…@…）或名称；只有一个可用工作流时可以不填"},
    "prompt": {"type": "string", "description": "提示词。分段工作流超过 15 秒要写多段：段与段之间单独一行「=== 第2段 ===」「=== 第3段 ===」…；各段时间用 h3_segments 算"},
    "duration": {"type": "number", "description": "成片总秒数"},
    "segment_length": {"type": "string", "description": "分段时长（秒）；不填 = 自动（每段不超过 15 秒、均分）"},
    "seed": {"type": "string", "description": "种子；不填 = 每条随机"},
    "params": {"type": "object", "description": "其他参数，键用 h3_workflows 里的键或名字（如 一采清晰度、二采清晰度、声音锁定强度、机器）；不填用默认"},
    "images": {"type": "array", "items": {"type": "string"}, "description": "参考图，按顺序是 <Picture 1>、<Picture 2>…；每项是本机文件路径或 h3_add_asset 给的素材编号。不放 = 不用参考图"},
    "videos": {"type": "array", "items": {"type": "string"}, "description": "参考视频，按顺序是 <Video 1>…；本机路径或素材编号"},
    "audios": {"type": "array", "items": {"type": "string"}, "description": "参考音频，按顺序是 <Audio 1>…（不是锁定声音）"},
    "lock_audio": {"type": "string", "description": "锁定声音：一采锁定用的源音频（本机路径或素材编号）。7 段 v3 工作流可以不放（模型自己出声）"},
    "project": {"type": "string", "description": "项目编号或名字（可写层级：《信》/第3场/15镜）；不填 = 首页"},
    "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "同一套设置出几条，默认 1"},
    "prefix": {"type": "string", "description": "出片文件名前缀，如 S3-15"},
    "out_dir": {"type": "string", "description": "出片文件夹；不填 = 项目的出片文件夹"},
}
RO = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = [
    {"name": "h3_status", "title": "工作台状态", "description": "工作台开没开、版本、是不是演示模式、API Key、RunningHub 余额、正在跑几条、自动提交开没开。",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": RO},
    {"name": "h3_workflows", "title": "工作流和参数", "description": "列出工作台里的工作流：编号、结构、能改的参数（键、名字、取值、默认值）、素材格子（几个、必不必填、不放会怎样）。",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": RO},
    {"name": "h3_projects", "title": "项目", "description": "列出工作台里的项目（编号、名字、上级、出片文件夹）。",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": RO},
    {"name": "h3_add_asset", "title": "添加素材", "description": "把本机的图片 / 视频 / 音频加进工作台素材库（需要时自动转成能上传的格式），返回素材编号。不上传到平台，不扣费。",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "本机文件的完整路径"}}, "required": ["path"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "h3_segments", "title": "算分段时间", "description": "分段工作流：给总时长（和分段时长），算出要几段、每段在成片里从几秒到几秒。写分段提示词前先用它。",
     "inputSchema": {"type": "object", "properties": {"workflow": GEN_PROPS["workflow"], "duration": GEN_PROPS["duration"],
                                                      "segment_length": GEN_PROPS["segment_length"]},
                     "required": ["duration"]}, "annotations": RO},
    {"name": "h3_check", "title": "检查一条生成", "description": "检查一条生成（不扣费、不提交）：和网页点「确认提交」同一套检查，把问题一次列全，"
                                                            "并说明会分几段、每段时间、用到哪些素材。没问题时给一个确认码，h3_submit 要用。",
     "inputSchema": {"type": "object", "properties": GEN_PROPS, "required": ["prompt"]}, "annotations": RO},
    {"name": "h3_put_in_composer", "title": "放进生成框", "description": "把一组设置放进工作台网页的生成框（推荐）：网页会弹框问用户要不要换上，"
                                                                    "换上后用户自己核对、自己点「生成」。不扣费。会先检查一遍，有问题也照样放进去，并告诉你问题。",
     "inputSchema": {"type": "object", "properties": dict(GEN_PROPS, note={"type": "string", "description": "给用户看的一句话，如「S3-15 v4：只改了第2段走位」"}),
                     "required": ["prompt"]}, "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "h3_submit", "title": "提交（会扣费）",
     "description": "【会扣费：按 RunningHub 机器运行时长扣 RH 币】提交到 RunningHub。只能在用户明确同意了这一次（条数、预计时间）之后调用。"
                    "两种用法：① 给 code = h3_check 的确认码，那组设置原样提交（要改就重新 h3_check）；"
                    "② 不给 code、直接给 prompt 等设置——只有用户在工作台网页「MCP」里打开了「自动提交」才行，交之前照样检查一遍。"
                    "自动提交开关只能由用户在网页上改。",
     "inputSchema": {"type": "object", "properties": dict({"code": {"type": "string", "description": "h3_check 给的确认码"}}, **GEN_PROPS)},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}},
    {"name": "h3_tasks", "title": "最近的任务", "description": "最近的生成任务：状态、进度提示、出片文件。可以按项目、状态筛。",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目编号或名字"},
                                                      "status": {"type": "string", "description": "uploading / queued / in_progress / completed / failed / canceled"},
                                                      "limit": {"type": "integer", "description": "最多几条，默认 10"}}}, "annotations": RO},
    {"name": "h3_task", "title": "任务详情", "description": "一条任务的详情：参数、提示词、用到的素材、种子、平台任务号、错误、每一采的出片文件路径。",
     "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "任务编号（h3_tasks 列出的）"}}, "required": ["task"]},
     "annotations": RO},
    {"name": "h3_cancel", "title": "取消任务", "description": "取消一条任务。平台上的任务会没掉，还没下载的那一采拿不到，已经跑掉的机器时长照扣。要用户同意再取消。",
     "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "任务编号"}}, "required": ["task"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
]

STATUS = {"uploading": "上传素材中", "queued": "排队中", "in_progress": "生成中", "completed": "完成", "failed": "失败",
          "nsfw": "被内容审核拦下", "canceled": "已取消"}


def t_status(a):
    st = get("/api/state")
    acct = None
    try:
        acct = get("/api/account")
    except Fail:
        pass
    ts = get("/api/tasks")
    run = [t for t in ts if t["状态"] in ("uploading", "queued", "in_progress")]
    return (f"工作台 v{st.get('版本')} 在运行（http://127.0.0.1:{PORT}/）" + ("，演示模式（不扣费）" if st.get("演示") else "") +
            f"\nAPI Key：{'已保存' if st.get('有RH密钥') else '没有（在工作台左下角填写）'}" +
            (f"\n余额：{acct.get('余额')}" if isinstance(acct, dict) and acct.get("余额") is not None else "") +
            f"\n正在跑 / 排队：{len(run)} 条" +
            f"\n自动提交：{'开着（h3_submit 可以不要确认码直接交；仍要用户先同意）' if st.get('MCP自动提交') else '关着（h3_submit 要 h3_check 的确认码；或用 h3_put_in_composer）'}")


def t_workflows(a):
    out = []
    for w in ways():
        props = schema_of(w)["properties"]
        fields = media_fields(w)
        keys = {f["字段"] for f in fields}
        ps = []
        for k, v in props.items():
            if k == "prompt" or k in keys:
                continue
            d = {"键": k, "名字": v.get("title") or k, "默认": v.get("default")}
            for x, y in (("enum", "取值"), ("minimum", "最小"), ("maximum", "最大"), ("description", "说明")):
                if x in v:
                    d[y] = v[x]
            ps.append(d)
        out.append({"编号": w["endpoint"], "名称": w["名称"], "结构": w["结构"], "workflowId": w["workflowId"],
                    "能用": not w["问题"], "问题": w["问题"], "备注": w.get("备注"), "参数": ps,
                    "格子": [{k: v for k, v in f.items() if v not in (None, False)} for f in fields],
                    "文生视频": "可以（一个参考都不放）" if not any(f["必填"] for f in fields) and not w.get("缺了不能跑") else
                            "不行：" + "；".join([f"要放「{f['名字']}」" for f in fields if f["必填"]] + list((w.get("缺了不能跑") or {}).values())),
                    "出片": w.get("输出名")})
    return json.dumps(out, ensure_ascii=False)


def t_projects(a):
    ps = get("/api/projects")
    byid = {p["id"]: p for p in ps}

    def path(p):
        out = []
        while p:
            out.append(p["名称"])
            p = byid.get(p.get("上级"))
        return "/".join(out[::-1])
    return "\n".join(f"{p['id']}｜{path(p)}｜出片到 {p.get('输出目录')}" for p in ps) or "还没有项目"


def t_add_asset(a):
    if not a.get("path"):
        raise Fail("要给 path（本机文件路径）")
    m = add_asset(a["path"])
    extra = "，".join(x for x in [f"{m['秒']:.2f} 秒" if m.get("秒") else "", f"{m.get('宽')}×{m.get('高')}" if m.get("宽") else "",
                                  "已转成能上传的格式" if m.get("转换") else ""] if x)
    return f"素材编号 {m['id']}（{KIND_CN.get(m['类型'], m['类型'])}：{m['名字']}{'，' + extra if extra else ''}）"


def t_segments(a):
    if a.get("duration") is None:
        raise Fail("要给 duration（成片总秒数）")
    w = pick_way(a.get("workflow"))
    p = {"duration": a["duration"], "prompt": "（算分段用）", "workflow": w["endpoint"], "segment_length": a.get("segment_length")}
    w, body = build(p)
    r = post("/api/check", body)
    if not r.get("分段"):
        return f"{w['名称']} 不分段（单段工作流），总时长 {a['duration']} 秒"
    probs = [x for x in r["问题"] if "提示词要写" not in x and "要写提示词" not in x and "Key" not in x and "Surfshark" not in x]
    lines = [f"总时长 {a['duration']} 秒 → {r['段数']} 段："]
    for s in r["分段"]:
        lines.append(f"第{s['段']}段 成片 {s['起点秒']}–{s['终点秒']} 秒" +
                     (f"；这段提示词的时间从 0 算（= 成片时间 − {s['起点秒']}），开头 0–{round(s['新内容起点秒'] - s['起点秒'], 2)} 秒是上一段末尾，别在那里起落动作" if s["段"] > 1 else ""))
    if r["段数"] > 1:
        lines.append("提示词写法：第 1 段写在最前面，后面每段前单独一行「=== 第2段 ===」…")
    return "\n".join(lines + [f"问题：{x}" for x in probs])


def _remember(body):
    code = code_of(body)
    now = time.time()
    with _lock:
        for k in [k for k, (_, t) in CHECKED.items() if now - t > CHECK_TTL]:
            CHECKED.pop(k)
        CHECKED[code] = (body, now)
    return code


def t_check(a):
    w, body = build(a)
    r = post("/api/check", body)
    rep = check_report(w, body, r)
    if r.get("问题"):
        return rep, True
    code = _remember(body)
    tail = "演示模式，不扣费" if r.get("演示") else f"会扣费：按机器运行时长扣 RH 币，{body['数量']} 条"
    return rep + f"\n\n检查通过。确认码 {code}（30 分钟内有效）。{tail}。先把条数和预计时间告诉用户，用户同意后再用 h3_submit 提交；或者用 h3_put_in_composer 让用户自己点。", False


def t_put(a):
    w, body = build(a)
    r = post("/api/check", body)
    post("/api/handoff", {"表单": body["表单"], "项目": body["项目"], "说明": a.get("note") or ""})
    rep = check_report(w, body, r)
    return rep + "\n\n已放进工作台：网页会弹框问要不要换上生成框（工作台网页要开着）。用户核对后自己点「生成」。" + \
        ("\n注意上面的问题：不改的话网页上点不了生成。" if r.get("问题") else ""), False


def _send(body):
    r = post("/api/check", body)  # 交之前再查一次（素材、Key、VPN 这期间可能变了）
    if r.get("问题"):
        raise Fail("现在交不了：" + "；".join(r["问题"]))
    ids = post("/api/submit", body)["任务"]
    return ids, r


def t_submit(a):
    code = str(a.get("code") or "").strip()
    if code:
        with _lock:
            hit = CHECKED.get(code)
        if not hit or time.time() - hit[1] > CHECK_TTL:
            raise Fail("确认码不对或过期了：先用 h3_check 检查，拿到新的确认码")
        ids, r = _send(hit[0])
        with _lock:
            CHECKED.pop(code, None)
        how = "按确认码"
    else:
        if not a.get("prompt"):
            raise Fail("要给 code（h3_check 的确认码）；或者在「自动提交」开着时直接给 prompt 等设置")
        if not auto_on():
            raise Fail("工作台的「自动提交」关着：先用 h3_check 拿确认码再 h3_submit(code=…)，或者用 h3_put_in_composer 让用户自己点「生成」。"
                       "要不要打开自动提交由用户在工作台网页的「MCP」里决定，这里改不了")
        w, body = build(a)
        ids, r = _send(body)
        how = "自动提交"
    return f"已提交（{how}）{len(ids)} 条：{'、'.join(ids)}。用 h3_task 看进度；工作台网页上也能看到。" + ("（演示模式，不扣费）" if r.get("演示") else ""), False


def _brief(t):
    out = [f"{t['id']}｜{STATUS.get(t['状态'], t['状态'])}｜{t.get('前缀') or '未命名'}｜{t.get('参数', {}).get('duration', '?')} 秒｜{t.get('创建')}"]
    if t.get("提示") and t["状态"] not in ("completed", "failed", "canceled"):
        out.append("  " + t["提示"])
    if t.get("错误"):
        out.append("  错误：" + str(t["错误"])[:300])
    for x in t.get("产出") or []:
        if x.get("视频"):
            out.append(f"  {x.get('名称')}：{x['视频']}")
    return "\n".join(out)


def t_tasks(a):
    ts = [t for t in get("/api/tasks") if not t.get("演示")] or get("/api/tasks")
    if a.get("project"):
        pid = pick_project(a["project"])
        ts = [t for t in ts if t.get("项目") == pid]
    if a.get("status"):
        ts = [t for t in ts if t["状态"] == a["status"]]
    try:
        n = int(a.get("limit") or 10)
    except (TypeError, ValueError):
        n = 10
    return "\n".join(_brief(t) for t in ts[:max(1, n)]) or "没有任务"


def _task(tid):
    if not tid:
        raise Fail("要给 task（任务编号）")
    t = next((x for x in get("/api/tasks") if x["id"] == tid), None)
    if not t:
        raise Fail(f"没有任务 {tid}")
    return t


def t_task(a):
    t = _task(a.get("task"))
    keep = ("id", "状态", "提示", "错误", "方式", "endpoint", "参数", "用到的格子", "种子", "request_id", "workflowId",
            "创建", "完成", "平台耗时", "输出目录", "产出", "平台检查", "项目", "数量", "序号")
    d = {k: t[k] for k in keep if t.get(k) not in (None, "", [], {})}
    d["状态"] = STATUS.get(t["状态"], t["状态"])
    if d.get("用到的格子"):
        d["用到的格子"] = {k: v for k, v in d["用到的格子"].items() if v}
    return json.dumps(d, ensure_ascii=False, indent=1)


def t_cancel(a):
    t = _task(a.get("task"))
    if t["状态"] in ("completed", "failed", "canceled", "nsfw"):
        return f"这条已经是「{STATUS.get(t['状态'], t['状态'])}」，不用取消"
    post(f"/api/tasks/{quote(t['id'])}/cancel", {})
    return f"已取消 {t['id']}"


HANDLERS = {"h3_status": t_status, "h3_workflows": t_workflows, "h3_projects": t_projects, "h3_add_asset": t_add_asset,
            "h3_segments": t_segments, "h3_check": t_check, "h3_put_in_composer": t_put, "h3_submit": t_submit,
            "h3_tasks": t_tasks, "h3_task": t_task, "h3_cancel": t_cancel}


# ---------- MCP（JSON-RPC 2.0）：stdio 和网址接口共用 ----------

ERR_VERSION, ERR_METHOD, ERR_PARAMS = -32022, -32601, -32602
_LAST_CLIENT = {}


def _err(i, code, msg, data=None):
    e = {"code": code, "message": msg}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": i, "error": e}


def handle(m, transport="stdio"):
    """处理一条 JSON-RPC 消息，返回要回的那一条（通知返回 None）。
    带 _meta 协议版本的是新式（2026-07-28：不握手、每个请求单独处理）；否则按老式（initialize 握手）处理。"""
    if not isinstance(m, dict) or m.get("jsonrpc") != "2.0" or not isinstance(m.get("method"), str):
        return _err(m.get("id") if isinstance(m, dict) else None, -32600, "不是 JSON-RPC 2.0 请求")
    meth, i = m["method"], m.get("id")
    p = m.get("params") if isinstance(m.get("params"), dict) else {}
    meta = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
    ver = meta.get(META_V)
    modern = ver is not None
    if i is None:  # 通知（initialized、cancelled…）：不回
        return None
    if modern and ver not in MODERN:
        return _err(i, ERR_VERSION, "Unsupported protocol version", {"supported": list(MODERN) + list(LEGACY), "requested": ver})
    _ctx.transport = transport
    ci = meta.get(META_CLIENT) if modern else (p.get("clientInfo") if meth == "initialize" else None)
    if isinstance(ci, dict) and ci.get("name"):  # 老式只在 initialize 里说自己是谁：记下来，后面的请求沿用（网址接口按最近一次握手算）
        _ctx.client = _LAST_CLIENT[transport] = str(ci.get("name"))[:60]
    else:
        _ctx.client = _LAST_CLIENT.get(transport, "")

    def ok(result):
        if modern:
            result = dict(result, resultType="complete")
        return {"jsonrpc": "2.0", "id": i, "result": result}

    if meth == "initialize":
        want = p.get("protocolVersion")
        return ok({"protocolVersion": want if want in LEGACY else LEGACY[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "minimax-h3-workbench", "title": "MiniMax H3 工作台", "version": VERSION},
                   "instructions": INSTRUCTIONS})
    if meth == "server/discover":
        return ok({"supportedVersions": list(MODERN) + list(LEGACY), "capabilities": {"tools": {"listChanged": False}},
                   "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "minimax-h3-workbench", "version": VERSION}},
                   "instructions": INSTRUCTIONS})
    if meth == "tools/list":
        return ok({"tools": TOOLS})
    if meth == "tools/call":
        fn = HANDLERS.get(p.get("name"))
        if not fn:
            return _err(i, ERR_PARAMS, f"没有工具 {p.get('name')}")
        args = p.get("arguments")
        try:
            r = fn(norm(args if isinstance(args, dict) else {}))
            text, bad = r if isinstance(r, tuple) else (r, False)
        except Fail as e:
            text, bad = str(e), True
        except Exception as e:  # 不让一个工具的意外把整个服务带崩
            text, bad = f"出错了：{type(e).__name__}: {e}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": bad})
    if meth == "ping":
        return ok({})
    return _err(i, ERR_METHOD, f"不支持 {meth}")


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    def out(msg):
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            out(_err(None, -32700, "不是 JSON"))
            continue
        for x in m if isinstance(m, list) else [m]:
            r = handle(x)
            if r is not None:
                out(r)


if __name__ == "__main__":
    main()
