# pvz-transformer

《植物大战僵尸杂交版 0.30》（Godot 4.7 Mono）的跨平台迁移脚本，目标平台是 macOS、iOS 和 Linux。

仓库不包含游戏本体。pck、程序集和素材需要自备，见下文「需要自备的文件」。

## 下载

预编译包在 Releases 页面：

https://github.com/liiilhhh02/pvz_trans/releases/tag/v0.30

- macOS：`PvzHybrid-macOS-universal-v0.30.dmg`，Intel 与 Apple Silicon 通用，未公证，首次打开需右键 → 打开
- Linux x86_64：`PvzHybrid-linux-x86_64-v0.30.zip`，解压后运行 `PvzHybrid.x86_64`
- Windows / Android：原版导出包与 APK
- iOS：不提供预编译包，需要用你自己的 Apple ID 重签，流程见 `ios/`

## 目录

- `mac/`：早期 macOS 迁移记录和脚本
- `ios/`：iPhone 真机安装，原版 IL + NativeAOT
- `linux/`：Linux x86_64 构建，原版 IL + CoreCLR

## 思路

原版 `PlantsVsZombies.dll` 是托管 IL，没有 Windows 专有 API 调用，所以不需要反编译重写。

桌面平台允许 JIT，CLR 直接运行这份 IL；iOS 不允许 JIT，用 NativeAOT 把它编译成本地代码。

资源统一用原版 479MB 的 pck。工程里只剩 `.import`、没有源图，自己导出的 pck 只有 61MB 左右，进游戏会黑屏。

## 快速开始

iOS（需要 Godot 4.7.2 mono、.NET SDK 9、Xcode 已登录 Apple ID）：

```bash
cd ios
./deploy_ios.sh            # 首次完整构建，2-3 分钟
./deploy_ios.sh --renew    # 免费证书 7 天过期后续签，约 30 秒
```

Linux（同样的 Godot 和 .NET，Linux 导出模板脚本会自动安装）：

```bash
cd linux
./build_linux.sh
# 把 build/linux 整个目录拷到 Linux，执行 ./PvzHybrid.x86_64
```

macOS 见 `mac/MIGRATION_NOTES.md`。

## 需要自备的文件

从原版 Windows 发布包中取：

```
植物大战僵尸杂交版发布版0.30.0.Csharp.pck
data_PlantsVsZombies_windows_x86_64/PlantsVsZombies.dll
```

DLL 放到 `ios/assets/` 和 `linux/assets/`，两处可以是同一个文件。pck 的位置用环境变量 `ORIG_PCK` 指定，脚本里有默认路径。

## 免责声明

- 本仓库只包含迁移脚本和文档，不包含、也不分发游戏本体、素材、程序集或 pck。
- 《植物大战僵尸》的名称、美术、音频等权利归 EA / PopCap 所有；杂交版的代码与内容归其作者所有。
- 使用前请确认你已合法持有原版软件。编译、安装、分发的行为及其后果由使用者自行承担。
- 作者不对任何数据丢失、设备损坏、账号封禁、证书吊销或法律纠纷负责。
- 请勿将本仓库用于商业用途，也不要公开分发游戏资源。
