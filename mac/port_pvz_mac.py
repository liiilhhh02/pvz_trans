#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
植物大战僵尸杂交版 —— Windows/Android 导出包 → macOS .app 一键打包器
======================================================================

用法（最常用）:
    python3 port_pvz_mac.py --zip "植物大战僵尸杂交版0.29.zip" --apk "植物大战僵尸杂交版0.29.APK"

    # 只看两个包的结构/版本，不打包
    python3 port_pvz_mac.py --inspect --zip A.zip --apk B.APK

    # 顺便做一个 Intel + Apple Silicon 通吃的包（额外下载一份 osx-x64 .NET 运行时）
    python3 port_pvz_mac.py --zip A.zip --apk B.APK --universal

    # 在只有低带宽时：手动把两个大文件放进缓存目录（脚本会打印确切路径），再跑
    python3 port_pvz_mac.py --zip A.zip --apk B.APK --offline

原理（详见 MIGRATION_NOTES.md）:
  这类游戏是 Godot 4.7 + C#(.NET 9) 的导出产物。pck（游戏数据）与托管程序集都是跨平台的：
  Windows 与 Android 两份包里的程序集，IL 逐字节相同（只差 PE machine / 时间戳 / MVID），
  且 0 个 P/Invoke。所以"移植"= 用官方 macOS 引擎 + 原 pck + 自包含 .NET 运行时拼一个 .app。

  唯一**必须**的补丁（已用最小复现实验证实）：
    程序集的 PE machine 要改成 AnyCPU(0x14C)。csc 会按 RID 写出 0x8664(x64)/0xAA64(arm64)，
    虽然 CorFlags.IlOnly=true，但 macOS 上 CoreCLR 就是拒绝绑定，报
    0x80132006 CLR_E_BIND_SYS_ASM_NI_MISSING，上层症状是
    ".NET: Failed to get GodotPlugins initialization function pointer"。
    把 PE 头那 2 字节改成 0x14C，同一个进程里立刻能取到入口点（rc=0）。

  两个"看起来像原因但不是"的坑（写在这里避免再被误导）：
    * 数据目录缺 Microsoft.NETCore.App.deps.json：在自包含(includedFrameworks)布局下**不影响**
      运行（Windows 官方导出就没有这个文件）；只有改成 framework-dependent(shared/) 布局才需要它。
    * 组件模式(hostfxr_initialize_for_runtime_config)不支持自包含 runtimeconfig，
      所以别去试"用 runtime_config 模式启动"那条路。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

try:  # 可选依赖：deflate64 (zip method 9) 解压
    import inflate64  # type: ignore
except ImportError:  # pragma: no cover
    inflate64 = None

GODOT_RELEASE = "https://github.com/godotengine/godot/releases/download/{ver}"
TEMPLATES_ASSET = "Godot_v{ver}_mono_export_templates.tpz"
EDITOR_ASSET = "Godot_v{ver}_mono_macos.universal.zip"

NUGET_MIRRORS = [
    # 华为云 Artifactory 镜像（国内 ~10MB/s），实测可用
    "https://repo.huaweicloud.com/artifactory/api/nuget/v3/nuget-remote",
    # 官方源（国外/慢）
    "https://api.nuget.org/v3-flatcontainer",
]

PACK_FORMAT_SUPPORTED = 4        # Godot 4.7 的 pck 目录格式
ENGINE_MIN = (4, 7, 0)           # 已实测验证过的引擎底线
FRAMEWORK_FALLBACK = "9.0.17"    # 包内 runtimeconfig 缺失时的兜底

SIGNAL_BAD_ENTRYPOINT = "Failed to get GodotPlugins initialization function pointer"
SIGNAL_NO_FRAMEWORK = "hostfxr_initialize_for_dotnet_command_line failed with code: -2147450730"

# --------------------------------------------------------------------------------------
# 终端输出 & 报错
# --------------------------------------------------------------------------------------

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def phase(title: str) -> None:
    print("\n" + _c("1;36", f"== {title} ") + _c("1;36", "=" * max(0, 66 - len(title))))


def info(msg: str) -> None:
    print(f"   {msg}")


def ok(msg: str) -> None:
    print(_c("32", f"   [OK] {msg}"))


def warn(msg: str) -> None:
    print(_c("33", f"   [WARN] {msg}"))


def verbose(enabled: bool, msg: str) -> None:
    if enabled:
        print(f"   [dbg] {msg}", flush=True)


class PortError(Exception):
    """带排障建议的用户级错误。"""

    def __init__(self, title, cause="", fix="", extra=""):
        super().__init__(title)
        self.title = title
        self.cause = cause
        self.fix = fix
        self.extra = extra

    def render(self) -> str:
        out = ["", _c("1;31", "!! 打包失败：" + self.title)]
        if self.cause:
            out += ["", "   原因/证据："]
            out += [f"     {ln}" for ln in str(self.cause).splitlines()]
        if self.extra:
            out += ["", "   参考信息："]
            out += [f"     {ln}" for ln in str(self.extra).splitlines()]
        if self.fix:
            out += ["", _c("33", "   怎么办：")]
            out += [f"     {ln}" for ln in str(self.fix).splitlines()]
        return "\n".join(out)


def die(err: PortError):
    print(err.render(), file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------------------
# 依赖自举（inflate64：这类包的 zip 用的是 deflate64/method 9，系统 unzip 不支持）
# --------------------------------------------------------------------------------------

def ensure_inflate64(cache: str) -> None:
    """确保能用 inflate64 解 deflate64(method 9)。缺失时静默装进缓存目录，不污染系统环境。"""
    global inflate64
    if inflate64 is not None:
        return
    import importlib
    pydeps = os.path.join(cache, "pydeps")
    if pydeps not in sys.path:
        sys.path.insert(0, pydeps)
    try:
        inflate64 = importlib.import_module("inflate64")
        return
    except ImportError:
        pass
    info("缺少 inflate64（解 deflate64 压缩的 zip 用），正在装到缓存目录…")
    os.makedirs(pydeps, exist_ok=True)
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", pydeps, "inflate64"],
                       check=True)
        importlib.invalidate_caches()
        inflate64 = importlib.import_module("inflate64")
    except Exception as exc:
        raise PortError(
            "无法安装 inflate64",
            cause=f"{type(exc).__name__}: {exc}",
            fix="任选其一：\n"
                f"  {sys.executable} -m pip install --target '{pydeps}' inflate64\n"
                "  python3 -m pip install --user inflate64\n"
                "  brew install sevenzip   # 或用 7z 手动解出 zip 后用 --windows-dir/--apk-dir 模式",
        )


# --------------------------------------------------------------------------------------
# zip 读取（含 deflate64）
# --------------------------------------------------------------------------------------

def _raw_member_bytes(zf, name, max_bytes=None):
    info = zf.getinfo(name)
    fp = zf.fp
    fp.seek(info.header_offset)
    hdr = fp.read(30)
    if len(hdr) < 30:
        raise PortError("zip 结构损坏", cause=f"{name}: 本地文件头被截断")
    sig, ver, flg, method, mt, md, crc, csz, usz, nlen, elen = struct.unpack("<IHHHHHIIIHH", hdr)
    if sig != 0x04034B50:
        raise PortError("zip 结构损坏", cause=f"{name}: 本地文件头签名非法 (0x{sig:08x})")
    fp.seek(info.header_offset + 30 + nlen + elen)
    size = info.compress_size if max_bytes is None else min(info.compress_size, max_bytes)
    return fp.read(size)


def _inflate64(raw: bytes) -> bytes:
    d = inflate64.Inflater()
    return d.inflate(raw)


def read_member(zf, name, limit=64 << 20, head=None):
    """读取 zip 成员；head=N 表示只读前 N 字节（支持 store/deflate/deflate64）。"""
    info = zf.getinfo(name)
    want = head if head is not None else info.file_size
    if head is None and info.file_size > limit:
        raise PortError("文件过大", cause=f"{name} = {info.file_size} 字节 > 上限 {limit}")
    if info.compress_type in (0, 8):
        with zf.open(name) as fh:
            return fh.read(want)
    if info.compress_type == 9:
        if head is None:
            return _inflate64(_raw_member_bytes(zf, name))[:want]
        chunk = max(want * 4, 1 << 16)
        out = b""
        while len(out) < want:
            raw = _raw_member_bytes(zf, name, max_bytes=chunk)
            if not raw:
                break
            out = _inflate64(raw)
            chunk *= 4
        return out[:want]
    raise PortError(
        "zip 压缩方式不支持",
        cause=f"{name}: compression method = {info.compress_type}",
        fix="只支持 store(0) / deflate(8) / deflate64(9)。若是其它方式，请用 7z 解包后再用 --windows-dir/--apk-dir 参数。",
    )


