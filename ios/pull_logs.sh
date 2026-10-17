#!/bin/bash
# 拉取 iOS 设备上的游戏日志与崩溃报告
# 用法: ./pull_logs.sh [设备UDID]
set -euo pipefail

DEVICE="${1:-}"
if [ -z "$DEVICE" ]; then
  DEVICE="$(xcrun devicectl list devices 2>/dev/null \
    | grep -i iphone | grep -iE "connected|available \\(paired\\)" \
    | grep -oE '[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}' | head -1)"
fi
[ -n "$DEVICE" ] || { echo "没有找到 iPhone"; exit 1; }

OUT="$(cd "$(dirname "$0")" && pwd)/logs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT/docs" "$OUT/crash"
echo "==> 设备: $DEVICE"
echo "==> 输出: $OUT"

xcrun devicectl device copy from --device "$DEVICE" \
  --domain-type appDataContainer --domain-identifier com.pvzhybrid.game \
  --source Documents --destination "$OUT/docs" >/dev/null 2>&1 || echo "（应用容器拉取失败）"

xcrun devicectl device copy from --device "$DEVICE" \
  --domain-type systemCrashLogs --source . --destination "$OUT/crash" >/dev/null 2>&1 || true

LOG="$(ls -t "$OUT"/docs/PVZHE_Logs/*.log 2>/dev/null | head -1)"
if [ -n "$LOG" ]; then
  echo "==> 最新启动日志: $LOG"
  echo "---- 错误汇总 ----"
  grep -E "^ERROR" "$LOG" | sed 's/: \(.\{0,110\}\).*/ \1/' | sort | uniq -c | sort -rn | head -10 || true
  echo "---- 尾部 20 行 ----"
  tail -20 "$LOG"
else
  echo "（没有找到 PVZHE_Logs）"
fi

IPS="$(ls -t "$OUT"/crash/out-*.ips 2>/dev/null | head -1)"
[ -n "$IPS" ] && echo "==> 最近崩溃报告: $IPS"
exit 0
