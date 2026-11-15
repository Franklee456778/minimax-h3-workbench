#!/usr/bin/env python3
"""把 MiniMax H3 工作台打成免安装包：Mac 版（Apple 芯片）和 Windows 版（64 位）各一个 zip。

  python3 打包.py              两个都打
  python3 打包.py mac          只打 Mac 版
  python3 打包.py win          只打 Windows 版
  python3 打包.py --带工作流库   把 Peter 现在的 工作流库.json 一起放进去（默认是空的，对方自己加 ID）

包里的结构（文件夹名用英文，Windows 的启动脚本才不会因为编码出问题）：
  启动.command / 启动.bat
  使用说明.txt
  app/       程序：hf.py、rh.py、web/、RunningHub工作流/（结构模板，不带 workflowId）
  runtime/   运行环境：python/、ffmpeg/、lib/（websockets、certifi）、LICENSES.txt
  data/      数据：项目、工作流库、缓存、出片——换电脑时拷这个文件夹

运行文件放在 下载/（不进 git，来源见 runtime/LICENSES.txt）；成品在 成品/。
"""
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "H3工作台"
if not SRC.exists():
    SRC = HERE.parent / "Higgsfield生成"
DL, OUT = HERE / "下载", HERE / "成品"
NAME = "MiniMax H3 工作台"

MAC_CMD = """#!/bin/bash
# 双击打开 MiniMax H3 工作台；关掉这个终端窗口就停止。
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null   # 从网上或隔空投送拿到的包，先去掉系统的隔离标记，里面的程序才能运行
export H3_DATA="$PWD/data"
export PATH="$PWD/runtime/ffmpeg:$PATH"
export PYTHONPATH="$PWD/runtime/lib"
export PYTHONDONTWRITEBYTECODE=1
exec "$PWD/runtime/python/bin/python3" "$PWD/app/hf.py" "$@"
"""

WIN_BAT = """@echo off
rem MiniMax H3 workbench launcher. Close this window to stop.
cd /d "%~dp0"
set "H3_DATA=%~dp0data"
set "PATH=%~dp0runtime\\ffmpeg;%PATH%"
set "PYTHONPATH=%~dp0runtime\\lib"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
title MiniMax H3
"%~dp0runtime\\python\\python.exe" "%~dp0app\\hf.py" %*
if errorlevel 1 pause
"""

README = """MiniMax H3 工作台（{plat}）  版本 {ver}

怎么用
  1. 把整个文件夹放到一个你有写权限的地方（桌面、文档、移动硬盘都行），不用安装。
  2. 双击「{launcher}」。会开一个黑色的窗口（后台），浏览器自动打开 http://127.0.0.1:8790/ 。
     关掉那个黑色窗口，工作台就停了。
{first}
  3. 第一次用：
     - 点左下角「没有 API Key」，粘贴你的 RunningHub API Key（保存前会验证，加密保存在本机）。
     - 点左边「工作流」，贴上 RunningHub 工作流的 ID，点「添加并核对」。
     - 左边「新建项目」，然后就可以放素材、写提示词、点生成了。

数据在哪
  data 文件夹：项目、工作流库、素材缓存、出的片（data/出片）。
  换电脑、换新版本时，把旧的 data 文件夹整个拷到新包里覆盖即可，项目和记录都还在。
  API Key：Mac 存在系统钥匙串，不在 data 里；Windows 存在 data/_缓存/api_key.bin，是加密的，只有这台电脑的这个账户能解开。
  所以换电脑时不管哪个版本都要重新填一次 Key（Windows 拷过去的那个文件解不开，不会泄露）。

说明
  - 只在本机运行，网页只有这台电脑能打开；生成在 RunningHub 上跑，按 RunningHub 的规则扣费。
  - 每次提交前都有确认框。
  - 包里带的 Python、FFmpeg 的来源和许可见 runtime/LICENSES.txt。
  - FFmpeg 按 GPL v3 授权：三年内可以免费索取它这次构建的完整源码，索取方式见 runtime/LICENSES.txt 里的「源码书面承诺」。
"""