def stream_member_to_file(zf, name, dest, chunk=1 << 22):
    """把 zip 成员流式解到文件（支持 deflate64；用于几 GB 的 pck）。"""
    info = zf.getinfo(name)
    if info.compress_type in (0, 8):
        with zf.open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, chunk)
    elif info.compress_type == 9:
        if inflate64 is None:
            raise PortError("需要 inflate64 才能解 deflate64 条目", cause=name)
        fp = zf.fp
        fp.seek(info.header_offset)
        hdr = fp.read(30)
        sig, ver, flg, method, mt, md, crc, csz, usz, nlen, elen = struct.unpack("<IHHHHHIIIHH", hdr)
        if sig != 0x04034B50:
            raise PortError("zip 结构损坏", cause=f"{name}: 本地文件头签名非法")
        fp.seek(info.header_offset + 30 + nlen + elen)
        d = inflate64.Inflater()
        left = info.compress_size
        written = 0
        with open(dest, "wb") as dst:
            while left > 0:
                raw = fp.read(min(chunk, left))
                if not raw:
                    break
                left -= len(raw)
                out = d.inflate(raw)
                if out:
                    dst.write(out)
                    written += len(out)
    else:
        raise PortError("zip 压缩方式不支持", cause=f"{name}: method {info.compress_type}")
    if os.path.getsize(dest) != info.file_size:
        raise PortError("解压结果大小不符", cause=f"{name}: {os.path.getsize(dest)} != {info.file_size}")


# --------------------------------------------------------------------------------------
# 包结构识别
# --------------------------------------------------------------------------------------

PCK_HEAD = 40


def gunzip_entries(zf):
    return zf.namelist()


def classify(zf, kind):
    """识别导出包里的关键文件。返回 dict。"""
    found = {
        "pck": None, "exe": None, "hta": None, "publish_dir": None,
        "publish_dlls": [], "deps": None, "runtimeconfig": None,
        "godotsharp": None, "android_dlls": [], "sparsepck": None, "android_libs": [],
    }
    for n in zf.namelist():
        low = n.lower()
        base = n.rsplit("/", 1)[-1]
        if n.endswith("/"):
            continue
        if low.endswith(".pck"):
            found["pck"] = n
        elif low.endswith(".exe"):
            found["exe"] = n
        elif low.endswith(".hta"):
            found["hta"] = n
        elif re.search(r"(^|/)(data_[^/]+)/$", n):
            found["publish_dir"] = n.rstrip("/")
        elif low.endswith(".deps.json"):
            found["deps"] = n
        elif low.endswith(".runtimeconfig.json"):
            found["runtimeconfig"] = n
        elif base == "GodotSharp.dll":
            found["godotsharp"] = n
        elif "/mono/publish/" in low and low.endswith(".dll"):
            found["android_dlls"].append(n)
        elif re.search(r"(^|/)(data_[^/]+)/[^/]+\.dll$", n):
            found["publish_dlls"].append(n)
        elif low.endswith("sparsepck"):
            found["sparsepck"] = n
        elif re.match(r"^lib/.*\.so$", low):
            found["android_libs"].append(n)
    if not found["publish_dir"]:
        src = found["deps"] or found["runtimeconfig"] or (found["publish_dlls"][0] if found["publish_dlls"] else None)
        if src and "/" in src:
            found["publish_dir"] = src.rsplit("/", 1)[0]
    return found


def pck_header(zf, name, with_entries=False):
    head = read_member(zf, name, head=PCK_HEAD)
    if head[:4] != b"GDPC":
        raise PortError(
            "这不是 Godot 的 pck 文件",
            cause=f"{name} 前 4 字节 = {head[:4]!r}（期望 b'GDPC'）",
            fix="确认 --zip 指向的是游戏本体导出包（里面应有 *.pck），不是补丁或资源包。",
        )
    fmt, maj, mi, pat, flags = struct.unpack("<5I", head[4:24])
    base, dir_off = struct.unpack("<2Q", head[24:40])
    size = zf.getinfo(name).file_size
    if flags & 1:
        raise PortError(
            "pck 目录被加密（Godot --encrypt / 自定义加密）",
            cause=f"flags=0x{flags:x}（bit0 = 目录加密）",
            fix="加密包需要导出时使用的密钥，脚本无法处理。请换未加密的版本。",
        )
    entry = None
    if with_entries and dir_off and dir_off < size:
        # 注意：deflate64 不能随机读，取这个数要把整包流一遍，所以只在 --inspect 时算
        raw = read_member(zf, name, head=dir_off + 4)[dir_off:dir_off + 4]
        if len(raw) == 4:
            entry = struct.unpack("<I", raw)[0]
    return {"name": name, "format": fmt, "version": f"{maj}.{mi}.{pat}",
            "version_tuple": (maj, mi, pat), "flags": flags, "file_base": base,
            "dir_offset": dir_off, "size": size, "entries": entry}


def read_json_member(zf, name, label):
    try:
        return json.loads(read_member(zf, name, limit=8 << 20))
    except PortError:
        raise
    except Exception as exc:
        raise PortError(
            f"无法解析 {label}",
            cause=f"{name}: {type(exc).__name__}: {exc}",
            fix="确认这是 Godot .NET 导出（文件应为 UTF-8 JSON）。",
        )


def pe_machine(data: bytes):
    if data[:2] != b"MZ":
        raise PortError("不是有效的 PE 文件", cause="缺少 MZ 头")
    pe = struct.unpack("<I", data[0x3C:0x40])[0]
    return struct.unpack("<H", data[pe + 4:pe + 6])[0], pe


def inspect(path, kind) -> dict:
    zf = zipfile.ZipFile(path)
    rep = {"path": path, "kind": kind, "bytes": os.path.getsize(path), "entries": len(zf.namelist())}
    found = classify(zf, kind)
    rep["found"] = {k: v for k, v in found.items() if v}
    if kind == "apk":
        try:
            from pyaxmlparser import APK
            a = APK(path)
            rep["apk"] = {"package": a.package, "versionName": a.version_name,
                          "minSdk": a.get_min_sdk_version(), "targetSdk": a.get_target_sdk_version()}
        except Exception as exc:  # pyaxmlparser 可选
            rep["apk"] = {"note": f"未解析（pyaxmlparser 不可用：{type(exc).__name__}）"}
    if found["pck"]:
        rep["pck"] = pck_header(zf, found["pck"], with_entries=True)
    if found["deps"]:
        deps = read_json_member(zf, found["deps"], "deps.json")
        rep["deps"] = {"runtimeTarget": deps.get("runtimeTarget", {}).get("name"),
                       "targets": list(deps.get("targets", {})),
                       "runtimepacks": [k for k in deps.get("libraries", {}) if k.startswith("runtimepack")]}
    if found["runtimeconfig"]:
        rc = read_json_member(zf, found["runtimeconfig"], "runtimeconfig.json")
        ro = rc.get("runtimeOptions", {})
        rep["runtimeconfig"] = {"tfm": ro.get("tfm"), "framework": ro.get("framework"),
                                "includedFrameworks": ro.get("includedFrameworks")}
    # 程序集：优先 APK 里 RID 专属那份
    cands = [n for n in found["android_dlls"] if not n.lower().endswith("godotsharp.dll")]
    if not cands:
        cands = [n for n in found["publish_dlls"] if not n.lower().endswith(("godotsharp.dll",))]
    if cands:
        name = max(cands, key=lambda n: zf.getinfo(n).file_size)
        data = read_member(zf, name, limit=128 << 20)
        mach, _ = pe_machine(data)
        rep["assembly"] = {"entry": name, "bytes": len(data), "machine": hex(mach)}
        try:
            import dnfile
            pe = dnfile.dnPE(data=data)
            flags = pe.net.Flags
            rep["assembly"]["ilonly"] = bool(getattr(flags, "CLR_ILONLY", False))
            rep["assembly"]["implmap"] = 0 if pe.net.mdtables.ImplMap is None else len(pe.net.mdtables.ImplMap.rows)
            rep["assembly"]["moduleref"] = 0 if pe.net.mdtables.ModuleRef is None else len(pe.net.mdtables.ModuleRef.rows)
            refs = {str(r.Name): f"{r.MajorVersion}.{r.MinorVersion}" for r in pe.net.mdtables.AssemblyRef.rows}
            rep["assembly"]["godotsharp_ref"] = refs.get("GodotSharp")
            rep["assembly"]["has_native_image"] = any(
                d.name == "IMAGE_DIRECTORY_ENTRY_MANAGED_NATIVE_HEADER" and d.Size
                for d in pe.OPTIONAL_HEADER.DATA_DIRECTORY)
        except ImportError:
            rep["assembly"]["note"] = "未安装 dnfile，跳过托管元数据检查"
        except Exception as exc:
            rep["assembly"]["note"] = f"dnfile 分析失败：{type(exc).__name__}: {exc}"
    return rep


# --------------------------------------------------------------------------------------
# 下载（GitHub 签名 URL + CDN 直连分段续传；NuGet 走国内镜像）
# --------------------------------------------------------------------------------------

def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_head(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), url
    except Exception as exc:
        return 0, {"error": str(exc)}, url


