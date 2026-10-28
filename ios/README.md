# iOS 真机安装

把 Windows 版移植到 iPhone 真机。程序集使用原版 IL，导出时用 NativeAOT 编译；资源使用原版 pck。

## 依赖

- Godot 4.7.2 mono 编辑器，默认路径 `$HOME/Downloads/pvz/godot472/editor/Godot_mono.app`，可用 `GODOT_BIN` 覆盖
- .NET SDK 9，默认 `$HOME/.dotnet`
- Xcode，并且已登录 Apple ID（免费账号即可）
- 原版 pck，路径用 `ORIG_PCK` 指定
- `assets/PlantsVsZombies.dll`，从原版包的 `data_PlantsVsZombies_windows_x86_64/` 取

工程本身在 `../pvz-rebuild/project`（Godot 工程、`.sln`、导出预设），用 `PVZ_REBUILD` 指定别的路径。

> [!IMPORTANT]
> 这份 Godot 工程**不包含在本仓库里**（pck 中的 `.cs` 只有 1 字节占位，没有源码）。仓库提供的是脚本和完整的工程改造步骤；要复现构建，需要自备一份由原版导出产物重建的 Godot 工程。

## 使用

```bash
cd ios
./deploy_ios.sh              # 自动选第一台已连接的 iPhone
./deploy_ios.sh <UDID>       # 指定设备
./deploy_ios.sh --renew      # 证书过期后，跳过导出，只重签和安装
```

首次安装后要在手机上信任证书：设置 → 通用 → VPN与设备管理 → Apple Development → 信任。

拉日志和崩溃报告：

```bash
./pull_logs.sh
```

## 实现说明

程序集。`project/PlantsVsZombies.csproj` 里加了两个 MSBuild target，编译完成后把产物替换成 `project/original/PlantsVsZombies.dll`。Godot 导出时会执行 `dotnet publish -r ios-arm64`，NativeAOT 编译的就是这份原版 IL。原版程序集自带当初编译生成的 `ScriptPathAttribute`，所以场景里的 C# 脚本绑定不需要额外处理。

资源。导出生成的 pck 缺素材，脚本在 xcodebuild 之后把原版 479MB pck 复制进 `out.app`。

签名。xcodebuild 给内嵌 framework 的签名 identifier 是 `PlantsVsZombies`，但 framework 的 Info.plist 写的是 `com.pvzhybrid.game.framework.PlantsVsZombies`，安装时会被拒绝。脚本从 `embedded.mobileprovision` 解析证书 SHA1，用 `--identifier com.pvzhybrid.game.framework.PlantsVsZombies` 重签 framework，再带着原 entitlements 重签 app。

几项容易漏的配置：工程必须有 `.sln`，否则 Godot 导出插件会对每个 `.cs` 报 `is a C# file but no solution file exists`，程序集根本不会被打包；iOS 导出要开 `dotnet/embed_build_outputs=true`；导出预设里 `application/export_project_only=true`，ipa 由脚本自己签。

## 证书与续签

免费 Apple ID 的开发证书和描述文件都是 7 天有效期。到期后图标还在，但打开会提示无法验证。

续签时保持设备连接并解锁，执行 `./deploy_ios.sh --renew`。bundle id 不变，覆盖安装视为更新，`Documents/` 里的存档和日志会保留。不要先删除 App。

查当前包的到期时间：

```bash
APP=$HOME/Downloads/pvz-rebuild/build/devderived/Build/Products/Debug-iphoneos/out.app
security cms -D -i "$APP/embedded.mobileprovision" \
  | /usr/libexec/PlistBuddy -c "Print :ExpirationDate"
```

免费账号还有两个限制：同时最多 3 个自签 App，每周最多 10 个 App ID。付费账号的 ad-hoc 描述文件有效期一年，但每台设备的 UDID 要提前注册。

## 验证情况

2026-10-18 在 iPhone 17 Pro Max 上验证：`dotnet publish -r ios-arm64` 返回 0，生成 154MB 的 `PlantsVsZombies.dylib`；AOT 日志里出现的类型来自原版程序集；xcodebuild `BUILD SUCCEEDED`；`devicectl` 安装和启动都成功，游戏能正常进入。

## 已知限制

- 原版代码里的 `Microsoft.CodeAnalysis` 会在运行时编译 mod 脚本，NativeAOT 下没有动态代码生成能力，所以模组编辑器相关功能不可用。基础游戏不受影响。
- ilc 对反射有裁剪告警（`Type.GetFields`、`Activator.CreateInstance` 等）。目前运行路径没触发问题，后续某个功能异常时可以先查这里。
- iOS 模拟器的 Metal 不支持 image cube arrays，Godot 的 Metal 渲染器无法启动，只能真机跑。

## 排错

| 现象 | 处理 |
|---|---|
| `No Account for Team "ABCDE12345"` | 导出会重置 team id，脚本会改成 Xcode 账号里的 Team ID，也可以设 `TEAM_ID` |
| `profile has not been explicitly trusted` | 在手机上信任证书 |
| `Code signing identifier ... does not match` | 重跑脚本，走 framework 重签那一步 |
| 启动闪退 | `./pull_logs.sh` 看 `.ips`。`CODESIGNING / Invalid Page` 是 AOT/JIT 问题；`excessive recursion` 说明跑到了重建程序集而不是原版 |
| 黑屏或缺素材 | 确认 `out.app/out.pck` 是那个 479MB 的原版文件 |

## 免责声明

本目录只包含构建和安装脚本。游戏资源与程序集的版权归原作者及权利方所有，使用前请确认自己持有原版。因使用本脚本产生的任何后果由使用者自行承担。
