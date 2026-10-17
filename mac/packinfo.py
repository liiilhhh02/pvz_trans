#!/usr/bin/env python3
"""Inspect a Godot .NET export package (Windows export zip and/or Android APK).

Stdlib + optional `inflate64` (needed for deflate64 / method 9 entries).
"""
import json, os, re, struct, sys, zipfile

try:
    import inflate64
except ImportError:
    inflate64 = None


def _raw_member_bytes(zf, name, max_bytes=None):
    """Read the raw (still compressed) bytes of a member straight from the file."""
    info = zf.getinfo(name)
    fp = zf.fp
    fp.seek(info.header_offset)
    hdr = fp.read(30)
    if len(hdr) < 30:
        raise RuntimeError(f"truncated local header for {name}")
    sig, ver, flg, method, mt, md, crc, csz, usz, nlen, elen = struct.unpack("<IHHHHHIIIHH", hdr)
    if sig != 0x04034B50:
        raise RuntimeError(f"bad local header signature for {name}")
    fp.seek(info.header_offset + 30 + nlen + elen)
    size = info.compress_size if max_bytes is None else min(info.compress_size, max_bytes)
    return fp.read(size)


def _inflate64_stream(raw):
    d = inflate64.Inflater()
    return d.inflate(raw), d.eof


def read_member_head(zf, name, nbytes):
    """First nbytes of a zip member; supports store / deflate / deflate64."""
    info = zf.getinfo(name)
    if info.compress_type in (0, 8):
        with zf.open(name) as fh:
            return fh.read(nbytes)
    if info.compress_type == 9:
        if inflate64 is None:
            raise RuntimeError("deflate64 (method 9) entry needs the 'inflate64' pip package")
        out = b""
        want = max(nbytes * 4, 1 << 16)
        while len(out) < nbytes:
            raw = _raw_member_bytes(zf, name, max_bytes=want)
            if not raw:
                break
            out, _ = _inflate64_stream(raw)
            want *= 4
        return out[:nbytes]
    raise RuntimeError(f"unsupported compression method {info.compress_type} ({name})")


def read_member(zf, name, limit=64 << 20):
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise RuntimeError(f"{name} is {info.file_size} bytes (> limit {limit})")
    if info.compress_type in (0, 8):
        with zf.open(name) as fh:
            return fh.read()
    if info.compress_type == 9:
        if inflate64 is None:
            raise RuntimeError("deflate64 (method 9) entry needs the 'inflate64' pip package")
        raw = _raw_member_bytes(zf, name)
        out, _ = _inflate64_stream(raw)
        return out
    raise RuntimeError(f"unsupported compression method {info.compress_type} ({name})")


def parse_pck_header(data):
    magic = data[:4]
    fmt, maj, mi, pat, flags = struct.unpack("<5I", data[4:24])
    base, dir_off = struct.unpack("<2Q", data[24:40])
    return {
        "magic": magic.decode("latin1"),
        "format": fmt, "engine": f"{maj}.{mi}.{pat}", "flags": flags,
        "dir_encrypted": bool(flags & 0x80000000) or bool(flags & 1),
        "file_base": base, "dir_offset": dir_off,
    }


def analyse_pck(zf, name):
    head = read_member_head(zf, name, 40)
    info = parse_pck_header(head)
    size = zf.getinfo(name).file_size
    dir_bytes = size - info["dir_offset"]
    info["pck_size"] = size
    info["dir_bytes"] = dir_bytes
    info["entry_count"] = struct.unpack("<I", read_member_head(zf, name, info["dir_offset"] + 4)[info["dir_offset"]:info["dir_offset"] + 4])[0]
    return info


def walk_zip(zf):
    """Classify entries of a Windows export zip / APK."""
    names = zf.namelist()
    out = {"entries": len(names), "pck": None, "exe": None, "publish_dir": None,
           "deps": None, "runtimeconfig": None, "assembly_dlls": [], "hta": None,
           "ndata": None, "android_libs": [], "sparsepck": None}
    for n in names:
        low = n.lower()
        if low.endswith(".pck"):
            out["pck"] = n
        elif low.endswith(".exe"):
            out["exe"] = n
        elif low.endswith(".hta"):
            out["hta"] = n
        elif re.search(r"\.cs\.hta$", n):
            out["hta"] = n
        elif re.search(r"/(data_[^/]+)/?$", n):
            out["publish_dir"] = n.rstrip("/")
        elif low.endswith(".deps.json"):
            out["deps"] = n
        elif low.endswith(".runtimeconfig.json"):
            out["runtimeconfig"] = n
        elif "godotsharp.dll" in low:
            out["assembly_dlls"].append(n)
        elif re.search(r"\.godot/mono/publish/.*\.dll$", n):
            out["assembly_dlls"].append(n)
        elif re.search(r"/(data_[^/]+)/[^/]+\.dll$", n):
            out.setdefault("publish_dlls", []).append(n)
        elif re.search(r"^lib/.*\.so$", n):
            out["android_libs"].append(n)
        elif low.endswith("sparsepck"):
            out["sparsepck"] = n
    return out