def resolve_redirect(url, timeout=40):
    """用系统代理取一次跳转，拿到 CDN 的签名 URL（GitHub 直连常被限速而 CDN 往往很快）。"""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        loc = exc.headers.get("Location") if exc.headers else None
        if loc:
            return loc
    except Exception:
        pass
    _, headers, eff = http_head(url, timeout)
    if eff and eff != url:
        return eff
    return None


def fetch_ranges(url, offset, size, dest, log_prefix="", timeout_keep=280, chunk_report=1 << 24):
    """把 url 的 [offset, offset+size) 追加写入 dest（支持断点续传）。

    每 timeout_keep 秒重取一次签名 URL（GitHub 链接会过期），并按 CDN 直连优先。
    """
    tmp = dest
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    opener = _no_proxy_opener()
    deadline = time.time() + timeout_keep
    while have < size:
        if time.time() > deadline:
            signed = resolve_redirect(url)
            if not signed:
                raise PortError("下载链接解析失败", cause=f"取不到 {url} 的跳转地址",
                                fix="网络/代理问题。可用 --offline 手动放置缓存文件（脚本会打印路径）。")
            url = signed
            deadline = time.time() + timeout_keep
        start = offset + have
        end = offset + size - 1
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
        try:
            with opener.open(req, timeout=60) as resp:
                if resp.status not in (200, 206):
                    raise PortError("下载被拒绝", cause=f"HTTP {resp.status} @ {url[:120]}…")
                with open(tmp, "ab") as fh:
                    last = have
                    while True:
                        block = resp.read(1 << 18)
                        if not block:
                            break
                        fh.write(block)
                        have += len(block)
                        if have >= size:
                            break
                        if have - last >= chunk_report:
                            last = have
                            verbose(True, f"{log_prefix}{have/1e6:.1f}/{size/1e6:.1f} MB")
        except PortError:
            raise
        except Exception:
            # 超时/断流：循环里会自动重取 URL 续传
            time.sleep(2)
    return dest


def download_via_mirrors(package, version, dest_dir):
    """从 NuGet 镜像下载 package.version，返回本地 nupkg 路径。"""
    fname = f"{package}.{version}.nupkg"
    dest = os.path.join(dest_dir, fname)
    if os.path.exists(dest) and os.path.getsize(dest) > 1 << 20:
        return dest
    last_err = []
    for base in NUGET_MIRRORS:
        url = f"{base}/{package}/{version}/{fname}"
        try:
            code, headers, _ = http_head(url, timeout=25)
            size = int(headers.get("Content-Length") or 0)
            info(f"下载 {fname} <- {base.split('/')[2]} ({size/1e6:.1f} MB)")
            if code not in (200, 206) or size <= 0:
                last_err.append(f"  {url} -> HTTP {code}")
                continue
            if os.path.exists(dest):
                os.remove(dest)
            fetch_ranges(url, 0, size, dest)
            if os.path.getsize(dest) != size:
                last_err.append(f"  {url} -> 大小不符 ({os.path.getsize(dest)} != {size})")
                continue
            return dest
        except PortError as exc:
            last_err.append(f"  {url} -> {exc.title}")
        except Exception as exc:
            last_err.append(f"  {url} -> {type(exc).__name__}: {exc}")
    raise PortError(
        f"下载 NuGet 包 {package} {version} 失败",
        cause="\n".join(last_err) or "无可用镜像",
        fix="检查网络/代理，或手动下载后放进缓存目录：\n"
            f"  {dest}\n"
            f"  下载地址示例：{NUGET_MIRRORS[0]}/{package}/{version}/{fname}\n"
            "  然后重跑（脚本会自动复用）。",
    )


# --------------------------------------------------------------------------------------
# 缓存：引擎模板 + .NET 运行时
# --------------------------------------------------------------------------------------

def engine_cache_app(cache, ver):
    return os.path.join(cache, "engine", f"macos_template_{ver}.app")


def fetch_engine_template(cache, ver, offline=False, allow_editor=False):
    """取官方 macOS .NET 导出模板里的 macos_template.app。

    做法：tpz 是 zip，只做 range 下载——先取尾部拿中央目录，再按偏移只抓 templates/macos.zip。
    """
    app = engine_cache_app(cache, ver)
    binary = template_binary(app)
    if binary:
        return app
    rel = f"{ver.split('.')[0]}.{ver.split('.')[1]}-stable"
    tpz_url = f"{GODOT_RELEASE.format(ver=rel)}/" + TEMPLATES_ASSET.format(ver=rel)
    macos_zip = os.path.join(cache, "engine", f"macos_{ver}.zip")
    os.makedirs(os.path.dirname(macos_zip), exist_ok=True)

    if not os.path.exists(macos_zip):
        if offline:
            raise PortError(
                "离线模式下缺少 macOS 导出模板",
                cause=f"需要文件：{macos_zip}",
                fix=f"联网时执行：\n  python3 {os.path.basename(__file__)} --zip A.zip --apk B.APK\n"
                    f"或手动下载官方 mono 导出模板 tpz，从中取出 templates/macos.zip 后放到：\n  {macos_zip}\n"
                    f"（下载页：https://godotengine.org/download/archive/{rel}/ ）",
            )
        info(f"下载 Godot {rel} 导出模板索引（只取 macos.zip，不整包下载）")
        code, headers, _ = http_head(tpz_url, timeout=30)
        total = int(headers.get("Content-Length") or 0)
        if code not in (200, 206) or total <= 0:
            raise PortError(
                f"找不到 Godot {rel} 的官方导出模板",
                cause=f"HEAD {tpz_url} -> HTTP {code}",
                fix="确认该版本存在（https://godotengine.org/download/archive/），"
                    "若是新引擎版本，请先更新本脚本里的引擎版本探测逻辑/常量。",
            )
        signed = resolve_redirect(tpz_url)
        if not signed:
            raise PortError("无法解析下载地址", cause=tpz_url)
        tail_len = min(1 << 20, total)
        tail_path = os.path.join(cache, "engine", "tpz_tail.bin")
        if os.path.exists(tail_path):
            os.remove(tail_path)
        fetch_ranges(signed, total - tail_len, tail_len, tail_path)
        tail = open(tail_path, "rb").read()
        entry = find_zip_entry(tail, total - tail_len, "templates/macos.zip")
        if not entry:
            raise PortError(
                "导出模板里没有 templates/macos.zip",
                cause=f"在 {os.path.basename(tpz_url)} 中央目录里未找到该条目",
                fix="可能是版本命名变化（如 templates/macos.universal.zip）。请把官方 macos.zip 取出后放到：\n"
                    f"  {macos_zip}",
            )
        # 读本地文件头，拿到真正的数据起始位置（本地头里的 name/extra 长度可能和中央目录不同）
        lh_path = os.path.join(cache, "engine", "local_header.bin")
        if os.path.exists(lh_path):
            os.remove(lh_path)
        fetch_ranges(signed, entry["local_offset"], 30, lh_path)
        lh = open(lh_path, "rb").read()
        if len(lh) < 30 or lh[:4] != b"PK\x03\x04":
            raise PortError("无法读取 tpz 的本地文件头",
                            cause=f"offset={entry['local_offset']} 处不是 PK\\x03\\x04",
                            fix="请手动解出 macos.zip 放到：\n"
                                f"  {macos_zip}")
        l_nlen, l_elen = struct.unpack("<HH", lh[26:30])
        data_offset = entry["local_offset"] + 30 + l_nlen + l_elen
        info(f"下载 templates/macos.zip（{entry['compress_size']/1e6:.1f} MB 压缩，分段续传）")
        if os.path.exists(macos_zip):
            os.remove(macos_zip)
        signed = resolve_redirect(tpz_url) or signed
        fetch_ranges(signed, data_offset, entry["compress_size"], macos_zip)
        if entry["method"] == 8:
            data = decompress_raw_deflate(open(macos_zip, "rb").read())
            open(macos_zip, "wb").write(data)

    os.makedirs(os.path.dirname(app), exist_ok=True)
    with zipfile.ZipFile(macos_zip) as z:
        names = z.namelist()
        top = {n.split("/")[0] for n in names}
        root = next(iter(top)) if len(top) == 1 else None
        if root is None:
            raise PortError("macos.zip 结构异常", cause=f"顶层条目：{sorted(top)[:5]}")
        tmp = app + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        z.extractall(tmp)
        shutil.move(os.path.join(tmp, root), app)
        shutil.rmtree(tmp, ignore_errors=True)
    if not template_binary(app):
        raise PortError("macos.zip 里没有可执行文件", cause=f"{app}/Contents/MacOS 为空")
    ok(f"引擎模板就绪：{app}")
    return app