LICENSES = """这个包里带的第三方软件（许可原文在 runtime/许可原文/ 和下面写的位置）

1. Python {py}（python-build-standalone 打的免安装版）
   来源：https://github.com/astral-sh/python-build-standalone （release 20261110）
   许可：PSF License，原文 {pylic}
   Python 里编进去的库：OpenSSL 3.5（Apache-2.0）、libffi（MIT）、expat（MIT）、zlib（zlib）、
     libmpdec（BSD-2-Clause）、xz/liblzma（0BSD）、bzip2（bzip2 许可）、SQLite（公有领域，无需署名）、
     Tcl/Tk（Tcl/Tk 许可）{extra}
   以上许可原文：runtime/许可原文/Python-第三方组件许可.txt、bzip2-Tcl-Tk许可.txt、xz-0BSD.txt{msvc}

2. FFmpeg 9.0（GPL 版，含 x264、x265 等编码库；没有 nonfree 部分）
   来源：{ff}
   许可：GNU GPL v3，全文 runtime/FFmpeg-LICENSE.txt
   源码：FFmpeg 官方 https://ffmpeg.org/download.html ；本构建的配置和各依赖库版本：{ffsrc}
   工作台只是把 ffmpeg 当成独立程序来调用（转素材格式、做缩略图），没有和它链接在一起，工作台本身的代码不受 GPL 约束。

{offer}

3. websockets 17.1    https://pypi.org/project/websockets/   BSD-3-Clause，原文 runtime/lib/websockets-17.1.dist-info/licenses/LICENSE
4. certifi 2026.10.23 https://pypi.org/project/certifi/      MPL-2.0，原文 runtime/lib/certifi-2026.10.23.dist-info/licenses/LICENSE（未修改，源码就是包里的文件）
"""


# 运行文件的 SHA-256（2026-11-11 下载时记下）。打包前逐个核对：下载/ 里的文件被换过、下坏了就停，不把来路不明的程序打进包里。
# 升级某个运行文件时，从官方发布页核对新文件的校验值，再改这里。
SHA256 = {
    "certifi-2026.10.23-py3-none-any.whl": "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1",
    "ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip": "f7fe240cfc7312e40e8686982f73fbf2b377b8cdc3bd52795e157d5b7036ebbf",
    "mac_ffmpeg.zip": "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924",
    "mac_ffprobe.zip": "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6",
    "py_mac_arm64.tar.gz": "7a2384641584ab9984221fcb0fc9265df465922af71be71410529e2a9c3f0540",
    "py_win_x64.tar.gz": "2e0fed09d48989f64c9305da9ff2d37a41c0d13ab2aa6283405b2b620dfbf805",
    "websockets-17.1-py3-none-any.whl": "f221081107b8c48184d99f7019604486376e7ef826037e70aad6b02540732c23",
}


def verify_downloads():
    import hashlib
    extra = sorted(f.name for f in DL.iterdir() if f.is_file() and not f.name.startswith(".") and f.name not in SHA256)
    if extra:
        raise SystemExit(f"下载/ 里有没登记的文件：{'、'.join(extra)}（要用就先把它的 SHA-256 记进 打包.py）")
    for name, want in SHA256.items():
        f = DL / name
        if not f.exists():
            raise SystemExit(f"下载/ 里缺 {name}")
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise SystemExit(f"{name} 的校验值对不上，文件可能被换过或没下完整，没有打包")