def analyse(path):
    zf = zipfile.ZipFile(path)
    kind = "APK" if path.lower().endswith(".apk") else "ZIP"
    rep = {"path": path, "kind": kind, "size_mb": round(os.path.getsize(path) / 1e6, 1)}
    if kind == "APK":
        try:
            from pyaxmlparser import APK
            a = APK(path)
            rep["apk"] = {"package": a.package, "versionName": a.version_name,
                          "minSdk": a.get_min_sdk_version(), "targetSdk": a.get_target_sdk_version()}
        except Exception as e:
            rep["apk"] = {"error": str(e)}
    info = walk_zip(zf)
    rep.update({k: v for k, v in info.items() if v is not None})
    if info.get("publish_dlls"):
        rep["publish_dlls"] = [os.path.basename(x) for x in info["publish_dlls"]]
    if info["pck"]:
        try:
            rep["pck_header"] = analyse_pck(zf, info["pck"])
        except Exception as e:
            rep["pck_header"] = {"error": f"{type(e).__name__}: {e}"}
    if info["runtimeconfig"]:
        d = json.loads(read_member(zf, info["runtimeconfig"]))
        ro = d.get("runtimeOptions", {})
        rep["runtimeconfig"] = {"tfm": ro.get("tfm"), "framework": ro.get("framework"),
                                 "includedFrameworks": ro.get("includedFrameworks")}
    if info["deps"]:
        d = json.loads(read_member(zf, info["deps"]))
        rep["deps"] = {"targets": list(d.get("targets", {})),
                       "runtimeTarget": d.get("runtimeTarget", {}).get("name"),
                       "libraries": len(d.get("libraries", {}))}
        for t in d.get("targets", {}):
            for lib, val in d["targets"][t].items():
                if val.get("runtime", {}).keys() and lib.split("/")[0].lower() not in ("godotsharp",):
                    pass
        rep["deps"]["runtimepack"] = [k for k in d.get("libraries", {}) if k.startswith("runtimepack")]
    # assembly details from the android publish dir (preferred: RID-specific build)
    cands = [n for n in info["assembly_dlls"] if "/mono/publish/" in n and n.endswith(".dll")
             and "godotsharp" not in n.lower()]
    if not cands:
        cands = [n for n in info["assembly_dlls"] if n.lower().endswith(".dll")
                 and "godotsharp" not in n.lower()]
    if cands:
        name = sorted(cands, key=lambda x: -zf.getinfo(x).file_size)[0]
        try:
            data = read_member(zf, name, limit=64 << 20)
            rep["assembly"] = analyse_assembly(data, name)
        except Exception as e:
            rep["assembly"] = {"entry": name, "error": f"{type(e).__name__}: {e}"}
    return rep


def analyse_assembly(data, entry):
    import dnfile
    pe = dnfile.dnPE(data=data)
    flags = pe.net.Flags
    mrefs = 0 if pe.net.mdtables.ModuleRef is None else len(pe.net.mdtables.ModuleRef.rows)
    impl = 0 if pe.net.mdtables.ImplMap is None else len(pe.net.mdtables.ImplMap.rows)
    refs = {str(r.Name): f"{r.MajorVersion}.{r.MinorVersion}" for r in pe.net.mdtables.AssemblyRef.rows}
    return {"entry": entry, "size": len(data),
            "machine": hex(pe.FILE_HEADER.Machine),
            "ilonly": bool(getattr(flags, "CLR_ILONLY", False)),
            "has_ready_to_run": bool(getattr(pe, "net", None) and any(
                d.name == "IMAGE_DIRECTORY_ENTRY_MANAGED_NATIVE_HEADER" and d.Size
                for d in pe.OPTIONAL_HEADER.DATA_DIRECTORY)),
            "implmap": impl, "moduleref": mrefs,
            "godotsharp_ref": refs.get("GodotSharp"),
            "refs": len(refs)}


if __name__ == "__main__":
    for p in sys.argv[1:]:
        try:
            print(json.dumps(analyse(p), ensure_ascii=False, indent=1))
        except Exception as e:
            print(json.dumps({"path": p, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