def parse_eocd(tail_bytes):
    """解析 zip 尾部：返回中央目录的位置/大小（含 zip64 回退）。"""
    idx = tail_bytes.rfind(b"PK\x05\x06")
    if idx < 0:
        return None
    (_disk, _cd_disk, _n_disk, n_total, cd_size, cd_off, _clen) = struct.unpack(
        "<HHHHIIH", tail_bytes[idx + 4:idx + 22])
    if cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF or n_total == 0xFFFF:
        z = tail_bytes.rfind(b"PK\x06\x06")
        if z >= 0:
            n_total = struct.unpack("<Q", tail_bytes[z + 32:z + 40])[0]
            cd_size = struct.unpack("<Q", tail_bytes[z + 40:z + 48])[0]
            cd_off = struct.unpack("<Q", tail_bytes[z + 48:z + 56])[0]
    return {"entries": n_total, "cd_size": cd_size, "cd_off": cd_off}


def walk_central_directory(tail_bytes, tail_start, eocd):
    """在尾部缓冲里遍历中央目录，返回条目列表。"""
    p = eocd["cd_off"] - tail_start
    if p < 0 or p >= len(tail_bytes):
        return []
    entries = []
    while tail_bytes[p:p + 4] == b"PK\x01\x02":
        (ver, verneed, flags, method, mt, md, crc, csize, usize, nlen, elen, clen,
         disk, iattr, eattr, off) = struct.unpack("<HHHHHHIIIHHHHHII", tail_bytes[p + 4:p + 46])
        name = tail_bytes[p + 46:p + 46 + nlen].decode("utf8", "replace")
        entries.append({"name": name, "method": method, "compress_size": csize,
                        "size": usize, "local_offset": off})
        p += 46 + nlen + elen + clen
    return entries


def find_zip_entry(tail_bytes, tail_start, want_name):
    """在 zip 尾部里找目标条目。优先精确名，其次后缀匹配。"""
    eocd = parse_eocd(tail_bytes)
    if not eocd:
        return None
    entries = walk_central_directory(tail_bytes, tail_start, eocd)
    for e in entries:
        if e["name"] == want_name:
            return e
    for e in entries:
        if e["name"].endswith("/" + want_name.split("/")[-1]):
            return e
    return None


def decompress_raw_deflate(raw: bytes) -> bytes:
    import zlib
    d = zlib.decompressobj(-15)
    out = d.decompress(raw)
    out += d.flush()
    return out


def template_binary(app):
    d = os.path.join(app, "Contents", "MacOS")
    if not os.path.isdir(d):
        return None
    cands = [os.path.join(d, f) for f in os.listdir(d)]
    cands = [p for p in cands if os.path.isfile(p) and not p.endswith(".real")]
    if not cands:
        return None
    rel = [p for p in cands if "release" in os.path.basename(p).lower()]
    return (rel or cands)[0]


GODOT_ARCH = {"osx-arm64": "arm64", "osx-x64": "x86_64", "linux-x64": "x86_64",
              "linux-arm64": "arm64", "win-x64": "x86_64", "android-arm64": "arm64"}


def godot_arch(rid):
    """RID → Godot Engine::get_architecture_name() 的取值（数据目录后缀用它）。"""
    return GODOT_ARCH.get(rid, rid.split("-", 1)[1])


def dotnet_key(rid, version):
    return f"{rid}-{version}"


def prepare_dotnet(cache, rid, version, tfm, offline=False):
    """准备 .NET 运行时（自包含布局）：<cache>/netcore/<rid>-<ver>/{shared/host}"""
    root = os.path.join(cache, "netcore", dotnet_key(rid, version))
    fw = os.path.join(root, "shared", "Microsoft.NETCore.App", version)
    fxr = os.path.join(root, "host", "fxr", version, "libhostfxr.dylib")
    manifest = os.path.join(fw, "Microsoft.NETCore.App.deps.json")
    if os.path.exists(fxr) and os.path.exists(manifest) and os.path.exists(os.path.join(fw, "System.Private.CoreLib.dll")):
        return root
    if offline:
        raise PortError(
            "离线模式下缺少 .NET 运行时",
            cause=f"需要目录：{fw} 与 {fxr}",
            fix="联网时重跑本脚本即可自动下载（国内镜像很快），或手动放置以下两个 nupkg 到 "
                f"{os.path.join(cache, 'netcore')}：\n"
                f"  microsoft.netcore.app.runtime.{rid}.{version}.nupkg\n"
                f"  microsoft.netcore.app.host.{rid}.{version}.nupkg",
        )
    os.makedirs(root, exist_ok=True)
    nupkg_dir = os.path.join(cache, "netcore")
    os.makedirs(nupkg_dir, exist_ok=True)
    # ---- 运行时包（框架本体） ----
    pack = download_via_mirrors(f"microsoft.netcore.app.runtime.{rid}", version, nupkg_dir)
    with zipfile.ZipFile(pack) as z:
        lib_prefix = f"runtimes/{rid}/lib/{tfm}/"
        nat_prefix = f"runtimes/{rid}/native/"
        libs = [n for n in z.namelist() if n.startswith(lib_prefix) and not n.endswith("/")]
        nats = [n for n in z.namelist() if n.startswith(nat_prefix) and not n.endswith("/")]
        if not any(n.endswith("Microsoft.NETCore.App.deps.json") for n in libs):
            raise PortError(
                f".NET 运行时包不完整（{rid} {version}）",
                cause=f"{os.path.basename(pack)} 的 {lib_prefix} 下没有 Microsoft.NETCore.App.deps.json。"
                      "该文件是 runtime 包（而非 ref/host 包）的标志，也用于校验框架版本。",
                fix="确认下载的是 microsoft.netcore.app.runtime.<rid>（runtime pack），"
                    "并且 <version> 与游戏 runtimeconfig 的 includedFrameworks 完全一致。",
            )
        os.makedirs(fw, exist_ok=True)
        for n in libs:
            shutil.copy2(_extract_to(z, n), os.path.join(fw, os.path.basename(n)))
        for n in nats:
            base = os.path.basename(n)
            target = os.path.join(root, "host", "fxr", version, "libhostfxr.dylib") if base == "libhostfxr.dylib"\
                else os.path.join(fw, base)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(_extract_to(z, n), target)
            if not base.endswith((".dylib", ".h", ".a")):
                os.chmod(target, 0o755)
    # ---- host 包（apphost 等，libhostfxr 通常已在运行时包里，缺了再补） ----
    if not os.path.exists(fxr):
        host = download_via_mirrors(f"microsoft.netcore.app.host.{rid}", version, nupkg_dir)
        with zipfile.ZipFile(host) as z:
            for n in z.namelist():
                if n.endswith("/libhostfxr.dylib") or n.endswith("/libhostpolicy.dylib"):
                    os.makedirs(os.path.dirname(fxr), exist_ok=True)
                    shutil.copy2(_extract_to(z, n), fxr if "hostfxr" in n else os.path.join(fw, os.path.basename(n)))
    if not os.path.exists(fxr):
        raise PortError("缺少 libhostfxr.dylib", cause=f"{fxr} 不存在",
                        fix="host 包内容异常，请检查网络/镜像返回的文件是否被篡改。")
    ok(f".NET 运行时就绪：{root}")
    return root


def _extract_to(zf, name):
    """把 zip 成员解到临时文件，返回路径。"""
    d = tempfile.mkdtemp(prefix="pvzport.")
    out = os.path.join(d, os.path.basename(name))
    with zf.open(name) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


# --------------------------------------------------------------------------------------
# 数据目录 / deps.json 组装
# --------------------------------------------------------------------------------------

def framework_files(dotnet_root, version):
    fw = os.path.join(dotnet_root, "shared", "Microsoft.NETCore.App", version)
    return {n: os.path.join(fw, n) for n in os.listdir(fw) if os.path.isfile(os.path.join(fw, n))}


def transform_deps_json(deps, rid, fw_files):
    """把 Windows 版 publish 的 deps.json 改造成 macOS 版：
       - runtimeTarget / target 段的 RID 换成目标平台
       - runtimepack 条目的托管/原生清单按 macOS 框架文件重建
       - 其余资产路径拍平成文件名（publish 输出本来就是拍平的）
       - 去掉 Windows 专属原生库条目
    """
    out = json.loads(json.dumps(deps))
    targets = out.get("targets", {})
    libs = out.get("libraries", {})
    rid_target_key = next((k for k in targets if "/" in k), None)
    if rid_target_key is None:
        raise PortError("deps.json 结构异常", cause="找不到带 RID 的 target 段",
                        fix="这不是标准 dotnet publish 的 deps.json，请提供完整的 Godot .NET Windows 导出包。")
    tfm = rid_target_key.split("/")[0]                 # 例如 .NETCoreApp,Version=v9.0
    tgt = targets.pop(rid_target_key)
    ver = next((k.split("/", 1)[1] for k in libs if k.startswith("runtimepack.")), FRAMEWORK_FALLBACK)
    win_natives = set()
    rp_keys = [k for k in tgt if k.startswith("runtimepack.")]
    if rp_keys:
        old_rp = tgt.pop(rp_keys[0])
        win_natives = set(old_rp.get("native", {}))
        old_rp["runtime"] = {n: {"assemblyVersion": "9.0.0.0", "fileVersion": f"{ver}.0"}
                             for n in sorted(fw_files) if n.endswith(".dll")}
        old_rp["native"] = {n: {"fileVersion": f"{ver}.0"}
                            for n in sorted(fw_files) if not n.endswith(".dll")}
        new_rp_key = f"runtimepack.Microsoft.NETCore.App.Runtime.{rid}/{ver}"
        tgt[new_rp_key] = old_rp
        for k in list(libs):
            if k.startswith("runtimepack."):
                libs[new_rp_key] = libs.pop(k)
        for lib, val in tgt.items():
            block = val.get("dependencies")
            if block:
                for dep in list(block):
                    if dep.startswith("runtimepack."):
                        block[new_rp_key] = block.pop(dep)
    for lib, val in tgt.items():
        for key in ("runtime", "native"):
            if key in val:
                val[key] = {os.path.basename(p): v for p, v in val[key].items()}
        if "native" in val:                            # 去掉 Windows 原生库
            val["native"] = {p: v for p, v in val["native"].items() if not p.lower().endswith(".dll")}
    out["runtimeTarget"]["name"] = f"{tfm}/{rid}"
    targets[f"{tfm}/{rid}"] = tgt
    json.dumps(out)
    return out, win_natives


