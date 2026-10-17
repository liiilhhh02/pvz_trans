#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳定性自测：对已打好的 .app 反复冷启动 + 长时挂机，统计成功率并抓崩溃报告。

用法：
    python3 stability_test.py --app "../pvz/植物大战僵尸杂交版.app" --runs 12
    python3 stability_test.py --app "..." --runs 5 --long 600      # 额外一次 10 分钟挂机
    python3 stability_test.py --app "..." --runs 5 --no-network    # 额外一次"断网"启动
    python3 stability_test.py --app "..." --runs 1 --no-crashes    # 不统计崩溃报告

判据：每轮启动后若日志里出现游戏自身逻辑的输出（ModManager/存档/资源加载等）即算通过；
      并统计 ~/Library/Logs/DiagnosticReports 里本轮新增的 .ips 崩溃报告。
"""
import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CRASH_DIR = os.path.expanduser("~/Library/Logs/DiagnosticReports")


def load_packager():
    spec = importlib.util.spec_from_file_location("pp", os.path.join(HERE, "port_pvz_mac.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def crash_snapshot():
    return {p: os.path.getmtime(p) for p in glob.glob(os.path.join(CRASH_DIR, "*.ips"))}


def crash_detail(path):
    try:
        raw = open(path, errors="replace").read()
        i = raw.find("\n")
        body = json.loads(raw[i:])
        exc = body.get("exception", {})
        imgs = body.get("usedImages", [])
        sig = f"{exc.get('type', '?')}/{exc.get('signal', '?')}"
        frames = []
        for t in body.get("threads", []):
            if t.get("triggered"):
                for fr in t.get("frames", [])[:3]:
                    idx = fr.get("imageIndex", -1)
                    nm = imgs[idx].get("name", "?") if 0 <= idx < len(imgs) else "?"
                    frames.append(f"{nm}:{str(fr.get('symbol', ''))[:40]}")
        return sig, " <- ".join(frames)
    except Exception as exc:  # 崩溃报告格式变了也不至于让测试脚本挂掉
        return "?", f"(解析失败 {type(exc).__name__})"


def resolve_exe(app, explicit):
    exe_dir = os.path.join(app, "Contents", "MacOS")
    if explicit and os.path.exists(os.path.join(exe_dir, explicit)):
        return explicit
    cands = [f for f in os.listdir(exe_dir) if not f.startswith("._")]
    if len(cands) != 1:
        raise SystemExit(f"无法确定可执行文件（{exe_dir} 里是 {cands}），请用 --exe 指定")
    return cands[0]


def main():
    ap = argparse.ArgumentParser(description="对 .app 做启动/长时间稳定性自测")
    ap.add_argument("--app", required=True, help=".app 路径")
    ap.add_argument("--exe", default="pvzhybrid")
    ap.add_argument("--name", default=None, help="游戏显示名（用来自查 user:// 日志；默认取 .app 名）")
    ap.add_argument("--runs", type=int, default=10, help="冷启动轮数（默认 10）")
    ap.add_argument("--seconds", type=int, default=35, help="单轮最长等待秒数")
    ap.add_argument("--interval", type=float, default=2.0, help="轮次间隔秒数")
    ap.add_argument("--long", type=int, default=0, help="额外做一次 N 秒的挂机长跑（0=不做）")
    ap.add_argument("--no-network", action="store_true", help="额外做一次「断网」启动（代理指向死端口）")
    args = ap.parse_args()

    pack = load_packager()
    app = os.path.abspath(args.app)
    exe = resolve_exe(app, args.exe)
    name = args.name or os.path.basename(app)[:-4]
    cache = os.path.join(tempfile.gettempdir(), "pvz_stability")
    os.makedirs(cache, exist_ok=True)
    print(f"目标：{app}\n可执行：{exe} | 显示名：{name} | 轮数：{args.runs}\n")

    baseline = crash_snapshot()
    results = []

    for i in range(1, args.runs + 1):
        t0 = time.time()
        v = pack.smoke_test(app, exe, name, cache, seconds=args.seconds)
        good = ("game_code_running" in v["signals"]) or ("hostfxr_initialized" in v["signals"])
        results.append(good)
        flag = "OK  " if good else "FAIL"
        print(f"[{i:>2}/{args.runs}] {flag} {time.time()-t0:5.1f}s  signals={v['signals']}")
        if not good:
            for line in v["log_excerpt"].splitlines()[-5:]:
                print("        | " + line.strip()[:140])
        time.sleep(args.interval)

    if args.no_network:
        print("\n-- 断网启动（代理指向 127.0.0.1:9，模拟无外网）--")
        env = dict(os.environ)
        env.update({"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                    "all_proxy": "socks5://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                    "HTTPS_PROXY": "http://127.0.0.1:9"})
        v = pack.smoke_test(app, exe, name, cache, seconds=args.seconds, env=env)
        good = ("game_code_running" in v["signals"]) or ("hostfxr_initialized" in v["signals"])
        results.append(good)
        print(f"[断网] {'OK  ' if good else 'FAIL'} signals={v['signals']}")

    if args.long:
        print(f"\n-- 挂机 {args.long}s --")
        out_path = os.path.join(cache, "stability_long.log")
        binary = os.path.join(app, "Contents", "MacOS", exe)
        with open(out_path, "wb") as out:
            proc = subprocess.Popen([binary], stdout=out, stderr=subprocess.STDOUT,
                                    cwd=os.path.dirname(app) or ".")
            t0, crashed_at, alive = time.time(), None, True
            while time.time() - t0 < args.long:
                time.sleep(10)
                if proc.poll() is not None:
                    alive = False
                    crashed_at = round(time.time() - t0)
                    break
            rss = "-"
            try:
                ps = subprocess.run(["ps", "-o", "rss=", "-p", str(proc.pid)],
                                    capture_output=True, text=True).stdout.strip()
                if ps:
                    rss = f"{int(ps)/1024:.0f} MB"
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        print(f"[长跑] {'存活' if alive else f'第 {crashed_at}s 退出/崩溃'} | 结束前 RSS≈{rss} | 输出：{out_path}")
        results.append(alive)

    fresh = {p: m for p, m in crash_snapshot().items() if p not in baseline}
    print(f"\n通过 {sum(results)}/{len(results)}")
    if fresh:
        print(f"新增崩溃报告 {len(fresh)} 个：")
        for p in sorted(fresh, key=lambda x: fresh[x]):
            sig, frames = crash_detail(p)
            print(f"  - {os.path.basename(p)}  {sig}  {frames}")
    else:
        print("无新增崩溃报告")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
