#!/bin/bash
# =============================================================================
# 植物大战僵尸杂交版 → iOS 真机部署（原版 IL + NativeAOT）
#
# 用法:
#   ./deploy_ios.sh [--renew] [设备UDID]
#   不传 UDID 时自动取第一台 connected 的 iPhone。
#   --renew  跳过导出，复用上次构建目录，仅重新签名 + 安装（证书 7 天过期后用这个续期）
#
# 环境变量（可选）:
#   PVZ_REBUILD   Godot 工程仓库根目录，默认 $HOME/Downloads/pvz-rebuild
#   GODOT_BIN     Godot 4.7.2 mono 可执行文件
#   ORIG_PCK      原版 479MB pck
#   TEAM_ID       Apple 开发团队 ID（不设则从 Xcode 账号自动读取）
#   DOTNET_ROOT   .NET SDK 9 根目录，默认 ~/.dotnet
#
# 流程: 导出 → 修正 team → xcodebuild（注册设备）→ 换原版 pck → 重签 → 安装 → 启动
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${PVZ_REBUILD:-$HOME/Downloads/pvz-rebuild}"
PROJECT="$REPO/project"
BUILD="$REPO/build/deploy"
DD="$REPO/build/devderived"
ORIG_DLL="$HERE/assets/PlantsVsZombies.dll"
TARGETS_XML="$HERE/original-il.targets.xml"
GODOT_BIN="${GODOT_BIN:-$HOME/Downloads/pvz/godot472/editor/Godot_mono.app/Contents/MacOS/Godot}"
ORIG_PCK="${ORIG_PCK:-$HOME/Downloads/pvz/win/植物大战僵尸杂交重制版/植物大战僵尸杂交版发布版0.30.0.Csharp.pck}"
if [ -z "${TEAM_ID:-}" ]; then
  TEAM_ID="$(defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier 2>/dev/null \
    | grep -oE 'teamID = [A-Z0-9]+' | head -1 | awk '{print $3}')"
fi
TEAM_ID="${TEAM_ID:?请设置 TEAM_ID，或在 Xcode 中登录 Apple ID 后重试}"
BUNDLE_ID="com.pvzhybrid.game"
DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export DOTNET_ROOT
export PATH="$DOTNET_ROOT:$PATH"

# 防并发：同一时间只允许一个部署（共用 build 目录）
mkdir -p "$REPO/build"
LOCK="$REPO/build/.deploy.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "已有另一个部署在进行（锁: $LOCK）。等它结束或删除该目录后重试。"
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 参数 ----------
RENEW=0
if [ "${1:-}" = "--renew" ]; then RENEW=1; shift; fi
DEVICE="${1:-}"
if [ -z "$DEVICE" ]; then
  DEVICE="$(xcrun devicectl list devices 2>/dev/null \
    | grep -i iphone | grep -i connected \
    | grep -oE '[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}' | head -1)"
fi
[ -n "$DEVICE" ] || die "没有找到已连接（connected）的 iPhone；请用数据线连接并解锁，或显式传入 UDID"
say "设备: $DEVICE"

# ---------- 前置检查 ----------
[ -x "$GODOT_BIN" ] || die "找不到 Godot: $GODOT_BIN"
[ -d "$PROJECT" ]   || die "找不到工程目录: $PROJECT（用 PVZ_REBUILD 指定）"
[ -f "$ORIG_DLL" ]  || die "缺少 assets/PlantsVsZombies.dll"
[ -f "$ORIG_PCK" ]  || die "找不到原版 pck: $ORIG_PCK（用 ORIG_PCK 指定）"

# 原版 IL 同步到工程（csproj target 从这里取）
mkdir -p "$PROJECT/original"
cp "$ORIG_DLL" "$PROJECT/original/PlantsVsZombies.dll"