def patch_machine_anycpi(src_path, dst_path):
    data = bytearray(open(src_path, "rb").read())
    mach, _ = pe_machine(data[:0x1000])
    off = struct.unpack("<I", data[0x3C:0x40])[0]
    data[off + 4:off + 6] = struct.pack("<H", 0x14C)
    with open(dst_path, "wb") as fh:
        fh.write(data)
    return mach


def build_data_dir(zip_path, apk_path, found_zip, found_apk, dotnet_root, fw_version,
                   assembly_name, rid, workdir, verbose_flag=False):
    """生成 <app>/Contents/Resources/data_<Assembly>_macos_<arch>"""
    data_dir = os.path.join(workdir, f"data_{assembly_name}_macos_{godot_arch(rid)}")
    shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir, exist_ok=True)
    zf = zipfile.ZipFile(zip_path)
    af = zipfile.ZipFile(apk_path) if apk_path and os.path.exists(apk_path) else None
    fw = framework_files(dotnet_root, fw_version)
    fxr_src = os.path.join(dotnet_root, "host", "fxr", fw_version, "libhostfxr.dylib")

    # 1) 框架本体（拍平）+ 框架清单 + libhostfxr
    for n, src in fw.items():
        shutil.copy2(src, os.path.join(data_dir, n))
    shutil.copy2(fxr_src, os.path.join(data_dir, "libhostfxr.dylib"))
    verbose(verbose_flag, f"框架文件 {len(fw)} 个 + libhostfxr.dylib")

    # 2) 游戏程序集（APK 里 RID 专属那份；机器码改 AnyCPU）+ 依赖清单/运行时配置
    dll_member, src_zf, source = None, None, "Windows 包"
    if af is not None and found_apk.get("android_dlls"):
        cands = [n for n in found_apk["android_dlls"] if not n.lower().endswith("godotsharp.dll")]
        if cands:
            dll_member = max(cands, key=lambda n: af.getinfo(n).file_size)
            src_zf, source = af, "APK"
    if dll_member is None and found_zip.get("publish_dlls"):
        cands = [n for n in found_zip["publish_dlls"] if os.path.basename(n).lower() ==
                 (assembly_name + ".dll").lower()]
        if cands:
            dll_member, src_zf, source = cands[0], zf, "Windows 包"
    if dll_member is None:
        raise PortError(
            "两个包里都找不到游戏程序集",
            cause=f"APK 里有 {len(found_apk.get('android_dlls', []))} 个托管 dll；"
                  f"zip 的 publish 目录里有 {len(found_zip.get('publish_dlls', []))} 个",
            fix="确认 --apk 是同一版本的安卓包（内含 assets/.godot/mono/publish/<arch>/*.dll），"
                "或 --zip 是 Godot .NET 的 Windows 导出（内含 data_*_windows_*/PlantsVsZombies.dll）。",
        )
    tmp_dll = os.path.join(workdir, f"_src_{os.path.basename(dll_member)}")
    open(tmp_dll, "wb").write(read_member(src_zf, dll_member, limit=128 << 20))
    old_machine = patch_machine_anycpi(tmp_dll, os.path.join(data_dir, assembly_name + ".dll"))
    ok(f"程序集 {os.path.basename(dll_member)}（{source}）PE machine "
       f"0x{old_machine:04x} → 0x014c (AnyCPU)")

    if not found_zip.get("deps") or not found_zip.get("runtimeconfig"):
        raise PortError(
            "Windows 包里缺少 .NET 装配信息",
            cause=f"deps.json: {found_zip.get('deps')!r}\nruntimeconfig.json: {found_zip.get('runtimeconfig')!r}",
            fix="标准 Godot .NET Windows 导出应包含 data_<名字>_windows_x86_64/ 目录，内存 "
                "{Assembly}.dll/.deps.json/.runtimeconfig.json。请确认下载的是完整导出包。",
        )
    deps = read_json_member(zf, found_zip["deps"], "deps.json")
    new_deps, win_natives = transform_deps_json(deps, rid, fw)
    with open(os.path.join(data_dir, assembly_name + ".deps.json"), "w") as fh:
        json.dump(new_deps, fh, indent=2)
    runtimeconfig = read_json_member(zf, found_zip["runtimeconfig"], "runtimeconfig.json")
    with open(os.path.join(data_dir, assembly_name + ".runtimeconfig.json"), "w") as fh:
        json.dump(runtimeconfig, fh, indent=2)

    # 3) GodotSharp + 第三方托管依赖（沿用 Windows publish 里的文件，纯 IL 跨平台）
    copied = 0
    for member in found_zip.get("publish_dlls", []):
        base = os.path.basename(member)
        if "/" in member[len(found_zip["publish_dir"]):][1:]:      # 跳过 cs/ja/... 本地化资源
            continue
        if base in fw or base in win_natives or base.startswith("Microsoft.DiaSymReader"):
            continue
        if base in (assembly_name + ".dll",):
            continue
        rel = os.path.join(data_dir, base)
        if os.path.exists(rel):
            continue
        with open(rel, "wb") as fh:
            fh.write(read_member(zf, member, limit=64 << 20))
        copied += 1
    gs_dst = os.path.join(data_dir, "GodotSharp.dll")
    if not os.path.exists(gs_dst):
        member = found_zip.get("godotsharp")
        if not member:
            raise PortError(
                "缺少 GodotSharp.dll",
                cause="Windows 导出的 publish 目录里没有 GodotSharp.dll",
                fix="这不是完整的 Godot .NET 导出包（缺 GodotSharp.dll 无法运行 C#）。请重新下载完整包。",
            )
        with open(gs_dst, "wb") as fh:
            fh.write(read_member(zf, member, limit=64 << 20))
        copied += 1
    ok(f"复制托管依赖 {copied} 个（含 GodotSharp.dll）")

    # 4) 静态自检（这两个是历史上踩过的坑）
    manifest = os.path.join(data_dir, "Microsoft.NETCore.App.deps.json")
    assert os.path.exists(manifest), "框架清单缺失"
    with open(os.path.join(data_dir, assembly_name + ".dll"), "rb") as fh:
        mach, _ = pe_machine(fh.read(0x1000))
    if mach != 0x14C:
        raise PortError("程序集 machine 修补失败", cause=f"仍是 0x{mach:04x}")
    return data_dir


# --------------------------------------------------------------------------------------
# 组装 .app
# --------------------------------------------------------------------------------------

def write_info_plist(path, exe, display_name, bundle_id, version, minos="12.0"):
    plist = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleExecutable": exe,
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": display_name,
        "CFBundleDisplayName": display_name,
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleIconFile": "icon.icns",
        "CFBundleSupportedPlatforms": ["MacOSX"],
        "LSMinimumSystemVersion": minos,
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)


DMG_README = """首次打开说明（macOS）
================================
1. 把「{app_name}」拖到左边的 Applications 文件夹（即"应用程序"）。
2. 第一次启动要在「应用程序」里 **右键点它 → 打开 → 再点"打开"**。
   若提示"Apple 无法验证此 App"，这是因为应用只做了本地(ad-hoc)签名、没有 Apple 公证，
   不是文件损坏。也可以用终端一次性去掉隔离标记：
       xattr -dr com.apple.quarantine "/Applications/{app_name}.app"
3. 运行要求：{arch_note}，macOS 12 及以上。无需安装 .NET/Godot/其他依赖。
4. 存档目录：~/Library/Application Support/Godot/app_userdata/{data_name}/
   （Windows 版存档在 %APPDATA%\\Godot\\app_userdata\\{data_name}\\Csharp，可整目录拷过来继承进度）
"""


