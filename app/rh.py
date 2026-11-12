"""生成窗口的 RunningHub 部分：把登记过的 ComfyUI 工作流当成一个「模型」，素材拖进格子，按「工作流 ID + 改值」提交。

接口写法照 01-项目/Claude-AI独唱/tools/rh_run.py（2026-10 实测过）：
  上传 /task/openapi/upload（multipart：apiKey、file、fileType）→ data.fileName
  提交 /task/openapi/create {apiKey, workflowId, instanceType, nodeInfoList} → data.taskId、netWssUrl
  状态 /task/openapi/status → data = QUEUED / RUNNING / SUCCESS / FAILED
  结果 /task/openapi/outputs → data = [{fileUrl, nodeId, taskCostTime}]；运行中返回 804，不给已完成那一采的文件
  实时进度 netWssUrl：每一采存盘时的 executed 消息里带 cos_url（2026-11-09 实测），一采跑完就按它下载，不用等二采
  取消 /task/openapi/cancel（排队、运行中都能取消；取消后任务就查不到了）
API Key 加密保存（Mac 钥匙串 / Windows DPAPI），只发给 www.runninghub.ai，不打印、不写进记录。
默认要求 Mac 上 Surfshark 已连上才提交（和 rh_run 一样，API 请求不经过浏览器插件）。

空格子：工作流加开关.py 给每个参考格子加了 ComfySwitchNode。没用的格子传占位文件（开跑前的检查要求文件存在）并把开关关掉，
H3 节点收到空就跳过（2026-11-09 Peter 实测：图像3 换成别的图、开关关着，出片没影响）。
"""
import json
import random
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

H = None  # hf 模块（bind 时传进来；hf.py 是直接运行的，不能 import hf，否则会有两份任务表）
HOST = "https://www.runninghub.ai"
KEYFILE = Path.home() / ".runninghub_key"
REG_DIR = Path(__file__).resolve().parent / "RunningHub工作流"
TAG = "<{Kind} {n}>"
KIND_TAG = {"image": "Picture", "video": "Video", "audio": "Audio"}
UPLOAD_REUSE_SECONDS = 24 * 3600
QUEUE_RETRY_SECONDS = 30
MAX_RUNNING = 3  # 会员 Key 同时最多 3 个任务（_缓存/设置.json 的「RunningHub同时最多」可改）；多出来的在本机排队
_slot_lock = threading.Lock()


def bind(mod):
    global H
    H = mod


# ---------- 接口 ----------

# ---------- API Key：加密保存 ----------
# Mac 存系统钥匙串；Windows 用系统自带的 DPAPI 加密成文件（只有本机这个账户能解开）。不打印、不写进记录、不发给网页。

KEY_SERVICE, KEY_ACCOUNT = "minimax-h3-workbench", "runninghub-api-key"
_key_mem = {"读过": False, "值": None}


def _svc():
    return KEY_SERVICE + ("-demo" if H.MOCK else "")  # 演示模式用另一格，不碰真 Key


def _key_file():
    return H.CACHE / ("api_key_demo.bin" if H.MOCK else "api_key.bin")


def key_place():
    return {"darwin": "Mac 钥匙串", "win32": "Windows 加密文件（DPAPI，只有本机这个账户能解开）"}.get(sys.platform, "本机文件（仅本人可读）")


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = BLOB(len(data), buf), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("Windows 加密/解密失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _store_read():
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password", "-s", _svc(), "-a", KEY_ACCOUNT, "-w"],
                               capture_output=True, text=True, timeout=15)
            return r.stdout.strip() if r.returncode == 0 else ""
        raw = _key_file().read_bytes()
        return (_dpapi(raw, False) if sys.platform == "win32" else raw).decode().strip()
    except Exception:
        return ""


def _store_write(k):
    if sys.platform == "darwin":
        # 经 security -i 的标准输入传，Key 不出现在进程列表里
        r = subprocess.run(["security", "-i"], input=f'add-generic-password -U -s "{_svc()}" -a "{KEY_ACCOUNT}" -w "{k}"\n',
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError("存进钥匙串失败")
    else:
        f = _key_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_dpapi(k.encode(), True) if sys.platform == "win32" else k.encode())
        if sys.platform != "win32":
            f.chmod(0o600)
    if _store_read() != k:
        raise RuntimeError("存完读回来对不上，没有保存成功")


def _store_delete():
    if sys.platform == "darwin":
        subprocess.run(["security", "delete-generic-password", "-s", _svc(), "-a", KEY_ACCOUNT], capture_output=True, timeout=15)
    else:
        _key_file().unlink(missing_ok=True)


def _key(quiet=False):
    if not _key_mem["读过"]:
        _key_mem.update(读过=True, 值=_store_read() or None)
    if not _key_mem["值"] and not quiet:
        raise RuntimeError("还没有 RunningHub API Key：点左下角的「API Key」填一下")
    return _key_mem["值"]


def has_key():
    return bool(_key(quiet=True))


def _acct(d):
    """会员账户的余额在 remainPoint（2026-11-11 实测），老字段 remainCoins 是 0。"""
    bal = d.get("remainPoint")
    if bal in (None, "", 0, "0"):
        bal = d.get("remainCoins")
    try:
        bal = round(float(bal), 1)
    except (TypeError, ValueError):
        bal = None
    return {"余额": bal, "在跑": int(d.get("currentTaskCounts") or 0)}


def account():
    """账户状态（不扣费）：余额、正在跑几个。读不到返回 None。"""
    try:
        j = post("/uc/openapi/accountStatus", {"apikey": _key()}, timeout=20)
        d = j.get("data") if j.get("code") == 0 else None
        return _acct(d) if isinstance(d, dict) else None
    except Exception:
        return None


def set_key(k):
    """网页填的 Key：先连 RunningHub 验一下（不扣费），再加密存下。"""
    k = re.sub(r"\s", "", str(k or ""))
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", k):
        raise RuntimeError("这不像 RunningHub 的 API Key（应该是一串字母数字，在 RunningHub 个人中心的 API 调用里复制）")
    verified, acct = False, None
    if not H.MOCK:
        old = dict(_key_mem)
        _key_mem.update(读过=True, 值=k)
        try:
            j = post("/uc/openapi/accountStatus", {"apikey": k}, timeout=20)
        except Exception:
            j = None
        finally:
            _key_mem.update(old)
        if isinstance(j, dict) and j.get("code") == 0:
            verified = True
            acct = _acct(j.get("data") or {})
        elif isinstance(j, dict) and j.get("code") is not None:
            raise RuntimeError(f"RunningHub 不认这个 Key（{str(j.get('msg'))[:80]}），没有保存")
    _store_write(k)
    _key_mem.update(读过=True, 值=k)
    return {"已验证": verified, "账户": acct, "存放": key_place()}


