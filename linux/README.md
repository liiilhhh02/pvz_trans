# Linux x86_64 构建

和 iOS 使用同样的两份输入：原版 IL 程序集和原版 pck。区别是桌面 Linux 允许 JIT，不需要 NativeAOT，CLR 直接运行原版程序集，行为和 Windows 一致。

## 构建

在装了 Godot 4.7.2 mono 的机器上执行（当前构建机是 macOS）：

```bash
cd linux
./build_linux.sh
```

> [!IMPORTANT]
> 脚本需要一个 **Godot 重建工程**（`PVZ_REBUILD`，默认 `~/Downloads/pvz-rebuild`），本仓库不包含它：pck 里的 `.cs` 是空占位，没有源码可导出。只想要成品的话直接用 Releases 里的 Linux 包。

脚本依次做这些事：

1. 如果导出模板里没有 `linux_release.x86_64`，从 `TEMPLATES_TPZ` 解压安装（默认 `$HOME/Downloads/pvz/godot472/templates_full.tpz`）
2. 把 `assets/PlantsVsZombies.dll` 同步到工程，缺 `UseOriginalIl` target 时补上
3. 往 `export_presets.cfg` 注入 Linux 预设（x86_64，`embed_pck=false`）
4. 导出到 `linux/build/linux/`
5. 用原版 pck 覆盖导出的 pck
6. 校验 `data_*_linuxbsd_x86_64/PlantsVsZombies.dll` 的 md5 和原版一致

可用环境变量：`PVZ_REBUILD`、`GODOT_BIN`、`TEMPLATES_TPZ`、`ORIG_PCK`、`OUT`。

## 运行

把整个输出目录拷到 Linux x86_64 机器：

```bash
cd build/linux
./PvzHybrid.x86_64
```

`PvzHybrid.x86_64`、`PvzHybrid.pck`、`data_PlantsVsZombies_linuxbsd_x86_64/` 必须在同一个目录。.NET 运行时已经打包在 `data_*` 里，系统不需要另外安装。

要求：

- x86_64，glibc 2.31 及以上
- X11 或 Wayland，显卡驱动支持 Vulkan 或 OpenGL
- 输出目录约 666MB

打包分发：

```bash
tar czf PvzHybrid-linux-x86_64.tar.gz -C build/linux .
```

## 验证情况

2026-10-18 在 macOS 上完成构建验证：

- `PvzHybrid.x86_64` 是 ELF 64-bit LSB executable，x86-64，GNU/Linux 5.15
- `data_PlantsVsZombies_linuxbsd_x86_64/` 包含 `PlantsVsZombies.dll`、`libcoreclr.so`、`libhostfxr.so`
- 程序集 md5 为 `79a144ada57e6aa6b6643c65baade87c`，与原版一致
- pck 已替换为 479MB 原版

没有 Linux 主机或容器，因此没有做实际启动测试。架构、运行时目录和程序集都检查过了，到 Linux 上直接运行即可。如果启动异常，先看显卡驱动和 glibc 版本。

## 与 iOS 的差异

| 项目 | iOS | Linux |
|---|---|---|
| 运行方式 | NativeAOT，禁止 JIT | CoreCLR，JIT |
| 签名 | 必须，免费证书 7 天 | 不需要 |
| 程序集 | 原版 IL 经 ilc 编译 | 原版 IL 直接运行 |
| 分发 | devicectl 安装 | 拷贝目录或 tar.gz |

## 免责声明

本目录只包含构建脚本。游戏资源与程序集的版权归原作者及权利方所有，使用前请确认自己持有原版。因使用本脚本产生的任何后果由使用者自行承担。