def make_dmg(app, out_dmg, volume_name, app_name, arch_note, data_name, fmt="UDZO"):
    """.app → DMG（标准"拖进应用程序"布局：app + Applications 软链 + 首次打开说明）。"""
    if shutil.which("hdiutil") is None:
        raise PortError("缺少 hdiutil", cause="找不到 hdiutil（macOS 自带）",
                        fix="在 macOS 上运行；或改用 zip 分发（不加 --dmg）。")
    staging = out_dmg + ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    dst_app = os.path.join(staging, os.path.basename(app))
    # APFS 上 cp -Rc 是 clonefile，秒级；失败再退回普通拷贝
    if subprocess.run(["cp", "-Rc", app, dst_app], capture_output=True).returncode != 0:
        shutil.copytree(app, dst_app, symlinks=True)
    os.symlink("/Applications", os.path.join(staging, "Applications"))
    with open(os.path.join(staging, "首次打开说明.txt"), "w") as fh:
        fh.write(DMG_README.format(app_name=app_name, arch_note=arch_note, data_name=data_name))
    if os.path.exists(out_dmg):
        os.remove(out_dmg)
    r = subprocess.run(["hdiutil", "create", "-volname", volume_name, "-srcfolder", staging,
                        "-ov", "-format", fmt, "-quiet", out_dmg],
                       capture_output=True, text=True)
    shutil.rmtree(staging, ignore_errors=True)
    if r.returncode != 0 or not os.path.exists(out_dmg):
        raise PortError("DMG 创建失败",
                        cause=(r.stderr or r.stdout or "").strip()[:500] or f"hdiutil 返回 {r.returncode}",
                        fix="换一种格式试试：--dmg-format UDZO（默认，兼容性最好）或 ULMO（更小，需 macOS 12+）。")
    return out_dmg


def adhoc_sign(app):
    exe_dir = os.path.join(app, "Contents", "MacOS")
    for name in os.listdir(exe_dir):
        p = os.path.join(exe_dir, name)
        subprocess.run(["codesign", "--force", "--sign", "-", p],
                       check=False, capture_output=True)
    r = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app],
                       check=False, capture_output=True, text=True)
    v = subprocess.run(["codesign", "--verify", "--strict", app],
                       check=False, capture_output=True, text=True)
    return r.returncode == 0, v.returncode == 0, (v.stderr or r.stderr or "").strip()


# --------------------------------------------------------------------------------------
# 冒烟自测：启动游戏，判读日志
# --------------------------------------------------------------------------------------

def user_log_path(display_name, not_before=None):
    base = os.path.expanduser("~/Library/Application Support/Godot/app_userdata")
    cands = []
    if display_name:
        cands.append(os.path.join(base, display_name, "logs", "godot.log"))
    for root, _dirs, files in os.walk(base):
        if "godot.log" in files:
            cands.append(os.path.join(root, "godot.log"))
    best, best_m = None, -1
    for c in cands:
        if not os.path.exists(c):
            continue
        m = os.path.getmtime(c)
        if not_before and m < not_before - 1:
            continue
        if m > best_m:
            best, best_m = c, m
    return best or (cands[0] if cands else "")


