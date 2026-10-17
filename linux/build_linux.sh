#!/bin/bash
# =============================================================================
# 植物大战僵尸杂交版 → Linux（x86_64）构建
#
# 与 iOS 相同的核心思路：**原版 IL + 原版 pck**，只是桌面平台允许 JIT，
# 所以不需要 AOT，.NET 直接跑原版程序集。
#
# 用法:
#   ./build_linux.sh
#
# 环境变量（可选）:
#   PVZ_REBUILD     Godot 工程仓库，默认 $HOME/Downloads/pvz-rebuild
#   GODOT_BIN       Godot 4.7.2 mono 编辑器
#   TEMPLATES_TPZ   官方 mono export templates（*.tpz）
#   ORIG_PCK        原版 479MB pck
#   OUT             输出目录，默认 ./build/linux
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${PVZ_REBUILD:-$HOME/Downloads/pvz-rebuild}"
PROJECT="$REPO/project"
OUT="${OUT:-$HERE/build/linux}"
GODOT_BIN="${GODOT_BIN:-$HOME/Downloads/pvz/godot472/editor/Godot_mono.app/Contents/MacOS/Godot}"
TEMPLATES_TPZ="${TEMPLATES_TPZ:-$HOME/Downloads/pvz/godot472/templates_full.tpz}"
ORIG_PCK="${ORIG_PCK:-$HOME/Downloads/pvz/win/植物大战僵尸杂交重制版/植物大战僵尸杂交版发布版0.30.0.Csharp.pck}"
ORIG_DLL="$HERE/assets/PlantsVsZombies.dll"
TPL_DIR="$HOME/Library/Application Support/Godot/export_templates/4.7.2.stable.mono"
DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export DOTNET_ROOT PATH="$DOTNET_ROOT:$PATH"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$GODOT_BIN" ] || die "找不到 Godot: $GODOT_BIN"
[ -d "$PROJECT" ]   || die "找不到工程: $PROJECT（用 PVZ_REBUILD 指定）"
[ -f "$ORIG_PCK" ]  || die "找不到原版 pck: $ORIG_PCK（用 ORIG_PCK 指定）"
[ -f "$ORIG_DLL" ]  || die "缺少 assets/PlantsVsZombies.dll（见 assets/README.md）"

# ---------- 1. Linux 导出模板 ----------
if [ ! -x "$TPL_DIR/linux_release.x86_64" ]; then
  [ -f "$TEMPLATES_TPZ" ] || die "缺少 Linux 模板，且找不到 $TEMPLATES_TPZ"
  say "安装 Linux 导出模板"
  mkdir -p "$TPL_DIR"
  TMP="$(mktemp -d)"
  unzip -o -j "$TEMPLATES_TPZ" "templates/linux_release.x86_64" "templates/linux_debug.x86_64" -d "$TMP" >/dev/null
  cp "$TMP"/* "$TPL_DIR/"
  rm -rf "$TMP"
fi

# ---------- 2. 工程准备 ----------
mkdir -p "$PROJECT/original"
cp "$ORIG_DLL" "$PROJECT/original/PlantsVsZombies.dll"

if ! grep -q "UseOriginalIl" "$PROJECT/PlantsVsZombies.csproj"; then
  say "注入原版 IL target"
  python3 - "$PROJECT/PlantsVsZombies.csproj" "$HERE/../ios/original-il.targets.xml" <<'PY'
import sys
csproj, frag = sys.argv[1], sys.argv[2]
s = open(csproj, encoding='utf-8').read()
s = s.replace('</Project>', open(frag, encoding='utf-8').read() + '\n</Project>')
open(csproj, 'w', encoding='utf-8').write(s)
PY
fi

python3 - "$PROJECT/export_presets.cfg" "$OUT" <<'PY'
import sys
p, out = sys.argv[1], sys.argv[2]
s = open(p, encoding='utf-8').read()
if 'platform="Linux"' not in s:
    s += f'''
[preset.1]

name="Linux"
platform="Linux"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="{out}/PvzHybrid.x86_64"
patches=PackedStringArray()
encryption_include_filters=""
encryption_exclude_filters=""
seed=0
encrypt_pck=false
encrypt_directory=false
script_export_mode=2

[preset.1.options]

custom_template/debug=""
custom_template/release=""
debug/export_console_wrapper=1
binary_format/embed_pck=false
texture_format/s3tc_bptc=true
texture_format/etc2_astc=false
binary_format/architecture="x86_64"
ssh_remote_deploy/enabled=false
dotnet/include_scripts_content=false
dotnet/include_debug_symbols=false
dotnet/embed_build_outputs=false
'''
    open(p, 'w', encoding='utf-8').write(s)
PY

# ---------- 3. 导出 ----------
say "导出 Linux（x86_64）"
rm -rf "$OUT"; mkdir -p "$OUT"
( cd "$PROJECT" && "$GODOT_BIN" --headless --path . --export-release "Linux" "$OUT/PvzHybrid.x86_64" ) \
  > "$OUT/export.log" 2>&1 || true
[ -x "$OUT/PvzHybrid.x86_64" ] || die "导出失败，见 $OUT/export.log"

# ---------- 4. 原版 pck ----------
say "替换为原版 pck"
cp "$ORIG_PCK" "$OUT/PvzHybrid.pck"

# ---------- 5. 校验 ----------
DATADIR="$(find "$OUT" -maxdepth 1 -type d -name 'data_*_linuxbsd_x86_64' | head -1)"
[ -n "$DATADIR" ] || die "找不到 data_*_linuxbsd_x86_64 目录"
BUILT_MD5="$(md5 -q "$DATADIR/PlantsVsZombies.dll" 2>/dev/null || md5sum "$DATADIR/PlantsVsZombies.dll" | awk '{print $1}')"
ORIG_MD5="$(md5 -q "$ORIG_DLL" 2>/dev/null || md5sum "$ORIG_DLL" | awk '{print $1}')"
[ "$BUILT_MD5" = "$ORIG_MD5" ] || die "程序集不是原版 IL（$BUILT_MD5 != $ORIG_MD5）"

say "完成"
file "$OUT/PvzHybrid.x86_64" | sed 's/^/  /'
du -sh "$OUT" | sed 's/^/  体积: /'
cat <<TIP

在 Linux 上运行：
  cd <拷贝到 Linux 的目录>
  ./PvzHybrid.x86_64

要求：x86_64、glibc 2.31+、X11 或 Wayland、支持 Vulkan/OpenGL 的显卡驱动。
整个目录（可执行文件 + PvzHybrid.pck + data_*）必须一起拷贝。
TIP