def delete_key():
    _store_delete()
    _key_mem.update(读过=True, 值=None)


def migrate_keyfile():
    """老版本的 Key 明文放在 ~/.runninghub_key：加密存储里还没有的话抄一份进来。
    明文文件不删：独唱项目的 rh_run.py 等几个脚本还直接读它。"""
    if H.MOCK or _store_read():
        return None
    try:
        k = KEYFILE.read_text().strip()
    except OSError:
        return None
    if not k:
        return None
    _store_write(k)
    _key_mem.update(读过=True, 值=k)
    return key_place()


def vpn_connected():
    if sys.platform != "darwin":  # 只有 Mac 上能查 Surfshark；别的系统不查
        return True
    out = subprocess.run(["scutil", "--nc", "list"], capture_output=True, text=True).stdout
    return any("(Connected)" in l and "surfshark" in l.lower() for l in out.splitlines())


def _open(req, timeout):
    if urlparse(req.full_url).hostname != "www.runninghub.ai":
        raise RuntimeError(f"不认识的接口地址：{req.full_url}")
    return urllib.request.urlopen(req, timeout=timeout)


def post(path, body, timeout=60):
    data = json.dumps(dict(body, apiKey=_key())).encode()
    req = urllib.request.Request(HOST + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": H.UA})
    try:
        with _open(req, timeout) as r:
            return json.loads(r.read().decode(errors="replace") or "{}")
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            return json.loads(txt)
        except ValueError:
            return {"code": e.code, "msg": txt[:500]}


def _upload_file(path, ftype):
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("apiKey", _key()), ("fileType", ftype)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    fname = "upload" + Path(path).suffix.lower()  # 文件名只用来认扩展名，不把本地文件名发出去
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(HOST + "/task/openapi/upload", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": H.UA})
    with _open(req, 900) as r:
        j = json.loads(r.read().decode(errors="replace"))
    if j.get("code") != 0:
        raise RuntimeError(f"上传到 RunningHub 失败：{json.dumps(j, ensure_ascii=False)[:300]}")
    return j["data"]["fileName"]


def upload(key, path, kind):
    """传到 RunningHub，返回节点里要填的文件名。同一文件 24 小时内不重传。"""
    if H.MOCK:
        return f"api/demo_{Path(path).name}"
    rk = "rh:" + key
    with H.lock:
        r = H.load_json(H.UPLOADS_FILE, {}).get(rk)
        if r and time.time() - r["时间"] < UPLOAD_REUSE_SECONDS:
            return r["url"]
    name = _upload_file(path, kind)
    with H.lock:
        rec = H.load_json(H.UPLOADS_FILE, {})
        rec[rk] = {"url": name, "时间": time.time(), "文件": Path(path).name}
        H.save_json(H.UPLOADS_FILE, rec)
    return name


def placeholder(kind):
    """空格子用的占位文件：开跑前 ComfyUI 会检查每个加载节点的文件在不在，开关关着也要有。"""
    d = H.CACHE / "占位"
    d.mkdir(parents=True, exist_ok=True)
    p = d / {"image": "占位.png", "video": "占位.mp4", "audio": "占位.wav"}[kind]
    if not p.exists():
        src = {"image": ["-f", "lavfi", "-i", "color=black:s=64x64", "-frames:v", "1"],
               "video": ["-f", "lavfi", "-i", "color=black:s=64x64:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p"],
               "audio": ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1", "-c:a", "pcm_s16le"]}[kind]
        H.run(["ffmpeg", "-y", "-v", "error"] + src + [str(p)])
    return p


# ---------- 登记表 → 窗口里的「模型」 ----------

def load_workflows():
    """结构模板（RunningHub工作流/*.json）：参数怎么对到节点、有哪些格子。workflowId 不在这里选，在工作流库里。"""
    out = []
    for f in sorted(REG_DIR.glob("*.json")):
        reg = H.load_json(f, None)
        if not reg:
            continue
        reg["_文件"] = f.name
        slots = reg.get("格子")  # 格子表写在模板里，换电脑也能用；老模板从本地工作流的 extra.YZ_可空槽 读
        if not slots:
            local = H.load_json(reg.get("本地工作流", ""), None)
            slots = ((local or {}).get("extra") or {}).get("YZ_可空槽", {}).get("格子", [])
        reg["_格子"] = slots or []
        if not reg["_格子"]:
            reg["_问题"] = "结构模板里没有格子表（RunningHub工作流/" + f.name + "）"
        out.append(reg)
    return out


# ---------- 工作流库：存很多个 workflowId，生成时选 ----------

def _lib_file():
    return H.DATA / "工作流库.json"


def load_library():
    lib = H.load_json(_lib_file(), None)
    if lib is None:  # 第一次用：把模板里原来登记的 workflowId 搬进库
        lib = [{"id": uuid.uuid4().hex[:8], "名称": reg["名称"], "备注": "", "workflowId": str(reg["workflowId"]),
                "模板": reg["_文件"][:-5], "创建": datetime.now().isoformat(timespec="seconds")}
               for reg in load_workflows() if reg.get("workflowId")]
        H.save_json(_lib_file(), lib)
    return lib


def cloud_nodes(wid):
    """读云端工作流的节点表（不扣费）。读不到抛错，说明原因。"""
    j = post("/api/openapi/getJsonApiFormat", {"workflowId": str(wid)})
    data = j.get("data")
    if isinstance(data, dict):
        data = data.get("prompt", data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if not isinstance(data, dict) or not data:
        msg = str(j.get("msg") or "")
        if "NOT_EXISTS" in msg:
            raise RuntimeError(f"RunningHub 上没有工作流 {wid}：ID 抄错了，或者这个工作流已经删掉")
        raise RuntimeError(f"读不到云端工作流 {wid}：{msg or '平台没返回节点表'}")
    return data


def _reg_nodes(reg):
    """模板要改的节点：{节点号: 用途}。"""
    need = {}

    def add(n, why):
        for x in n if isinstance(n, list) else [n]:
            need.setdefault(str(x), why)
    add(reg["提示词"]["节点"], "提示词")
    add(reg["提示词"].get("分段节点", []), "分段提示词")
    for p in reg.get("参数", []):
        if "节点" in p:
            add(p["节点"], p.get("名") or p["键"])
    for k, v in reg.get("种子节点", {}).items():
        add(v["节点"], k + "种子")
    for v in reg.get("固定值", []):
        add(v["节点"], "固定设置")
    for n, label in reg.get("输出", {}).items():
        add(n, label + "存盘")
    for sl in reg.get("_格子", []):
        add(sl.get("同开关", []), sl["名字"] + "的分段开关")
    return need


def template_problems(reg, data):
    """云端节点表和这个结构模板对不对得上；返回人话写的问题列表（空 = 对得上）。"""
    out = []
    miss = {}
    for n, why in _reg_nodes(reg).items():
        if n not in data:
            miss.setdefault(why, []).append(n)
    if miss:
        out.append("找不到这些节点：" + "、".join(f"{why}（{'/'.join(ns)} 号）" for why, ns in miss.items()))
    gone = [s["名字"] for s in reg["_格子"] if str(s["加载节点"]) not in data or (s["开关节点"] and str(s["开关节点"]) not in data)]
    if gone:
        out.append("这些格子在云端读不到：" + "、".join(sorted(gone, key=lambda x: (re.sub(r"\d", "", x), int(re.sub(r"\D", "", x) or 0))))
                   + "。多半是它们的节点在工作流里被「绕过」了（发紫）：取消绕过再保存，空格子窗口会自己关")
    bad = [s["名字"] for s in reg["_格子"] if s["开关节点"] and str(s["开关节点"]) in data
           and data[str(s["开关节点"])].get("class_type") != "ComfySwitchNode"]
    if bad:
        out.append("这些格子的开关节点类型不对：" + "、".join(bad))
    return out


def match_template(data, prefer=None):
    """认出云端工作流是哪种结构：对得上的模板里挑要求最多的那个（分段版包含单段版的全部节点）。"""
    regs = [r for r in load_workflows() if r["_格子"]]
    if prefer:
        regs = [r for r in regs if r["_文件"][:-5] == prefer] or regs
    scored = [(template_problems(r, data), len(_reg_nodes(r)), r) for r in regs]
    ok = [x for x in scored if not x[0]]
    if ok:
        return max(ok, key=lambda x: x[1])[2], []
    if not scored:
        raise RuntimeError("没有可用的结构模板")
    probs, _, r = min(scored, key=lambda x: len(x[0]) * 1000 - x[1])
    return r, probs


def _lib_check(e):
    """核对一条库记录，结果记在它身上。演示模式不连平台。"""
    if H.MOCK:
        tpl = e.get("模板") or load_workflows()[0]["_文件"][:-5]
        e.update({"模板": tpl, "核对": {"时间": datetime.now().isoformat(timespec="seconds"), "结果": "演示模式没连平台", "问题": []}})
        return
    data = cloud_nodes(e["workflowId"])
    reg, probs = match_template(data, None)
    e["模板"] = reg["_文件"][:-5]
    e["核对"] = {"时间": datetime.now().isoformat(timespec="seconds"), "节点数": len(data), "结构": reg.get("结构名") or reg["名称"],
               "结果": "对不上" if probs else "通过", "问题": probs}
    if not probs:
        _cloud_ok[e["workflowId"]] = time.time()


def lib_public(e):
    regs = {r["_文件"][:-5]: r for r in load_workflows()}
    return {**e, "结构": (regs.get(e.get("模板")) or {}).get("结构名") or e.get("模板"), "endpoint": f"rh/{e.get('模板')}@{e['id']}"}


def lib_list():
    return [lib_public(e) for e in load_library()]


def lib_add(b):
    wid = re.sub(r"\s", "", str(b.get("workflowId") or ""))
    m = re.search(r"(\d{15,22})", wid)  # 直接贴工作流页面地址也行
    if not m:
        raise RuntimeError("工作流 ID 是一串数字（RunningHub 工作流页面地址里那串），这里没找到")
    wid = m.group(1)
    with H.lock:
        lib = load_library()
        if any(e["workflowId"] == wid for e in lib):
            raise RuntimeError("这个 ID 已经在工作流库里了：" + next(e["名称"] for e in lib if e["workflowId"] == wid))
    e = {"id": uuid.uuid4().hex[:8], "名称": (b.get("名称") or "").strip(), "备注": (b.get("备注") or "").strip(),
         "workflowId": wid, "创建": datetime.now().isoformat(timespec="seconds")}
    _lib_check(e)
    if not e["名称"]:
        e["名称"] = f"{e['核对'].get('结构') or '工作流'} {wid[-6:]}"
    with H.lock:
        lib = load_library()
        lib.append(e)
        H.save_json(_lib_file(), lib)
    return lib_public(e)


def lib_update(lid, b):
    with H.lock:
        lib = load_library()
        e = next((x for x in lib if x["id"] == lid), None)
        if not e:
            raise RuntimeError("工作流库里没有这一条")
        if "名称" in b:
            if not b["名称"].strip():
                raise RuntimeError("名称不能为空")
            e["名称"] = b["名称"].strip()
        if "备注" in b:
            e["备注"] = b["备注"].strip()
    if b.get("核对"):
        _lib_check(e)
    with H.lock:
        H.save_json(_lib_file(), lib)
    return lib_public(e)


def lib_delete(lid):
    with H.lock:
        lib = load_library()
        ep = "@" + lid
        if any(t.get("endpoint", "").endswith(ep) and t["状态"] not in H.TERMINAL for t in H.tasks.values()):
            raise RuntimeError("这个工作流还有任务在跑，等跑完再删")
        H.save_json(_lib_file(), [x for x in lib if x["id"] != lid])


def _slot_order(s):
    return int(re.sub(r"\D", "", s["名字"]) or 0)


def slot_groups(reg):
    """把格子分成窗口里的素材字段。参考格子按 1、2、3… 连续往后填，提示词里的编号才对得上。"""
    sl = reg["_格子"]
    refs = lambda kind: sorted([s for s in sl if s["类型"] == kind and s["开关节点"]], key=_slot_order)
    groups = {"image_urls": refs("image"), "video_urls": refs("video"), "audio_urls": refs("audio")}
    fixed = [s for s in sl if not s["开关节点"]]  # 不是 H3 参考的格子（如声音锁定的源音频）：必填单格
    return groups, fixed


def fixed_field(s):
    return "rh_" + s["名字"]


def as_model():
    """所有登记的工作流合成一个「RunningHub」模型，每个工作流是一种方式；参数表按登记表现做。"""
    ways = []
    regs = {r["_文件"][:-5]: r for r in load_workflows()}
    entries = [(e, regs.get(e.get("模板"))) for e in load_library()]
    if not entries:  # 库是空的：先让窗口能打开，提交时提示去加工作流
        entries = [({"id": "", "名称": r["名称"], "workflowId": "", "模板": k}, r) for k, r in regs.items()]
    for e, reg in entries:
        if not reg:
            continue
        ep = "rh/" + e["模板"] + ("@" + e["id"] if e["id"] else "")
        chk = e.get("核对") or {}
        problem = reg.get("_问题") or ("还没添加工作流 ID：到左边「工作流」里加一个" if not e["workflowId"] and not H.MOCK else None) \
            or ("；".join(chk["问题"]) if chk.get("问题") else None)
        props, required, names, kinds = {"prompt": {"type": "string", "minLength": 1}}, ["prompt"], {}, {}
        must = {}
        if reg["_格子"]:
            groups, fixed = slot_groups(reg)
            kinds.update(image_urls="image", video_urls="video", audio_urls="audio")
            for field, sl in groups.items():
                if sl:
                    props[field] = {"type": "array", "items": {"type": "string", "format": "uri"}, "maxItems": len(sl)}
                    if any(x["名字"] in reg.get("必填格子", []) for x in sl):  # 旧版分段：不放跑不了，窗口里提前说清楚（不标必填）
                        must[field] = OLD_SEG_TIP
            for s in fixed:
                f = fixed_field(s)
                props[f] = {"type": "string", "format": "uri"}
                required.append(f)
                names[f] = reg.get("格子名", {}).get(s["名字"], s["名字"])
                kinds[f] = s["类型"]
        for p in reg.get("参数", []):
            v = {k: p[k] for k in ("type", "minimum", "maximum", "multipleOf", "default", "description") if k in p}
            if "取值" in p:
                v = {"type": "string", "enum": list(p["取值"]), "default": p.get("default")}
            if p.get("说明"):
                v["description"] = p["说明"]
            if p.get("名"):
                v["title"] = p["名"]
            props[p["键"]] = v
        ways.append({
            "缺了不能跑": must,
            "名称": e["名称"], "endpoint": ep, "workflowId": e["workflowId"], "库id": e["id"], "备注": e.get("备注", ""),
            "结构": reg.get("结构名") or reg["名称"],
            "字段名": names, "格子类型": kinds, "输出名": list(reg.get("输出", {}).values()),
            "参数表": {"endpoint": ep, "来源": reg["_文件"],
                    "说明": problem or (e.get("备注") or f"{reg.get('结构名') or ''} · ID {e['workflowId']}"),
                    "schema": {"type": "object", "properties": props, "required": required}},
            "问题": problem,
        })
    return {"名称": "MiniMax H3", "简称": "RH", "厂家": "RunningHub（ComfyUI）", "平台": "runninghub",
            "启用": True, "标签": TAG, "标签类型": KIND_TAG, "计价": {"方式": "机器时长"}, "方式": ways}


def find_reg(endpoint, strict=False):
    """endpoint = rh/<结构模板>@<库里的编号>。老任务没有 @ 后半截，用模板自己登记的 workflowId。"""
    stem, _, lid = endpoint.split("/", 1)[1].partition("@")
    reg = next((r for r in load_workflows() if r["_文件"] == stem + ".json"), None)
    if not reg:
        raise RuntimeError(f"RunningHub工作流/ 里没有 {stem}.json")
    if lid:
        e = next((x for x in load_library() if x["id"] == lid), None)
        if e:
            reg["workflowId"], reg["名称"] = e["workflowId"], e["名称"]
        elif strict:
            raise RuntimeError("这个工作流已经从工作流库里删掉了，换一个再交")
    elif strict and not H.MOCK:
        reg["workflowId"] = ""
    return reg


# ---------- 分段（工作流加分段.py 做的工作流）----------

SEG_MARK = re.compile(r"^[ \t]*[=＝]{2,}[ \t]*第[ \t]*(\d)[ \t]*段[ \t]*[=＝]{2,}[ \t]*$", re.M)


def _frames(sec):
    a = max(5, round(float(sec) * 24))
    return a + (5 - a % 17) % 17


def seg_frames(reg, p):
    """本条每段帧数。分段时长空着或写「自动」：每段不超过上限（15 秒 = 362 帧，H3 节点的训练长度上限），
    超过就按最少段数均分（避免最后一段太短）；填数字就用那个秒数。"""
    sp = reg["分段"]
    ctx = sp.get("续接帧", 22)
    T = _frames(p.get("duration") or 5)
    v = str(p.get(sp["时长键"]) or sp.get("默认", "自动")).strip()
    if v in ("", "自动"):
        cap = _frames(sp.get("上限", 15))
        if T <= cap:
            return cap
        n = -(-(T - ctx) // (cap - ctx))
        L = -(-(T - ctx) // n) + ctx
        return L + (5 - L % 17) % 17
    try:
        return _frames(float(v))
    except ValueError:
        raise RuntimeError(f"分段时长要填秒数，或者空着 / 写「自动」（每段不超过 {sp.get('上限', 15)} 秒、均分），这里写的是「{v}」")


def seg_count(reg, p):
    """和工作流里「段数」节点同一个算法：总帧数 ≤ 段帧数 → 1 段，否则每段往后接（段帧 − 22）帧。"""
    sp = reg.get("分段")
    if not sp:
        return 1
    T = _frames(p.get("duration") or 5)
    L = seg_frames(reg, p)
    ctx = sp.get("续接帧", 22)
    if T <= L:
        return 1
    n = 1 + -(-(T - L) // (L - ctx))
    if n > sp.get("最多", 3):
        raise RuntimeError(f"总时长要 {n} 段，超过这个工作流最多 {sp.get('最多', 3)} 段；把分段时长调大，或者拆镜")
    return n


def seg_plan(reg, p):
    """每段在成片里的时间（秒）：[(起点, 新内容起点, 终点), …]。写分段提示词时用：第 k 段的时间从起点算。"""
    ctx = reg["分段"].get("续接帧", 22)
    T, L, n = _frames(p.get("duration") or 5), seg_frames(reg, p), seg_count(reg, p)
    out = []
    for i in range(n):
        a = i * (L - ctx)
        e = a + min(L, T - a)
        out.append((round(a / 24, 2), round((a + (ctx if i else 0)) / 24, 2), round(e / 24, 2)))
    return out


def split_prompt(reg, p):
    """提示词框里用一行「=== 第2段 ===」「=== 第3段 ===」隔开各段；分隔行前面是第 1 段。"""
    text = p.get("prompt", "")
    if not reg["提示词"].get("分段节点"):
        return [text]
    marks = list(SEG_MARK.finditer(text))
    parts = [text[:marks[0].start()] if marks else text]
    for i, m in enumerate(marks):
        if int(m.group(1)) != i + 2:
            raise RuntimeError(f"分段提示词的分隔行要按顺序写「=== 第{i + 2}段 ===」，这里写成了第{m.group(1)}段")
        parts.append(text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)])
    parts = [x.strip() for x in parts]
    n = seg_count(reg, p)
    if len(parts) < n or any(not x for x in parts[:n]):
        raise RuntimeError(f"这条要分 {n} 段（总时长 {p.get('duration')} 秒、每段 {seg_frames(reg, p)} 帧 = {seg_frames(reg, p) / 24:.2f} 秒），"
                           f"提示词要写 {n} 段：第 1 段写在最前面，后面每段前加一行「=== 第2段 ===」…。各段在成片里的时间："
                           + "；".join(f"第{i + 1}段 {a}–{e} 秒" + (f"（开头到 {b} 秒是上一段末尾）" if i else "") for i, (a, b, e) in enumerate(seg_plan(reg, p))))
    if len(parts) > n:
        raise RuntimeError(f"这条只分 {n} 段，提示词却写了 {len(parts)} 段；多出来的不会用到，删掉或者把总时长加长")
    return parts


OLD_SEG_TIP = ("这个分段工作流是旧版：不放「参考视频 1」跑不了。不用参考视频的镜头，先选单段工作流；"
               "或把新版分段工作流（参考视频可空）传到 RunningHub，再到左边「工作流」里添加它的 ID")


def check_required(reg, t):
    groups, _ = slot_groups(reg)
    for name in reg.get("必填格子", []):
        for field, sl in groups.items():
            idx = next((i for i, s in enumerate(sl) if s["名字"] == name), None)
            if idx is not None and len(t["素材"].get(field, [])) <= idx:
                raise RuntimeError(OLD_SEG_TIP)


SLOT_LABEL = {"image": "图像", "video": "参考视频", "audio": "参考音频"}


def check_undefined_slots(reg, t):
    """反向检查：参考格子放了素材，提示词（分段的话所有段合起来）里却没有对应的 <Picture N> / <Video N> / <Audio N>。
    没定义的格子模型会自己拿去用（S3-15 v3 图像3 留着空镜1，三条都多出一盏灯）。锁定声音这类固定格不是参考，不查。"""
    groups, _ = slot_groups(reg)
    text = t["参数"].get("prompt", "")
    bad = []
    for field, sl in groups.items():
        for i, aid in enumerate(t["素材"].get(field, [])[:len(sl)]):
            kind = sl[i]["类型"]
            tag = TAG.format(Kind=KIND_TAG[kind], n=i + 1)
            if tag not in text:
                name = H.load_json(H.ASSETS / f"{aid}.json", {}).get("名字", aid)
                bad.append(f"{SLOT_LABEL[kind]}{i + 1} 放了 {name}，但提示词里没有 {tag}")
    if bad:
        raise RuntimeError("；".join(bad) + "。没定义的格子模型会自己拿去用：要用就在提示词里定义，不用就清空")


# ---------- 拼节点值 ----------

def node_values(reg, t, files):
    """files：{格子名: RunningHub 文件名}，没放素材的格子放占位文件。返回 nodeInfoList 和给记录看的明细。"""
    groups, fixed = slot_groups(reg)
    p = t["参数"]
    nodes, detail = [], []
    add = lambda n, f, v: nodes.append({"nodeId": str(n), "fieldName": f, "fieldValue": v})
    used = {}
    for field, sl in groups.items():
        for i, s in enumerate(sl):
            aids = t["素材"].get(field, [])
            on = i < len(aids)
            used[s["名字"]] = aids[i] if on else None
            add(s["加载节点"], s["文件字段"], files[s["名字"]])
            add(s["开关节点"], "switch", on)
            for n in s.get("同开关", []):  # 分段版：每段切出来的参考视频各有一个开关，跟着一起开关
                add(n, "switch", on)
            if s["类型"] == "video":
                for k, v in reg.get("参考视频固定值", {}).items():
                    add(s["加载节点"], k, v)
    for s in fixed:
        aids = t["素材"].get(fixed_field(s), [])
        used[s["名字"]] = aids[0] if aids else None
        add(s["加载节点"], s["文件字段"], files[s["名字"]])
    parts = split_prompt(reg, p)
    add(reg["提示词"]["节点"], reg["提示词"]["字段"], parts[0])
    for k, nid in enumerate(reg["提示词"].get("分段节点", []), start=2):
        add(nid, reg["提示词"]["字段"], parts[k - 1] if k <= len(parts) else "")
    for d in reg.get("参数", []):
        if "节点" not in d or d["键"] not in p:
            continue
        v = p[d["键"]]
        v = d["取值"].get(v, v) if "取值" in d else v
        if reg.get("分段") and d["键"] == reg["分段"]["时长键"]:
            v = seg_frames(reg, p) / 24   # 工作流里 FRAMES(秒) 正好还原成这个帧数
        for nid in (d["节点"] if isinstance(d["节点"], list) else [d["节点"]]):
            add(nid, d["字段"], v)
    for d in reg.get("固定值", []):
        add(d["节点"], d["字段"], d["值"])
    sn = reg.get("种子节点", {})
    if "一采" in sn:
        add(sn["一采"]["节点"], sn["一采"]["字段"], t["种子"])
    if "二采" in sn:
        add(sn["二采"]["节点"], sn["二采"]["字段"], t["种子"] + sn["二采"].get("加", 0))
    stem = H.out_stem(t)
    for nid, label in reg.get("输出", {}).items():
        add(nid, "filename_prefix", f"{stem}_{label}")
    return nodes, used


def instance_type(reg, p):
    d = next((x for x in reg.get("参数", []) if x["键"] == "机器"), None)
    return d["取值"].get(p.get("机器"), "plus") if d else "plus"


_cloud_ok = {}


def check_cloud(reg, nodes):
    """提交前读一次云端工作流（不扣费），确认要改的节点都在，免得 803 NODE_INFO_MISMATCH 才发现传错了工作流。
    平台一时读不出来就跳过检查，不拦提交；明确对不上才拦。"""
    wid = reg["workflowId"]
    if H.MOCK or time.time() - _cloud_ok.get(wid, 0) < 3600:
        return
    try:
        data = cloud_nodes(wid)
    except RuntimeError as e:
        if "没有工作流" in str(e):
            raise
        return
    probs = template_problems(reg, data)
    missing = sorted({n["nodeId"] for n in nodes if n["nodeId"] not in data}, key=int)
    if missing and not probs:
        probs = [f"找不到节点 {', '.join(missing)}"]
    if probs:
        raise RuntimeError(f"云端工作流 {wid} 和窗口对不上：" + "；".join(probs))
    _cloud_ok[wid] = time.time()


# ---------- 提交 ----------

def prepare(req, m, w, tasks_new):
    """hf.submit 建好任务后调这里补 RunningHub 需要的字段。"""
    reg = find_reg(w["endpoint"], strict=True)
    if reg.get("_问题"):
        raise RuntimeError(reg["_问题"])
    if not reg.get("workflowId") and not H.MOCK:
        raise RuntimeError("还没添加工作流 ID：到左边「工作流」里加一个")
    if not H.MOCK:
        _key()
        if H.load_json(H.SETTINGS_FILE, {}).get("RunningHub要求VPN", True) and not vpn_connected():
            raise RuntimeError("Surfshark 没连上，不提交。先在 Mac 上连好 Surfshark App 再点生成")
    seed_txt = str(req["参数"].get("种子") or "").strip()
    if seed_txt and not seed_txt.isdigit():
        raise RuntimeError("种子要填数字，或者空着（随机）")
    split_prompt(reg, req["参数"])          # 分段工作流：段数和提示词段数要对上，提交前就拦下
    if tasks_new:
        check_required(reg, tasks_new[0])
        check_undefined_slots(reg, tasks_new[0])
    for i, t in enumerate(tasks_new):
        t["平台"] = "runninghub"
        t["workflowId"] = reg["workflowId"]
        t["种子"] = int(seed_txt) + i if seed_txt else random.randint(1, 2 ** 48)
        t["产出"] = []


def _running():
    """已经交给平台、还没结束的任务数（演示和正式分开算）。"""
    return sum(1 for x in H.tasks.values() if x.get("平台") == "runninghub" and x.get("request_id")
               and x["状态"] in ("queued", "in_progress") and x.get("演示", False) == H.MOCK)


def _my_turn(tid):
    """本机排队按提交顺序：轮到自己（最早在等的那条）且平台上不满 3 个才交。"""
    limit = int(H.load_json(H.SETTINGS_FILE, {}).get("RunningHub同时最多", MAX_RUNNING))
    waiting = sorted(x["id"] for x in H.tasks.values() if x.get("等待") and x["状态"] == "uploading"
                     and x.get("演示", False) == H.MOCK)
    return (not waiting or waiting[0] == tid) and _running() < limit, limit


def send_batch(ids):
    first = H.tasks[ids[0]]
    reg = find_reg(first["endpoint"])
    groups, fixed = slot_groups(reg)
    try:
        files = {}
        for field, sl in list(groups.items()) + [(fixed_field(s), [s]) for s in fixed]:
            aids = first["素材"].get(field, [])
            for i, s in enumerate(sl):
                if i < len(aids):
                    meta = H.load_json(H.ASSETS / f"{aids[i]}.json", None)
                    if not meta:
                        raise RuntimeError("有素材在缓存里找不到了，请重新拖进来")
                    files[s["名字"]] = upload(meta["id"], H.ASSETS / meta["上传文件"], s["类型"])
                else:
                    files[s["名字"]] = upload("占位:" + s["类型"], placeholder(s["类型"]), s["类型"])
    except Exception as e:
        for tid in ids:
            H._fail(tid, f"上传素材失败：{e}")
        return
    for tid in ids:  # 整批一起进本机队列，排队顺序 = 提交顺序
        H.tasks[tid]["等待"] = True
    for tid in ids:
        t = H.tasks[tid]
        try:
            nodes, used = node_values(reg, t, files)
            t["用到的格子"] = {k: (H.load_json(H.ASSETS / f"{v}.json", {}).get("名字", v) if v else None) for k, v in used.items()}
            reg["workflowId"] = t.get("workflowId") or reg["workflowId"]  # 以提交那一刻选的 ID 为准
            check_cloud(reg, nodes)
            body = {"workflowId": reg["workflowId"], "nodeInfoList": nodes}
            it = instance_type(reg, t["参数"])
            if it:
                body["instanceType"] = it
            t["节点值"] = nodes
            H.save_tasks()
            while True:
                if t["状态"] == "canceled":  # 本机排队、还没交出去时被取消
                    break
                with _slot_lock:  # 判断「轮到没有」和提交放在一起，两条线程不会同时占最后一个名额
                    ok, limit = _my_turn(tid)
                    if not ok:
                        r = None
                    elif H.MOCK:
                        t.update({"request_id": "demo-" + uuid.uuid4().hex[:12], "状态": "queued", "提交": time.time(),
                                  "等待": False, "提示": None})
                        break
                    else:
                        r = post("/task/openapi/create", body, timeout=120)
                        if r.get("code") == 0:
                            t["等待"] = False
                            t.update({"request_id": str(r["data"]["taskId"]), "状态": "queued", "提交": time.time(), "提示": None})
                if r is None:
                    tip = f"本机排队：RunningHub 上已有 {limit} 个在跑，前面的跑完就自动交（这时取消不扣费）"
                    if t.get("提示") != tip:
                        t["提示"] = tip
                        H.save_tasks()
                    time.sleep(5)
                    continue
                msg = json.dumps(r, ensure_ascii=False)
                if r.get("code") == 0:
                    d = r["data"]
                    tips = d.get("promptTips")
                    if tips and tips not in ("{}", '{"node_errors": {}}'):
                        t["平台检查"] = tips[:3000]
                    if d.get("netWssUrl"):
                        threading.Thread(target=ws_listen, args=(tid, d["netWssUrl"], reg), daemon=True).start()
                    break
                if r.get("code") == 421 or "QUEUE_MAXED" in msg:
                    t["提示"] = f"RunningHub 说排队已满，{QUEUE_RETRY_SECONDS} 秒后再交（这时取消不扣费）"
                    H.save_tasks()
                    time.sleep(QUEUE_RETRY_SECONDS)
                    continue
                raise RuntimeError(f"RunningHub 拒绝了：{msg[:800]}")
        except Exception as e:
            H._fail(tid, str(e))
            continue
        H.save_tasks()


# ---------- 实时进度（netWssUrl，ComfyUI 式消息）----------

WS_LOG = None  # _缓存/实时连接.log（bind 后定）
WS_MAX = 32 << 20  # 一条实时消息最大 32 MB（进度消息只有几 KB；不设上限的话，对面发一条巨大的消息能把内存占满）
WS_LOST_TIP = "实时连接断了，正在重连（断开期间一采跑完的话，要等整条跑完才下）"


def ws_log(tid, text, url=None):
    """实时连接的每次连上、断开、出错、收到的消息都记一行，下次丢消息能看出是哪一种。连接地址可能带凭据，不写进去。"""
    if url:
        text = text.replace(url, "<连接地址>")
    line = f"{datetime.now().isoformat(timespec='seconds')}  {tid}  {text}\n"
    try:
        path = WS_LOG or (Path(H.CACHE) / "实时连接.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _ws_state(t, state, tip):
    """记连接状态；断开时在卡片上提示，重连上就把这条提示撤掉（别的提示不动）。"""
    t["实时连接"] = state
    if tip:
        if not t.get("中途取件"):
            t["提示"] = WS_LOST_TIP
    elif t.get("提示") == WS_LOST_TIP:
        t["提示"] = None
    H.save_tasks()


def ws_listen(tid, url, reg):
    """跑完一采、二采时马上标出来，按 executed 消息里的 cos_url 中途取件。
    断了就一直重连到任务结束（以前连 5 次不成就放弃，后面整条都收不到一采消息）。netWssUrl 可能带凭据，只在内存里用。"""
    t = H.tasks[tid]
    try:
        from websockets.sync.client import connect
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError as e:
        ws_log(tid, f"没法连实时进度：缺少库 {e}")
        _ws_state(t, "没连（缺库）", True)
        return
    outs = reg.get("输出", {})
    if urlsplit(url).scheme != "wss":  # 只连加密的实时连接
        ws_log(tid, "实时连接地址不是 wss，没有连")
        _ws_state(t, "没连（地址不是 wss）", True)
        return
    fails, n_conn = 0, 0
    while t["状态"] in ("queued", "in_progress"):
        try:
            with connect(url, ssl=ctx, open_timeout=20, max_size=WS_MAX) as ws:
                n_conn += 1
                fails = 0
                ws_log(tid, f"连上（第 {n_conn} 次）")
                _ws_state(t, "已连", False)
                seen = set()
                while t["状态"] in ("queued", "in_progress"):
                    try:
                        raw = ws.recv(timeout=60)
                    except TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        continue
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    typ, data = msg.get("type"), msg.get("data") if isinstance(msg.get("data"), dict) else {}
                    node = str(data.get("node") or "")
                    if typ not in seen:
                        seen.add(typ)
                        ws_log(tid, f"第一次收到 {typ} 消息")
                    if typ == "executing" and t["状态"] == "queued":
                        t["状态"] = "in_progress"
                    if typ == "executed":
                        ws_log(tid, f"executed 节点 {node}" + (f"（{outs[node]}）" if node in outs else ""))
                    if typ == "executed" and node in outs:
                        label = outs[node]
                        t.setdefault("阶段", {})[label] = datetime.now().isoformat(timespec="seconds")
                        t["中途输出"] = {**t.get("中途输出", {}), label: _file_info(data.get("output"))}
                        t["提示"] = f"{label} 已跑完，下载中"
                        H.save_tasks()
                        _try_midway(t, reg, node, t["中途输出"][label])
                        ws_log(tid, f"{label} 中途取件：{t.get('中途取件', {}).get(label)}")
                    if typ in ("execution_success", "execution_error", "execution_interrupted"):
                        ws_log(tid, f"收到 {typ}，实时连接结束")
                        t["实时连接"] = "结束"
                        return
                ws_log(tid, f"任务已是 {t['状态']}，断开实时连接")
                return
        except Exception as e:
            if t["状态"] not in ("queued", "in_progress"):
                break
            fails += 1
            wait = min(60, 5 * 2 ** min(fails - 1, 4))  # 5、10、20、40、60、60… 秒
            ws_log(tid, f"断开/连不上（连续第 {fails} 次）：{type(e).__name__} {str(e)[:200]}；{wait} 秒后重连", url)
            _ws_state(t, f"断开，重连中（连续 {fails} 次）", True)
            time.sleep(wait)
    ws_log(tid, f"任务已是 {t['状态']}，不再重连")


def _file_info(output):
    """executed 消息里的输出：每一项的字段都记下（长值截到 300 字），用来判断能不能中途取件。"""
    out = []
    for key, v in (output or {}).items():
        for it in v if isinstance(v, list) else [v]:
            if isinstance(it, dict):
                out.append({"组": key, **{k: (x[:300] if isinstance(x, str) else x) for k, x in it.items()
                                          if isinstance(x, (str, int, float, bool)) or x is None}})
            else:
                out.append({"组": key, "值": str(it)[:300]})
    return out


def _try_midway(t, reg, node, info):
    """这一采刚存盘：按消息里的 cos_url 马上下载（视频文件那一项），不等别的采跑完。"""
    if H.MOCK:
        return
    url = next((x.get("cos_url") for x in info if x.get("cos_url") and str(x.get("format", "")).startswith("video")), None)
    try:
        if not url:
            raise RuntimeError("消息里没有 cos_url")
        _fetch(t, reg, node, url, None)
        t["中途取件"] = {**t.get("中途取件", {}), reg["输出"][node]: "已下载"}
        t["提示"] = f"{reg['输出'][node]} 已下载"
    except Exception as e:
        t["中途取件"] = {**t.get("中途取件", {}), reg["输出"][node]: f"没下成，跑完再下：{str(e)[:200]}"}
    H.save_tasks()


# ---------- 查状态、下载 ----------

def poll(t):
    reg = find_reg(t["endpoint"])
    if H.MOCK:
        return _poll_mock(t, reg)
    r = post("/task/openapi/status", {"taskId": t["request_id"]}, timeout=30)
    s = r.get("data")
    if s == "QUEUED":
        t["状态"] = "queued"
    elif s == "RUNNING":
        t["状态"] = "in_progress"
    elif s == "SUCCESS":
        o = post("/task/openapi/outputs", {"taskId": t["request_id"]}, timeout=60)
        if o.get("code") != 0 or not isinstance(o.get("data"), list):
            raise RuntimeError(f"取结果出错：{json.dumps(o, ensure_ascii=False)[:300]}")
        _download_outputs(t, reg, o["data"])
        _done(t, reg)
    elif s == "FAILED":
        o = post("/task/openapi/outputs", {"taskId": t["request_id"]}, timeout=60)
        t["状态"] = "failed"
        t["错误"] = "RunningHub 运行失败：" + _fail_reason(o)
        t["完成"] = datetime.now().isoformat(timespec="seconds")
    else:
        raise RuntimeError(f"状态看不懂：{json.dumps(r, ensure_ascii=False)[:200]}")


def _fail_reason(o):
    d = o.get("data")
    if isinstance(d, dict):
        fr = d.get("failedReason") or d
        if isinstance(fr, dict):
            return " ".join(str(fr.get(k)) for k in ("node_name", "exception_message") if fr.get(k)) or json.dumps(fr, ensure_ascii=False)[:600]
    return json.dumps(o, ensure_ascii=False)[:600]


def _safe_url(url):
    """平台地址里有中文文件名（前缀是中文时），urllib 要先转码。"""
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, quote(u.path, safe="/%"), quote(u.query, safe="=&%"), u.fragment))


_fetch_lock = threading.Lock()


def _fetch(t, reg, nid, url, cost):
    """下载一份输出（实时进度线程和查状态线程都会调，同一节点只下一次）。"""
    outs = reg.get("输出", {})
    with _fetch_lock:
        if nid in outs and nid in {x["节点"] for x in t["产出"]}:
            return
        if urlsplit(url).scheme != "https":  # 下载地址是平台给的：只认 https，不认 file:// 和明文 http
            raise RuntimeError("平台给的下载地址不是 https，没有下载")
        base = H.safe_name(url.split("?")[0].rsplit("/", 1)[-1], "输出")
        if nid in outs:
            p = H.unique(Path(t["输出目录"]) / f"{H.out_stem(t)}_{outs[nid]}.mp4")
        else:
            p = H.unique(Path(t["输出目录"]) / "_其他输出" / f"{H.out_stem(t)}_{base}")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        req = urllib.request.Request(_safe_url(url), headers={"User-Agent": H.UA})
        with urllib.request.urlopen(req, timeout=1800) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(p)
        x = {"节点": nid, "名称": outs.get(nid, base), "视频": H.posix(p), "耗时": cost}
        t["产出"].append(x)
        _sidecar(t, x)
        if nid == reg.get("成片") or not t.get("视频"):
            t["视频"] = H.posix(p)
        H.save_tasks()


def _download_outputs(t, reg, items):
    outs = reg.get("输出", {})
    for it in sorted(items, key=lambda x: list(outs).index(str(x.get("nodeId"))) if str(x.get("nodeId")) in outs else 99):
        nid = str(it.get("nodeId") or "")
        if it.get("fileUrl"):
            _fetch(t, reg, nid, it["fileUrl"], it.get("taskCostTime"))
        x = next((y for y in t["产出"] if y["节点"] == nid), None)
        if x and x.get("耗时") is None and it.get("taskCostTime"):  # 中途下载的那一份补上平台耗时
            x["耗时"] = it.get("taskCostTime")


def _sidecar(t, x):
    record = {"平台": "RunningHub", "工作流": t["方式"], "workflowId": t.get("workflowId"), "taskId": t.get("request_id"),
              "这一份": x["名称"], "节点": x["节点"], "种子_一采": t.get("种子"),
              "提示词": t["参数"].get("prompt", ""), "参数": {k: v for k, v in t["参数"].items() if k != "prompt"},
              "格子": t.get("用到的格子"), "提交": t["创建"], "完成": datetime.now().isoformat(timespec="seconds"),
              "平台耗时": x.get("耗时"), "节点值": [n for n in t.get("节点值", []) if n["fieldName"] != "text"],
              "演示": t.get("演示", False)}
    Path(x["视频"]).with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def _done(t, reg):
    t["状态"] = "completed"
    t["完成"] = datetime.now().isoformat(timespec="seconds")
    t["提示"] = None
    missing = [v for k, v in reg.get("输出", {}).items() if k not in {x["节点"] for x in t["产出"]}]
    if missing:
        t["提示"] = "平台没给这几份：" + "、".join(missing)


def _poll_mock(t, reg):
    """演示：3 秒开跑，6 秒一采出来（演示里能中途下载），12 秒二采出来、完成。"""
    age = time.time() - t["提交"]
    outs = list(reg.get("输出", {}).items())
    t["状态"] = "queued" if age < 3 else "in_progress"
    secs = min(int(t["参数"].get("duration") or 4), 4)
    for i, (nid, label) in enumerate(outs):
        if age >= 6 + 6 * i and nid not in {x["节点"] for x in t["产出"]}:
            p = H.unique(Path(t["输出目录"]) / f"{H.out_stem(t)}_{label}.mp4")
            size = "960x544" if i == 0 else "1920x1088"
            H.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={secs}",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)])
            x = {"节点": nid, "名称": label, "视频": H.posix(p), "耗时": None}
            t["产出"].append(x)
            t.setdefault("阶段", {})[label] = datetime.now().isoformat(timespec="seconds")
            _sidecar(t, x)
            if nid == reg.get("成片") or not t.get("视频"):
                t["视频"] = H.posix(p)
            t["提示"] = f"{label} 已下载"
    if len(t["产出"]) >= len(outs):
        _done(t, reg)


def cancel(t):
    """排队、运行中都能取消。取消后平台上这条任务就查不到了，还没下载的那一采也拿不到。"""
    if t["状态"] == "uploading" and not t.get("request_id"):
        with _slot_lock:  # 别和正在提交的那一刻撞上
            if not t.get("request_id"):
                t["状态"], t["错误"], t["等待"] = "canceled", "已取消（还没交给 RunningHub，不扣费）", False
                t["完成"] = datetime.now().isoformat(timespec="seconds")
                return
    if not H.MOCK:
        r = post("/task/openapi/cancel", {"taskId": t["request_id"]})
        if r.get("code") != 0:
            raise RuntimeError(f"取消失败：{json.dumps(r, ensure_ascii=False)[:300]}")
    t["状态"] = "canceled"
    got = "、".join(x["名称"] for x in t.get("产出", []))
    t["错误"] = "已取消" + (f"（已下载：{got}）" if got else "")
    t["完成"] = datetime.now().isoformat(timespec="seconds")