def _read_text(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def smoke_test(app, exe, display_name, cache, seconds=45, env=None):
    """启动刚打好的 .app，只依据"本次运行产生的新输出"判断是否真的跑起来了。

    注意：游戏的 user://logs/godot.log 是**追加**的（历史会话内容会留在里面），
    所以必须按启动前的偏移量只读增量，否则会把上次失败的报错当成这次的（假阴性）。
    判读的三种结局：加载成功 / 命中经典坑（入口点、框架缺失）/ 进程崩了或没 GUI。
    """
    started_at = time.time()
    binary = os.path.join(app, "Contents", "MacOS", exe)
    out_path = os.path.join(cache, "smoke_stdout.log")
    known_log = user_log_path(display_name, not_before=0)
    log_offset = os.path.getsize(known_log) if known_log and os.path.exists(known_log) else 0
    with open(out_path, "wb") as out:
        # --verbose 会额外打印 "Found hostfxr" / ".NET: hostfxr initialized"，便于精确判读
        proc = subprocess.Popen([binary, "--verbose"], stdout=out, stderr=subprocess.STDOUT,
                                cwd=os.path.dirname(os.path.abspath(app)) or ".", env=env)
    seen, new_log, exit_code = "", known_log, None
    try:
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(1.2)
            new_out = _read_text(out_path)                     # 子进程 stdout：天然只有本次
            cand = user_log_path(display_name, not_before=started_at) or known_log
            delta = ""
            if cand and os.path.exists(cand):
                size = os.path.getsize(cand)
                with open(cand, errors="replace") as fh:
                    fh.seek(log_offset if cand == known_log and size >= log_offset else 0)
                    delta = fh.read()
                new_log = cand
            seen = new_out + "\n" + delta
            exit_code = proc.poll()
            if SIGNAL_BAD_ENTRYPOINT in seen or SIGNAL_NO_FRAMEWORK in seen:
                break
            if ".NET: hostfxr initialized" in seen:
                time.sleep(6)          # 再给游戏自己的启动逻辑一点时间
                new_out = _read_text(out_path)
                seen = new_out + "\n" + delta
                break
            if exit_code is not None:
                break
            if ("Godot Engine v" in seen and any(k in seen for k in ("Metal", "Vulkan", "OpenGL"))
                    and time.time() - started_at > 10 and "hostfxr" in seen):
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    v = {"started": "Godot Engine v" in seen, "signals": [], "log": new_log,
         "stdout": out_path, "exit": exit_code, "seconds": round(time.time() - started_at, 1)}
    for sig, key in ((SIGNAL_BAD_ENTRYPOINT, "bad_entrypoint"), (SIGNAL_NO_FRAMEWORK, "no_framework"),
                     ("Found hostfxr", "hostfxr_found"),
                     (".NET: hostfxr initialized", "hostfxr_initialized"),
                     ("GodotPlugins", "godotplugins_loaded")):
        if sig in seen:
            v["signals"].append(key)
    if any(k in seen for k in ("user://", "ModManager", "save", "Loading] loaded")):
        v["signals"].append("game_code_running")
    v["log_excerpt"] = "\n".join(seen.strip().splitlines()[-14:])
    return v


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------

def detect_arch():
    m = platform.machine()
    if sys.platform != "darwin":
        raise PortError(
            "本脚本只能在 macOS 上运行",
            cause=f"当前系统：{sys.platform} / {m}",
            fix="在 macOS 上运行；.app 打包与 ad-hoc 签名都需要 macOS 工具链（codesign/plutil）。"
                "若只想分析包结构，可用 --inspect（该模式跨平台可用）。",
        )
    if m == "arm64":
        return "arm64", "osx-arm64"
    if m == "x86_64":
        return "x86_64", "osx-x64"
    raise PortError("不支持的 CPU 架构", cause=m, fix="需要在 Apple Silicon 或 Intel Mac 上运行。")


def check_tools():
    missing = [t for t in ("codesign", "plutil", "curl") if shutil.which(t) is None]
    if missing:
        raise PortError("缺少系统工具", cause="找不到：" + ", ".join(missing),
                        fix="安装 Xcode Command Line Tools：xcode-select --install")


def guess_name_and_version(zip_path, pck_name):
    ver = None
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", os.path.basename(pck_name))
    if m:
        ver = m.group(1)
    if ver is None:
        m = re.search(r"(\d+\.\d+(?:\.\d+)*)", os.path.basename(zip_path))
        ver = m.group(1) if m else "1.0"
    base = os.path.basename(zip_path)
    base = re.sub(r"\.(zip|ZIP)$", "", base)
    name = re.sub(r"\d+\.\d+(\.\d+)*.*$", "", base).strip(" -_") or "PVZ Hybrid"
    return name, ver


def main():
    ap = argparse.ArgumentParser(
        description="把《植物大战僵尸杂交版》Windows zip + Android APK 打成 macOS .app",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python3 port_pvz_mac.py --inspect --zip 0.29.zip --apk 0.29.APK\n"
               "  python3 port_pvz_mac.py --zip 0.29.zip --apk 0.29.APK\n")
    ap.add_argument("--zip", help="Windows 版导出 zip")
    ap.add_argument("--apk", help="同版本 Android APK（提供 arm64 游戏程序集；缺失时可只靠 zip 兜底）")
    ap.add_argument("--inspect", action="store_true", help="只分析包，不打包")
    ap.add_argument("--out", default=None, help="输出目录（默认：输入文件所在目录）")
    ap.add_argument("--workdir", default=None, help="工作/缓存目录（默认：<输出目录>/.pvzport）")
    ap.add_argument("--name", default=None, help="App 显示名（默认从文件名推断）")
    ap.add_argument("--exe", default="pvzhybrid", help="可执行文件名（ASCII，默认 pvzhybrid）")
    ap.add_argument("--version", default=None, help="App 版本号（默认从包名推断）")
    ap.add_argument("--engine-app", default=None, help="本地已有的 macos_template.app（离线时用）")
    ap.add_argument("--dotnet-root", default=None, help="本地已有的 .NET 运行时根目录（离线时用）")
    ap.add_argument("--universal", action="store_true", help="同时打包 osx-x64 数据目录，支持 Intel Mac")
    ap.add_argument("--dmg-only", default=None, metavar="APP",
                    help="只对已打好的 .app 出 DMG（不重新打包），仍会先跑一次启动自测")
    ap.add_argument("--dmg", action="store_true", help="额外产出 DMG（内含拖到「应用程序」的软链与首次打开说明）")
    ap.add_argument("--dmg-format", default="UDZO", choices=["UDZO", "UDBZ", "ULMO"],
                    help="DMG 压缩：UDZO=zlib(默认,最兼容) / UDBZ=bzip2 / ULMO=lzma(最小,需 macOS 12+)")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    ap.add_argument("--no-smoke", action="store_true", help="跳过启动自测")
    ap.add_argument("--clean", action="store_true", help="先清空工作目录")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not args.zip and not args.apk and not args.dmg_only:
        die(PortError("缺少输入", cause="必须提供 --zip（Windows 导出包）和/或 --apk",
                      fix="python3 port_pvz_mac.py --zip 游戏.zip --apk 游戏.APK"))
    cache = (args.workdir or os.path.join(
        args.out or os.path.dirname(os.path.abspath(args.zip or args.apk or args.dmg_only)), ".pvzport"))
    os.makedirs(cache, exist_ok=True)
    if args.clean:
        shutil.rmtree(cache, ignore_errors=True)
        os.makedirs(cache, exist_ok=True)

    try:
        if args.dmg_only:
            phase("0. 环境检查（--dmg-only）")
            check_tools()
            app = os.path.abspath(args.dmg_only)
            exe_dir = os.path.join(app, "Contents", "MacOS")
            if not os.path.isdir(exe_dir):
                die(PortError("--dmg-only 指向的不是 .app", cause=app,
                              fix="传 .app 目录的路径，例如 --dmg-only '../pvz/植物大战僵尸杂交版.app'"))
            exe = args.exe
            if not os.path.exists(os.path.join(exe_dir, exe)):
                cands = [f for f in os.listdir(exe_dir) if not f.startswith("._")]
                if len(cands) != 1:
                    die(PortError("无法确定可执行文件名", cause=f"{exe_dir} 里是：{cands}",
                                  fix="用 --exe <名字> 指定。"))
                exe = cands[0]
                info(f"自动识别可执行文件：{exe}")
            name = args.name or os.path.basename(app)[:-4]
            arch_note = "Apple Silicon 或 Intel" if args.universal else "Apple Silicon (M 系列)"
            if not args.no_smoke:
                phase("1. 启动自测")
                v = smoke_test(app, exe, name, cache, seconds=45)
                if "bad_entrypoint" in v["signals"] or "no_framework" in v["signals"]:
                    die(PortError("这个 .app 启动自测没通过，先修好再打 DMG",
                                  cause="命中信号：" + ", ".join(v["signals"]),
                                  fix=f"游戏日志：{v['log']}"))
                ok(f"自测通过：{', '.join(v['signals']) or '已启动（无 C# 信号）'}")
            phase("2. 产出 DMG")
            out_dir = args.out or os.path.dirname(app)
            os.makedirs(out_dir, exist_ok=True)
            dmg = os.path.join(out_dir, f"{name}.dmg")
            make_dmg(app, dmg, name, name, arch_note, name, args.dmg_format)
            ok(f"DMG：{dmg}（{os.path.getsize(dmg)/1e9:.2f} GB，{args.dmg_format}）")
            print(f"""

   挂载：open '{dmg}'
   对方：把 app 拖进「应用程序」→ 首次启动右键→打开（未公证）
   要求：{arch_note}，macOS 12+；无需任何额外依赖""")
            return
        phase("0. 环境检查")
        if args.inspect:
            ensure_inflate64(cache)
            for p, k in ((args.zip, "zip"), (args.apk, "apk")):
                if p:
                    print(json.dumps(inspect(p, k), ensure_ascii=False, indent=1))
            return
        ensure_inflate64(cache)
        check_tools()
        arch, rid = detect_arch()
        info(f"主机：macOS / {arch} → 数据目录后缀 {rid}")
        for p, label in ((args.zip, "--zip"), (args.apk, "--apk")):
            if p and not os.path.exists(p):
                die(PortError(f"{label} 文件不存在", cause=p,
                              fix="检查路径；中文/空格路径请加引号。"))
            if p and not zipfile.is_zipfile(p):
                die(PortError(f"{label} 不是有效的 zip", cause=p,
                              fix="请确认下载完整（普通 zip 的中央目录在文件末尾；不完整会打不开）。"))
        if not args.zip:
            die(PortError("目前必须提供 Windows 导出 zip",
                          cause="APK 里只有程序集与资源散文件，没有 pck。",
                          fix="把同版本的 Windows 导出 zip 一起传进来。"))

        phase("1. 识别两个包")
        found_zip = classify(zipfile.ZipFile(args.zip), "zip")
        found_apk = classify(zipfile.ZipFile(args.apk), "apk") if args.apk else {}
        if not found_zip.get("pck"):
            top = sorted({n.split("/")[0] for n in zipfile.ZipFile(args.zip).namelist()})[:8]
            die(PortError("Windows 包里找不到 .pck",
                          cause=f"顶层条目：{top}",
                          fix="--zip 必须是游戏本体导出包（含 植物大战僵尸杂交版发布版X.Y.Z.Csharp.pck）。"))
        pck = pck_header(zipfile.ZipFile(args.zip), found_zip["pck"])
        info(f"pck：{os.path.basename(pck['name'])}  {pck['size']/1e6:.0f} MB，"
             f"引擎 {pck['version']}，目录格式 {pck['format']}")
        if pck["format"] > PACK_FORMAT_SUPPORTED:
            die(PortError("pck 目录格式比本脚本支持的更新",
                          cause=f"pck format={pck['format']} > 支持上限 {PACK_FORMAT_SUPPORTED}",
                          fix="这是新引擎（>4.7）导出的包：需确认 Godot 的数据目录/hostfxr 规则是否变化，"
                              "并同步更新脚本常量 PACK_FORMAT_SUPPORTED / ENGINE_MIN。"))
        if pck["version_tuple"][:2] != (4, 7):
            warn(f"引擎版本 {pck['version']} 与实测验证过的 4.7.x 不同，"
                 f"将按 4.7 的规则尝试（失败时请把日志发我）。")
        if not found_zip.get("deps") or not found_zip.get("runtimeconfig"):
            die(PortError("Windows 包不像 Godot .NET 导出",
                          cause=f"deps.json={found_zip.get('deps')!r} runtimeconfig.json={found_zip.get('runtimeconfig')!r}",
                          fix="需要含 data_<名字>_windows_x86_64/ 的完整导出（不是精简/绿色版）。"))

        # 程序集名 & 框架版本
        assembly_name = "PlantsVsZombies"
        m = re.match(r"([^/]+)\.runtimeconfig\.json$", os.path.basename(found_zip["runtimeconfig"]))
        if m:
            assembly_name = m.group(1)
        runtimeconfig = read_json_member(zipfile.ZipFile(args.zip), found_zip["runtimeconfig"], "runtimeconfig")
        ro = runtimeconfig.get("runtimeOptions", {})
        included = ro.get("includedFrameworks") or []
        fw_version = included[0].get("version") if included else None
        tfm = ro.get("tfm", "net9.0")
        if not fw_version:
            fw_version = FRAMEWORK_FALLBACK
            warn(f"runtimeconfig 没有 includedFrameworks，按 {fw_version} 兜底")
        tfm_dir = "net" + tfm.replace("net", "")
        info(f"程序集名 {assembly_name}，目标 {tfm}，自包含框架 Microsoft.NETCore.App {fw_version}")
        name, version = (args.name or None), (args.version or None)
        guessed_name, guessed_ver = guess_name_and_version(args.zip, pck["name"])
        name = name or guessed_name
        version = version or guessed_ver
        info(f"App 名称：{name}，版本：{version}，可执行文件：{args.exe}")

        phase("2. 准备引擎模板与 .NET 运行时（有缓存则跳过）")
        if args.engine_app:
            engine_app = args.engine_app
            if not template_binary(engine_app):
                die(PortError("--engine-app 无效", cause=f"{engine_app}/Contents/MacOS 下没有可执行文件"))
        else:
            engine_app = fetch_engine_template(cache, pck["version"], offline=args.offline, allow_editor=False)
        if args.dotnet_root:
            dotnet_root = args.dotnet_root
            fw_dir = os.path.join(dotnet_root, "shared", "Microsoft.NETCore.App", fw_version)
            fxr_file = os.path.join(dotnet_root, "host", "fxr", fw_version, "libhostfxr.dylib")
            if not os.path.isdir(fw_dir):
                die(PortError(
                    "--dotnet-root 里没有这个版本的框架目录",
                    cause=f"缺少 {fw_dir}\n（游戏 runtimeconfig 要求 {fw_version}）",
                    fix="要么去掉 --dotnet-root 让脚本自动下载，要么把对应版本的 "
                        "Microsoft.NETCore.App 目录放到上面这个路径。",
                ))
            if not os.path.exists(fxr_file):
                die(PortError("--dotnet-root 里没有 libhostfxr.dylib",
                              cause=f"缺少 {fxr_file}",
                              fix="标准的 <root>/host/fxr/<版本>/libhostfxr.dylib 结构；"
                                  "缺了就用不带 --dotnet-root 的自动下载模式。"))
        else:
            dotnet_root = prepare_dotnet(cache, rid, fw_version, tfm_dir, offline=args.offline)

        phase("3. 组装 .app")
        app = os.path.join(args.out or os.path.dirname(os.path.abspath(args.zip)), f"{name}.app")
        build = os.path.join(cache, "build")
        shutil.rmtree(build, ignore_errors=True)
        macos_dir = os.path.join(build, "Contents", "MacOS")
        res_dir = os.path.join(build, "Contents", "Resources")
        os.makedirs(macos_dir, exist_ok=True)
        os.makedirs(res_dir, exist_ok=True)
        data_dir = build_data_dir(args.zip, args.apk, found_zip, found_apk or {}, dotnet_root,
                                  fw_version, assembly_name, rid, cache, args.verbose)
        shutil.move(data_dir, os.path.join(res_dir, os.path.basename(data_dir)))
        if args.universal:
            other = "osx-x64" if rid == "osx-arm64" else "osx-arm64"
            info(f"--universal：额外准备 {other} 运行时数据目录")
            other_root = prepare_dotnet(cache, other, fw_version, tfm_dir, offline=args.offline)
            odata = build_data_dir(args.zip, args.apk, found_zip, found_apk or {}, other_root,
                                   fw_version, assembly_name, other, cache, args.verbose)
            shutil.move(odata, os.path.join(res_dir, os.path.basename(odata)))
        shutil.copy2(template_binary(engine_app), os.path.join(macos_dir, args.exe))
        os.chmod(os.path.join(macos_dir, args.exe), 0o755)
        pck_dst = os.path.join(res_dir, f"{args.exe}.pck")
        stream_member_to_file(zipfile.ZipFile(args.zip), found_zip["pck"], pck_dst)
        icon = os.path.join(engine_app, "Contents", "Resources", "icon.icns")
        if os.path.exists(icon):
            shutil.copy2(icon, os.path.join(res_dir, "icon.icns"))
        with open(os.path.join(build, "Contents", "PkgInfo"), "w") as fh:
            fh.write("APPL????")
        write_info_plist(os.path.join(build, "Contents", "Info.plist"), args.exe, name,
                         "com.pvzhybrid." + re.sub(r"[^a-z0-9]", "", args.exe.lower()), version)
        ok(f"pck {pck['size']/1e6:.0f} MB 已放入（原始文件，未改动）")

        phase("4. 签名与校验")
        signed, verified, msg = adhoc_sign(build)
        if not (signed and verified):
            warn(f"ad-hoc 签名/校验异常：{msg[:200]}")
            info("不影响本机运行；分发时对方需要 `xattr -dr com.apple.quarantine` 或右键打开。")
        else:
            ok("ad-hoc 签名有效（codesign --verify --strict 通过）")
        shutil.rmtree(app, ignore_errors=True)
        shutil.move(build, app)
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _, fs in os.walk(app) for f in fs)
        ok(f"产出：{app}（{size/1e9:.2f} GB）")

        phase("5. 启动自测")
        if args.no_smoke:
            warn("已跳过（--no-smoke）；建议手动双击验证")
        else:
            v = smoke_test(app, args.exe, name, cache)
            if "bad_entrypoint" in v["signals"]:
                info("日志摘录：\n" + "\n".join("     " + l for l in v["log_excerpt"].splitlines()))
                runtime_ok = "hostfxr_initialized" in v["signals"]
                die(PortError(
                    "引擎起来了，但 .NET 程序集入口点没加载成功（0x80132006 CLR_E_BIND_SYS_ASM_NI_MISSING）",
                    cause=("hostfxr/.NET 运行时**已经初始化成功**（日志里有 'hostfxr initialized'），"
                           "所以问题在程序集本身：PE machine 不是 AnyCPU(0x14C)——"
                           "macOS 上 CoreCLR 会拒绝绑定纯 IL 但带 RID machine 的程序集。"
                           if runtime_ok else
                           "没看到 'hostfxr initialized'，可能是数据目录结构/hostfxr 位置不对，"
                           "或程序集 PE machine 不是 AnyCPU(0x14C)。"),
                    fix=f"逐项核对：\n"
                        f"  1) Contents/Resources/{os.path.basename(data_dir)}/{assembly_name}.dll 的 PE machine 应为 0x14c\n"
                        f"     python3 - <<'P'\n     import struct\n"
                        f"     d=open('<app>/Contents/Resources/{os.path.basename(data_dir)}/{assembly_name}.dll','rb').read(0x1000)\n"
                        f"     pe=struct.unpack('<I',d[0x3c:0x40])[0]; print(hex(struct.unpack('<H',d[pe+4:pe+6])[0]))\n     P\n"
                        f"  2) 引擎二进制与数据目录架构一致（{rid}）\n"
                        f"  3) 游戏日志：{v['log']}",
                ))
            if "no_framework" in v["signals"]:
                die(PortError(
                    "hostfxr 找不到 .NET 框架（0x80008096 FrameworkMissingFailure）",
                    cause="runtimeconfig 里 includedFrameworks 的版本与数据目录里实际框架版本不一致，"
                          "或 framework 目录名/内容不匹配。",
                    fix="确认 runtimeconfig 的版本 == shared/Microsoft.NETCore.App/<版本>，"
                        "并含 Microsoft.NETCore.App.deps.json。",
                ))
            if "hostfxr_initialized" in v["signals"] or "game_code_running" in v["signals"]:
                ok("启动自测通过：引擎 + .NET 运行时 + C# 程序集都加载成功")
                keys = ("hostfxr", ".NET", "ModManager", "save", "Loading", "resource")
                ev = [l.strip() for l in v["log_excerpt"].splitlines()
                      if any(k.lower() in l.lower() for k in keys)][-6:]
                if ev:
                    info("关键证据（stdout/游戏日志）：")
                    for l in ev:
                        info("  " + l[:150])
            elif v["started"]:
                warn(f"引擎启动成功，但 {v['seconds']}s 内没观察到 .NET 初始化日志"
                     "（可能窗口没拿到焦点/日志延迟，或 C# 侧没起来）")
                info(f"stdout: {v['stdout']}\n游戏日志: {v['log']}")
                if v["log_excerpt"]:
                    info("日志尾部：\n" + "\n".join("     " + l for l in v["log_excerpt"].splitlines()))
            else:
                warn(f"自测没拿到引擎输出（进程 exit={v['exit']}）。常见原因："
                     "没有图形会话（ssh/无 GUI）、被 Gatekeeper 拦、或缺少运行库。")
                info(f"stdout: {v['stdout']}\n游戏日志: {v['log']}")
                if v["log_excerpt"]:
                    info("输出尾部：\n" + "\n".join("     " + l for l in v["log_excerpt"].splitlines()))

        dmg_path, dmg_size = None, 0
        if args.dmg:
            phase("6. 产出 DMG")
            arch_note = "Apple Silicon 或 Intel" if args.universal else "Apple Silicon (M 系列)"
            dmg_path = os.path.join(os.path.dirname(app), f"{name}.dmg")
            make_dmg(app, dmg_path, name, name,
                     arch_note, "植物大战僵尸杂交版", args.dmg_format)
            dmg_size = os.path.getsize(dmg_path)
            ok(f"DMG：{dmg_path}（{dmg_size/1e9:.2f} GB，{args.dmg_format}）")
            info("挂载后把 app 拖进 Applications 即可；首次启动请右键→打开（未公证）")

        phase("完成")
        print(f"""
   产物：{app}
   运行：双击，或 open '{app}'
   存档：~/Library/Application Support/Godot/app_userdata/{name}/Csharp
         （Windows 的 %APPDATA%\\Godot\\app_userdata\\{name}\\Csharp 可直接拷贝过来继承进度）
   分发：ditto -c -k --sequesterRsrc --keepParent '{os.path.basename(app)}' '{args.exe}-mac.zip'
         （.app 是目录，必须打包后传；zip 实测约为原体积的 53%）
         要更小：tar -cf - '{os.path.basename(app)}' | zstd -19 -T0 -o '{args.exe}-mac.tar.zst'   # 约 47%
         对方首次打开：右键 → 打开，或 xattr -dr com.apple.quarantine '{args.exe}-mac.zip'
         要零提示分发需要 Apple 开发者账号做 Developer ID 签名 + 公证（本脚本只做 ad-hoc）
   缓存：{cache}（可重复使用；删掉它不影响已产出的 .app）""")
        if dmg_path:
            print(f"   DMG ：{dmg_path}（{dmg_size/1e9:.2f} GB）")
    except PortError as exc:
        die(exc)
    except KeyboardInterrupt:
        print("\n已中断（缓存保留，可直接重跑续传）")
        sys.exit(130)


if __name__ == "__main__":
    main()