def version():
    """版本号读 hf.py 里的 VERSION（界面左下角显示的也是它），后面跟打包日期。"""
    import re
    m = re.search(r'^VERSION = "([^"]+)"', (SRC / "hf.py").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("hf.py 里找不到 VERSION，不能打包")
    return f"v{m.group(1)}（{datetime.now():%Y-%m-%d} 打包）"


# 成品带的结构模板：分段、参考视频可空。≤15 秒只跑 1 段，和单段工作流一样，所以所有镜头都用它（Peter 2026-11-12 定）。
# v1.1 加 7 段版（每段仍 ≤15 秒，最长 90 秒；Peter 2026-11-14 定）；3 段版留着，v1.0 时部署的 3 段工作流 ID 照样能用。
# 窗口按云端节点自动认是哪一个。开发文件夹里的另外两个模板留着（工作流库里老的 ID 还对着它们）。
# 7 段 v2（2026-11-14）：网页上不用参考视频1 时关掉加载节点就行，各段自动跳过；窗口按「识别节点」把它和 v1 分开。
PACK_TEMPLATES = ["信_Pika_分段_参考可空", "信_Pika_分段7_参考可空", "信_Pika_分段7v2_空格可关"]


def copy_app(dst, with_library):
    app = dst / "app"
    app.mkdir(parents=True)
    for f in ("hf.py", "rh.py"):
        shutil.copy2(SRC / f, app / f)
    shutil.copytree(SRC / "web", app / "web")
    (app / "RunningHub工作流").mkdir()
    for f in [SRC / "RunningHub工作流" / f"{n}.json" for n in PACK_TEMPLATES]:
        reg = json.loads(f.read_text(encoding="utf-8"))
        if not reg.get("格子"):
            raise SystemExit(f"{f.name} 里没有格子表，不能打包")
        for k in ("workflowId", "本地工作流"):  # 包里的模板不带 Peter 的工作流 ID 和本机路径
            reg.pop(k, None)
        (app / "RunningHub工作流" / f.name).write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    data = dst / "data"
    (data / "_缓存").mkdir(parents=True)
    (data / "出片").mkdir()
    # 别人的电脑上不要求连 Surfshark（那是 Peter 自己机器上的规矩）
    (data / "_缓存" / "设置.json").write_text(json.dumps({"RunningHub要求VPN": False}, ensure_ascii=False), encoding="utf-8")
    lib = []
    if with_library:
        lib = [e for e in json.loads((SRC / "工作流库.json").read_text(encoding="utf-8")) if e.get("模板") in PACK_TEMPLATES]
    (data / "工作流库.json").write_text(json.dumps(lib, ensure_ascii=False, indent=1), encoding="utf-8")
    (data / "项目.json").write_text("[]", encoding="utf-8")


# FFmpeg 源码的书面承诺（GPL v3 第 6 条；Peter 2026-11-12 定用这种方式）。索取渠道用公开仓库的 Issues，不写个人联系方式。
OFFER_CONTACT = "https://github.com/Franklee456778/minimax-h3-workbench/issues"
SOURCE_OFFER = """   【源码书面承诺】
   本包里的 FFmpeg 程序按 GNU GPL v3 授权。自你收到本包之日起三年内（只要我们还在提供本软件或对它的支持，期限相应延长），
   任何持有本包的人都可以向我们索取这次构建对应的完整源码（FFmpeg 本身、编进去的各个库的源码和构建脚本），
   我们免费提供下载；如需光盘、U 盘等实物，只收介质和寄送的成本费。
   索取方式：在 {contact} 新建一个 Issue，写明「索取 FFmpeg 源码」和你用的是 {plat}。
   这次构建：{build}
   在此之前也可以先从上游取：{upstream}

   [Written offer] The FFmpeg binaries in this package are licensed under the GNU GPL v3. For at least three years from
   the date you received this package, anyone who has it may request the complete Corresponding Source for this build
   (FFmpeg, the libraries compiled into it, and the build scripts), free of charge as a download, or on physical media
   for no more than the cost of the media and shipping. Request it by opening an issue at {contact}.
   Build: {build_en}
"""


LIC_DIR = HERE / "许可原文"  # 第三方许可原文（文字文件，进 git）：CPython 第三方组件、bzip2/Tcl/Tk、xz、GPLv3


def add_licenses(rt, gpl=True):
    d = rt / "许可原文"
    d.mkdir(parents=True, exist_ok=True)
    for f in ("Python-第三方组件许可.txt", "bzip2-Tcl-Tk许可.txt", "xz-0BSD.txt"):
        shutil.copy2(LIC_DIR / f, d / f)
    if gpl:
        shutil.copy2(LIC_DIR / "GPL-3.0.txt", rt / "FFmpeg-LICENSE.txt")


def add_libs(rt):
    lib = rt / "lib"
    lib.mkdir(parents=True)
    for whl in DL.glob("*.whl"):
        with zipfile.ZipFile(whl) as z:
            z.extractall(lib)


def untar(name, to):
    with tarfile.open(DL / name) as t:
        t.extractall(to, filter="data")  # 不让压缩包里的路径写到目标文件夹外面，也不带进特殊文件


def build_mac(ver, with_library):
    dst = OUT / f"{NAME} Mac版"
    shutil.rmtree(dst, ignore_errors=True)
    copy_app(dst, with_library)
    rt = dst / "runtime"
    add_libs(rt)
    untar("py_mac_arm64.tar.gz", rt)
    ff = rt / "ffmpeg"
    ff.mkdir()
    for z in ("mac_ffmpeg.zip", "mac_ffprobe.zip"):
        with zipfile.ZipFile(DL / z) as zf:
            zf.extractall(ff)
    for f in ff.iterdir():
        f.chmod(0o755)
    add_licenses(rt)
    (rt / "LICENSES.txt").write_text(LICENSES.format(
        py="3.13.18", pylic="runtime/python/lib/python3.13/LICENSE.txt",
        extra="、libedit（BSD-3-Clause，readline 模块用）、ncurses（MIT 风格）——这两个的许可原文 python-build-standalone 的精简版没带，见 https://thrysoee.dk/editline/ 和 https://invisible-island.net/ncurses/",
        msvc="",
        ff="https://ffmpeg.martin-riedl.de （macOS arm64，9.0.2）", ffsrc="https://ffmpeg.martin-riedl.de",
        offer=SOURCE_OFFER.format(contact=OFFER_CONTACT, plat="Mac 版",
                                  build="FFmpeg 9.0.2 正式版，martin-riedl.de 的 macOS arm64 构建（2026-09-20）",
                                  build_en="FFmpeg 9.0.2 release, macOS arm64 build by martin-riedl.de (2026-09-20)",
                                  upstream="FFmpeg 9.0.2 源码 https://ffmpeg.org/releases/ ；构建方式和依赖库 https://ffmpeg.martin-riedl.de")), encoding="utf-8")
    cmd = dst / "启动.command"
    cmd.write_text(MAC_CMD, encoding="utf-8")
    cmd.chmod(0o755)
    first = "     如果系统说「无法打开，因为无法验证开发者」：在「启动.command」上点右键 → 打开 → 再点「打开」，只需要这一次。"
    (dst / "使用说明.txt").write_text(README.format(plat="Mac 版，Apple 芯片", ver=ver, launcher="启动.command", first=first), encoding="utf-8")
    z = OUT / f"{NAME} Mac版.zip"
    z.unlink(missing_ok=True)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", "--norsrc", str(dst), str(z)], check=True)
    return dst, z


def build_win(ver, with_library):
    dst = OUT / f"{NAME} Windows版"
    shutil.rmtree(dst, ignore_errors=True)
    copy_app(dst, with_library)
    rt = dst / "runtime"
    add_libs(rt)
    untar("py_win_x64.tar.gz", rt)
    ff = rt / "ffmpeg"
    ff.mkdir()
    with zipfile.ZipFile(DL / "ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip") as zf:
        for n in zf.namelist():
            base = n.rsplit("/", 1)[-1]
            if "/bin/" in n and base and base != "ffplay.exe":
                (ff / base).write_bytes(zf.read(n))
            elif base == "LICENSE.txt":
                (rt / "FFmpeg-LICENSE.txt").write_bytes(zf.read(n))
    add_licenses(rt, gpl=False)  # Windows 的 GPL 全文在解压 FFmpeg 时已经放好（FFmpeg-LICENSE.txt）
    (rt / "LICENSES.txt").write_text(LICENSES.format(
        py="3.13.18", pylic="runtime/python/LICENSE.txt", extra="",
        msvc="\n   微软 Visual C++ 运行库（vcruntime140.dll 等）：按微软「可再发行代码」条款随 Python 一起分发，条款见 runtime/python/LICENSE.txt",
        ff="https://github.com/BtbN/FFmpeg-Builds （win64-gpl-shared，n9.0）", ffsrc="https://github.com/BtbN/FFmpeg-Builds",
        offer=SOURCE_OFFER.format(contact=OFFER_CONTACT, plat="Windows 版",
                                  build="FFmpeg n9.0.2-49-gc179411bbb（release/9.0 分支），BtbN/FFmpeg-Builds 的 win64-gpl-shared 自动构建（2026-11-11）",
                                  build_en="FFmpeg n9.0.2-49-gc179411bbb (release/9.0 branch), BtbN/FFmpeg-Builds win64-gpl-shared autobuild (2026-11-11)",
                                  upstream="FFmpeg 源码 https://git.ffmpeg.org/ffmpeg.git 的提交 c179411bbb ；构建脚本和依赖库版本 https://github.com/BtbN/FFmpeg-Builds")), encoding="utf-8")
    (dst / "启动.bat").write_bytes(WIN_BAT.replace("\n", "\r\n").encode("ascii"))
    first = "     如果弹出「Windows 已保护你的电脑」：点「更多信息」→「仍要运行」。防火墙问是否允许，选允许（只在本机用）。"
    (dst / "使用说明.txt").write_bytes(("﻿" + README.format(plat="Windows 版，64 位", ver=ver, launcher="启动.bat", first=first)).replace("\n", "\r\n").encode("utf-8"))
    z = OUT / f"{NAME} Windows版.zip"
    z.unlink(missing_ok=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:  # zipfile 会给中文文件名打上 UTF-8 标记，Windows 解压不乱码
        for f in sorted(dst.rglob("*")):
            if f.is_file():
                zf.write(f, f"{dst.name}/{f.relative_to(dst).as_posix()}")
    return dst, z


def size(p):
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with_library = "--带工作流库" in sys.argv
    OUT.mkdir(exist_ok=True)
    verify_downloads()
    ver = version()
    for plat, fn in (("mac", build_mac), ("win", build_win)):
        if args and plat not in args:
            continue
        dst, z = fn(ver, with_library)
        print(f"{dst.name}：文件夹 {size(dst) / 1e6:.0f} MB，zip {size(z) / 1e6:.0f} MB → {z}")


if __name__ == "__main__":
    main()