# 确保 csproj 已注入 UseOriginalIl target
if ! grep -q "UseOriginalIl" "$PROJECT/PlantsVsZombies.csproj"; then
  say "注入原版 IL target 到 csproj"
  python3 - "$PROJECT/PlantsVsZombies.csproj" "$TARGETS_XML" <<'PY'
import sys
csproj, targets = sys.argv[1], sys.argv[2]
s = open(csproj, encoding='utf-8').read()
frag = open(targets, encoding='utf-8').read()
s = s.replace('</Project>', frag + '\n</Project>')
open(csproj, 'w', encoding='utf-8').write(s)
PY
fi

# ---------- 1. 导出 ----------
if [ "$RENEW" -eq 1 ]; then
  say "续签模式：复用上次导出（跳过导出）"
  [ -d "$BUILD/out.xcodeproj" ] || die "没有可复用的构建，请先正常跑一次 ./deploy_ios.sh"
else
  say "导出 iOS 工程"
  rm -rf "$BUILD"; mkdir -p "$BUILD"
  ( cd "$PROJECT" && "$GODOT_BIN" --headless --path . --export-debug "iOS" "$BUILD/out.ipa" ) \
    > "$BUILD/export.log" 2>&1 || true
  [ -d "$BUILD/out.xcodeproj" ] || die "导出失败，见 $BUILD/export.log"
fi

# ---------- 2. 签名配置 ----------
cd "$BUILD"
python3 - "$TEAM_ID" <<'PY'
import sys
team = sys.argv[1]
p = 'out.xcodeproj/project.pbxproj'
s = open(p, encoding='utf-8', errors='replace').read()
s = s.replace('DEVELOPMENT_TEAM = ABCDE12345;', f'DEVELOPMENT_TEAM = {team};')
s = s.replace('CODE_SIGN_STYLE = Manual;', 'CODE_SIGN_STYLE = Automatic;')
open(p, 'w', encoding='utf-8').write(s)
PY

say "xcodebuild（自动把当前设备注册进描述文件）"
/usr/bin/xcodebuild -project out.xcodeproj -scheme out -configuration Debug \
  -destination "id=$DEVICE" -allowProvisioningUpdates -derivedDataPath "$DD" build 2>&1 \
  | grep -E "BUILD|error:" | head -5
APP="$DD/Build/Products/Debug-iphoneos/out.app"
[ -d "$APP" ] || die "构建产物不存在: $APP"

# ---------- 3. 原版 pck ----------
say "替换为原版 pck"
cp "$ORIG_PCK" "$APP/out.pck"

# ---------- 4. 重签 ----------
security cms -D -i "$APP/embedded.mobileprovision" > /tmp/pvz_profile.plist
CERT="$(python3 - <<'PY'
import plistlib, hashlib
p = plistlib.load(open('/tmp/pvz_profile.plist', 'rb'))
print(hashlib.sha1(p['DeveloperCertificates'][0]).hexdigest().upper())
PY
)"
say "签名证书: $CERT"
codesign -d --entitlements :- "$APP/out" > /tmp/pvz_ent.plist 2>/dev/null || true
codesign -f -s "$CERT" --timestamp=none \
  --identifier "$BUNDLE_ID.framework.PlantsVsZombies" \
  "$APP/Frameworks/PlantsVsZombies.framework"
codesign -f -s "$CERT" --timestamp=none --generate-entitlement-der \
  --entitlements /tmp/pvz_ent.plist "$APP"

# ---------- 5. 安装 + 启动 ----------
say "安装"
xcrun devicectl device install app --device "$DEVICE" "$APP" 2>&1 \
  | grep -E "bundleID|installed|Failed|error" || true
say "启动"
xcrun devicectl device process launch --device "$DEVICE" --terminate-existing "$BUNDLE_ID" 2>&1 | tail -2

cat <<'TIP'

完成。
- 首次安装需在手机信任证书：设置 → 通用 → VPN与设备管理 → Apple Development → 信任
- 免费证书 7 天过期，重跑本脚本即可续期
- 拉日志：./pull_logs.sh
TIP
