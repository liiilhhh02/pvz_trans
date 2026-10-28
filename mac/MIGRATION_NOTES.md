# 植物大战僵尸杂交版 ： Windows/Android 导出包 → macOS .app 迁移笔记

> 交付物（都在本目录）：
> - **`port_pvz_mac.py`** ： 一键脚本：输入同版本的 Windows 导出 zip + Android APK，输出可双击的 `.app`（含启动自测、DMG 打包与针对性报错）。
> - **`stability_test.py`** ： 稳定性自测：反复冷启动 + 长时挂机 + 断网启动，统计成功率并抓崩溃报告。
> - **`packinfo.py`** ： 单文件"包体检"工具。
> - 本文 ： 迁移过程中确认的事实、踩过的坑、以及扩展新版本/新平台的方法。
>
> 已实测版本：0.26.1 / 0.28.1 / 0.29 / 0.30（四个版本的架构完全一致，脚本通用）。
>
> 本文写于 macOS 阶段。第 10 节里 iOS / Linux 的结论已按后来的实现更新，具体流程以 [`ios/`](../ios/) 与 [`linux/`](../linux/) 为准。
>
> 常用命令：
> ```bash
> # 打包 .app（全自动下载引擎/.NET 依赖）
> python3 port_pvz_mac.py --zip 0.31.zip --apk 0.31.APK
> # 打包并出 DMG（推荐分发；--dmg-format ULMO 更小、UDZO 更兼容）
> python3 port_pvz_mac.py --zip 0.31.zip --apk 0.31.APK --dmg --dmg-format ULMO
> # 对已打好的 .app 只补一个 DMG（不重新打包，仍会先自测）
> python3 port_pvz_mac.py --dmg-only "../pvz/植物大战僵尸杂交版.app" --out ../pvz/dist
> # 只体检 / 离线复跑 / Intel 也能玩
> python3 port_pvz_mac.py --inspect --zip A.zip --apk B.APK
> python3 port_pvz_mac.py --zip A.zip --apk B.APK --engine-app <模板.app> --dotnet-root <net9root> --offline
> python3 port_pvz_mac.py --zip A.zip --apk B.APK --universal
> # 稳定性自测
> python3 stability_test.py --app "../pvz/植物大战僵尸杂交版.app" --runs 8 --long 600 --no-network
> ```

---

## 1. 架构核对：几个版本是同构的

用 `port_pvz_mac.py --inspect` 逐个包扫出来的结果：

| 项 | 0.26.1 | 0.28.1 | 0.29 | 0.30 |
|---|---|---|---|---|
| 引擎（pck 头 `GDPC` 里写的版本） | 4.7.0 | 4.7.0 | 4.7.0 | 4.7.0 |
| pck 目录格式 / 是否加密 | format 4 / 未加密 | 同 | 同 | 同 |
| pck 条目数 | 19,272 | 25,729 | 26,522 | 27,426 |
| 目标框架 | net9.0 + `includedFrameworks` 9.0.17 | 同 | 同 | 同 |
| 程序集 | `PlantsVsZombies.dll`，machine `0xaa64`，`IlOnly=true`，**ImplMap=0** | 同 | 同 | 同 |
| Windows 侧数据目录 | `data_PlantsVsZombies_windows_x86_64/` | 同 | 同 | 同 |
| APK | 包名 `xvwugame.PlantsVsZombies`，minSdk 24，仅 arm64-v8a | 同 | 同 | 同 |
| 启动补丁 | `杂交版启动补丁.cs.hta` | 同 | 同 | 同 |

结论：**同一个模板、同一个引擎大版本、同一个 .NET 版本、同一套命名**。所以一个脚本能通吃，版本升级只需换两个输入文件。

顺带得到两个"可以放心"的证据：
- Windows 版与 Android 版的 `PlantsVsZombies.dll` **逐字节只差 21 字节**（PE machine、编译时间戳、MVID），说明游戏逻辑里没有平台分支；
- 整个程序集 **0 个 P/Invoke**（`ImplMap=0`、`ModuleRef=[]`），AssemblyRef 里也没有任何 Windows 专用程序集；
- pck 内没有任何 `.dll/.so/.gdextension`（无原生插件），3,104 个 `.cs` 是 **1 字节空占位**（源码不在包里）。

---

## 2. 迁移原理（为什么"接引擎"就能跑）

Godot 的导出产物可以拆成三层，只有第一层和平台绑定：

| 层 | 内容 | 是否跨平台 |
|---|---|---|
| 引擎二进制 | `*.exe` / `libgodot_android.so` / macOS 模板 | 不可用 与平台绑定 |
| 游戏数据 `*.pck` | 场景、贴图（.ctex）、字体、配置 JSON、GDScript 编译产物 | 可用 跨平台（未加密、无平台专属内容） |
| 托管程序集 | `PlantsVsZombies.dll` + 依赖 dll | 可用 纯 IL，跨平台 |

所以移植 = **官方 macOS 引擎 + 原 pck + 自包含 .NET 运行时**，一个字节的游戏数据都不用改。

---

## 3. 引擎在 macOS 上到底去哪儿找东西（读 Godot 4.7 源码得到的确定事实）

| 事实 | 出处 |
|---|---|
| 非编辑器构建里，`hostfxr` 从**数据目录**加载：`<api_assemblies_dir>/libhostfxr.dylib` | `modules/mono/mono_gd/gd_mono.cpp: find_hostfxr()` |
| 数据目录名 = `data_<程序集名>_<platform>_<arch>`，macOS 上先看可执行文件同级、再看 `Contents/Resources/` | `modules/mono/godotsharp_dirs.cpp: _GodotSharpDirs()` |
| 启动流程 = `hostfxr_initialize_for_dotnet_command_line(argv[0]=<数据目录>/<程序集>.dll)`，即**自包含模式**，用程序集旁边的 `.runtimeconfig.json`/`.deps.json` 建 TPA | `gd_mono.cpp: initialize_hostfxr_self_contained()` |
| 入口点 = `"GodotPlugins.Game.Main, <程序集名>"` 的 `InitializeFromGameProject`（`[UnmanagedCallersOnly]`） | `gd_mono.cpp: initialize_hostfxr_and_godot_plugins()` |
| 程序集查找走 `ComponentActivator.LoadAssemblyAndGetFunctionPointer` → `Type.GetType(typeName, resolver, ...)`，出错只把 HResult 返回给上层 | `dotnet/runtime: System.Private.CoreLib/.../ComponentActivator.cs` |
| pck 放在 `Contents/Resources/<可执行文件名>.pck`（与可执行文件同名）即可被自动加载 | `main/main.cpp` 的 pck 探测注释 + 实测 |
| 编辑器构建（TOOLS_ENABLED）走的是另一套（`GodotSharp/Api/<config>` + `res://.godot/mono/temp/bin/Debug/`），所以**不要**用编辑器二进制当运行时 | `godotsharp_dirs.cpp` 的 `#ifdef TOOLS_ENABLED` 分支 |

---

## 4. 唯一的必修补丁：程序集 PE machine 改成 AnyCPU(0x14C)

### 症状
```
ERROR: .NET: Failed to get GodotPlugins initialization function pointer
   at: initialize_hostfxr_and_godot_plugins (modules/mono/mono_gd/gd_mono.cpp:487)
ERROR: Parameter "godot_plugins_initialize" is null.
```
底层 HResult：`0x80132006` = `CLR_E_BIND_SYS_ASM_NI_MISSING`（名字极具误导性，跟"native image"无关）。

### 最小复现（我就是这么定位的）
写了个 ~40 行的 C 宿主直接调 hostfxr（等价于 Godot 的调用序列），替换程序集再跑：

| 传进去的程序集 | `load_assembly_and_get_function_pointer` 返回 |
|---|---|
| Android 版（machine `0xaa64`） | `0x80132006` 不可用 |
| Windows 版（machine `0x8664`） | `0x80132006` 不可用 |
| 同一文件，**只把 PE 头 2 字节改成 `0x14c`** | `0x00000000`，拿到函数指针 可用 |
| 编辑器自带的小程序集（AnyCPU） | `0x00000000` 可用 |

结论：`csc` 会按目标 RID 写死 machine（x64/arm64），而 macOS 上 CoreCLR 对"纯 IL 但带 RID machine"的程序集拒绝绑定；**IL 本身完全无所谓**，改这 2 字节不改变任何代码语义。

### 等价性证据
Windows 与 Android 两份 dll 逐字节只差 21 字节（其中 machine 2 字节、时间戳 4 字节、MVID 16 字节里有重叠），所以改 machine 之后两份在语义上完全等价。

### 两个"看起来像原因但不是"的坑（别再被绕进去）
1. **数据目录缺 `Microsoft.NETCore.App.deps.json`（框架清单）**：在 Godot 的**自包含**（`includedFrameworks`）布局下**不影响运行**：Windows 官方导出目录里就没有这个文件，实测把 macOS 数据目录里的它删掉，游戏照样跑。它只在改成 framework-dependent（`shared/`）布局时才必需。
2. **`hostfxr_initialize_for_runtime_config`（组件模式）不支持自包含 runtimeconfig**，会直接报 `0x80008093 InvalidConfigFile`。别试图用"runtime config 模式"启动来绕开问题；Godot 用的是 command-line 模式。

---

## 5. macOS 数据目录配方（脚本产出的内容）

```
<App>/Contents/
├── MacOS/<exe>                      # 官方 macos_template.app 里的 godot_macos_release.universal（重命名）
├── Resources/<exe>.pck              # 原 Windows pck，原样字节不动
├── Resources/data_<asm>_macos_arm64/
│   ├── Microsoft.NETCore.App.deps.json        # .NET 9.0.17 框架本体（169 托管 + 16 原生，全部拍平）
│   ├── System.Private.CoreLib.dll …           #   ↑ 来自 runtime pack，含框架清单
│   ├── libcoreclr.dylib / libclrjit.dylib / libSystem*.dylib …
│   ├── libhostfxr.dylib                        # Godot 只认这个位置（数据目录根）
│   ├── GodotSharp.dll                          # 4.7.0.0（与编辑器 Api/Release 里的完全同哈希）
│   ├── <asm>.dll                               # 从 APK 取 arm64 版 → PE machine 改 0x14C
│   ├── <asm>.deps.json                         # RID 改 osx-arm64，runtimepack 清单按 macOS 文件集重建
│   ├── <asm>.runtimeconfig.json                 # 原样（includedFrameworks 9.0.17）
│   └── ZLogger.dll / ZString.dll / Utf8StringInterpolation.dll / System.IO.Hashing.dll /
│       Microsoft.Extensions.*.dll / Microsoft.CodeAnalysis(.CSharp).dll …
├── Resources/icon.icns
├── Info.plist / PkgInfo
└── _CodeSignature（ad-hoc）
```

细节规则：
- **托管依赖全部沿用 Windows publish 目录里的文件**：它们是纯 IL，跨平台；判据是"文件名不在 macOS 框架文件集里，也不是 Windows 原生库（`coreclr.dll`/`hostfxr.dll`/`*Native.dll`/`Microsoft.DiaSymReader.Native.amd64.dll` 等）"。
- `deps.json` 改造三步：① RID 段键名换成 `osx-arm64`；② `runtimepack.*` 条目的 `runtime`/`native` 清单换成 macOS 框架文件集（托管用文件名，原生去掉 `.dll`）；③ 其余包资产路径拍平成文件名（publish 输出本来就是拍平的）。
- 所有依赖（含引擎模板、.NET 运行时）都可离线预置：脚本有 `--engine-app` / `--dotnet-root` / `--offline`。
- `--universal` 会再放一份 `data_<asm>_macos_x86_64/`，引擎按运行架构自动选目录，于是 Intel Mac 也能用。

---

## 6. 下载经验（国内网络下这条最省时间）

实测（同一台机器）：

| 通道 | 速度 |
|---|---|
| GitHub release 直连 / 代理 | 20–45 KB/s（限速，且经常连不上） |
| **GitHub 签名 URL + CDN 直连**（用代理取一次 302，再 `--noproxy` 拉） | 45 KB/s ～ **2.2 MB/s**（波动大，但明显更好） |
| SourceForge 镜像 | 5 KB/s（更差） |
| 各类 ghproxy | ≤27 KB/s |
| **华为云 NuGet 镜像** `repo.huaweicloud.com/artifactory/api/nuget/v3/nuget-remote` | **≈10 MB/s** |
| 阿里云镜像（参照） | 10.9 MB/s |

脚本里实现的技巧：
1. **只取需要的那一部分**：导出模板是 1.2 GB 的 `.tpz`（zip），脚本先 range 取尾部读中央目录，再按偏移只抓 `templates/macos.zip`（约 123 MB）；
2. **签名 URL 会过期**（几分钟），所以每 ~4.5 分钟重新取一次 302，并 `-C` 续传；
3. **.NET 运行时用 NuGet 镜像**：`microsoft.netcore.app.runtime.<rid>` + `microsoft.netcore.app.host.<rid>`，版本必须等于游戏 `runtimeconfig.json` 里 `includedFrameworks` 的版本；
4. 全部落在缓存目录（默认 `<输出目录>/.pvzport`），重跑复用，可离线搬运。

---

## 7. 怎么验证（不要只看"能打开"）

1. **静态断言**（脚本已内置）：程序集 machine == `0x14c`、框架文件齐全、`pck` 大小与 zip 内条目一致、`deps.json` 合法 JSON。
2. **启动冒烟**（脚本已内置 `smoke_test`）：拉起 `.app`，同时看子进程 stdout 与游戏自己的 `user://logs/godot.log`，按下面这张表判读：

| 日志特征 | 含义 |
|---|---|
| `Godot Engine v4.7.stable.mono` + `Metal 4.0 - Forward+` | 引擎与渲染器正常（Apple Silicon 走 Metal，S3TC 贴图数组可用） |
| `Found hostfxr: …/data_…/libhostfxr.dylib` + `.NET: hostfxr initialized` | 运行时加载成功 |
| `Failed to get GodotPlugins initialization function pointer` | **machine 补丁没打**（或框架版本不匹配） |
| `hostfxr_initialize_for_dotnet_command_line failed with code: -2147450730` | `0x80008096` 框架找不到：目录里的框架版本 ≠ runtimeconfig 版本 |
| `主存档已保存到 user://Csharp/save.res` / `[ModManager] …` | C# 已经在跑（游戏自身逻辑起来了） |

3. 真正的"移植完成"标准：进主菜单 → 进一关 → 打一场（这条只有人能点，所以脚本跑完会提示手动确认）。

---

## 8. 疑难速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `unzip`/`bsdtar` 解这个 zip 报 `method 9 / deflate64` | 这批包用 deflate64 压缩 | 脚本自动装 `inflate64`；或 `brew install sevenzip` 后手工解 |
| 双击没反应 / 提示"未验证的开发者" | ad-hoc 签名未公证 | 右键 → 打开；或 `xattr -dr com.apple.quarantine <app>` |
| `libhostfxr.dylib` 找不到 | 数据目录名或位置不对 | 必须是 `Contents/Resources/data_<程序集名>_macos_<arch>/libhostfxr.dylib` |
| 顶部 `WARNING: Image format RGBFloat/RGB8 not supported by hardware` | Metal 不支持这些格式 | 正常，Godot 自动转 RGBAFloat/RGBA8；只是显存略增 |
| `[ModManager] 上次 Mod 加载未完成…零 Mod 安全模式` | 上一次是被强杀（比如我自测超时）导致 | 正常退一次就恢复；不影响玩法 |
| 偶发崩溃（`.ips` 里 SIGBUS 在 `FinalizerThread::FinalizeAllObjects`） | 游戏自身对象生命周期竞态，终结器线程回调引擎原生码 | 无源码无法根除；观察是否高频，必要时把 `.ips` 交给我分析 |

---

## 9. 已知残余风险（诚实记录）

- **偶发终结器崩溃**：0.30 上 10 次左右启动中命中过 1 次（主线程正在 GC 分配，终结器线程调用引擎原生函数触发 SIGBUS/保护页错误）。Windows 上大概也存在，只是时序不同。没有源码无法修，只能观察频率。
- **游戏内 Roslyn 模组编译**：游戏带 `ModEditor`（运行时编译 C# 模组）。它需要 .NET 参考程序集/工具链，macOS 上未逐一验证该子功能；主流程不受影响。
- **未做 Apple 公证**：跨机分发需要用户右键打开/去 quarantine；要做到"零提示"需要 Apple 开发者账号做 Developer ID 签名 + 公证。

---

## 10. 扩展

| 目标 | 做法 | 状态 |
|---|---|---|
| Intel Mac | `--universal`（多放一份 `data_<asm>_macos_x86_64/`） | 可用 已实测产出双数据目录的 .app |
| Linux | 同一配方：`linuxbsd` .NET 导出模板 + `microsoft.netcore.app.runtime.linux-x64` + 同一个 machine 补丁 | 已实现，见 [`linux/`](../linux/)；构建与产物已校验，尚未在 Linux 实机启动 |
| Windows | 直接用官方 zip（自带 .NET，无需依赖） | — |
| Android | 直接用 APK（minSdk 24，仅 arm64-v8a） | — |
| **iOS/iPadOS** | 已用 NativeAOT 实现：原版 IL 经 `dotnet publish -r ios-arm64` 交叉编译，Xcode 构建后重签，用免费 Apple ID 装真机。触屏输入由 pck 自带的输入映射处理；模组编辑器因 NativeAOT 无动态代码生成而不可用 | 已实现，见 [`ios/`](../ios/)；iPhone 17 Pro Max 实测通过。iOS 模拟器不支持（Metal image cube arrays） |

新引擎版本（比如 4.8）需要同步的常量：脚本顶部 `PACK_FORMAT_SUPPORTED`（pck `format` 上限）、`ENGINE_MIN`；`--inspect` 会先告诉你新包的实测值，若 `format` 变大且启动自测报同一个入口点错误，再按第 3、4 节重新核对引擎行为即可。

---

## 11. 验证矩阵（本次实测记录）

| 场景 | 命令要点 | 结果 |
|---|---|---|
| 0.30 zip+apk，缓存已就绪 | `--engine-app --dotnet-root` | 可用 自测通过（.78 GB，8 s） |
| 0.29 zip+apk，**预置缓存** | 同上 | 可用 通过（21 s） |
| 0.29 zip+apk，**全自动下载** | 不给缓存（tpz 尾部→macos.zip 123 MB + 华为云 NuGet 包） | 可用 通过（22.7 s，含下载） |
| 0.26.1 zip+apk（最老版本） | 预置缓存 | 可用 通过，pck 550 MB，版本/名称自动识别为 0.26.1 |
| 0.28.1 **只有 zip（无 APK）** | 只给 `--zip` | 可用 通过（回退用 Windows 版程序集并打 machine 补丁） |
| 负向：程序集 machine 还原成 0x8664 | 手动改回去再自测 | 可用 脚本正确报 `bad_entrypoint`，且能指出"运行时已 initialized，问题在程序集" |
| 负向：删掉框架清单 | 手动删 | 可用 仍能跑（→ 证明清单在自包含布局下不是必需项） |
| 负向：输入不是 zip / 包里没有 pck / pck 头不对 | 故意喂错文件 | 可用 分别给出可操作的报错，无堆栈崩溃 |
| 可移植性 | 整包拷到 `/tmp` 再跑 | 可用 无绝对路径依赖 |

### 一个值得记下的假阴性（踩过的坑）
冒烟自测最初**误报失败**：游戏的 `user://logs/godot.log` 是**追加写**的（历史会话内容留在文件里），
我把整个文件读进来判读，于是"上一轮故意打坏时的报错"被当成本次的错误。
修法：启动前记录文件偏移，只读本次新增的增量（脚本里已实现），并优先采信子进程 stdout。
**教训：判读日志一定要按"本次运行产生的增量"，否则会得到随机的假阴性。**

另外，自测时给引擎加了 `--verbose`：日志里出现 `Found hostfxr:` / `.NET: hostfxr initialized` 这两行，
可以把"运行时没起来"和"程序集没起来"精确区分开（前者要查数据目录/hostfxr，后者几乎必然是 machine 补丁）。

### 另一个容易错的细节：数据目录里的架构名不是 RID 后缀
官方导出用的是 `data_{程序集名}_{platform}_{arch}`，其中 arch 取的是**Godot 的架构名**：
Intel 是 `x86_64`（**不是** RID 里的 `x64`），Apple Silicon 是 `arm64`。
证据：GodotTools 的 `ExportPlugin.cs`（`$"data_{GodotSharpDirs.CSharpProjectName}_{platform}_{arch}"`，
`arch` 来自导出预设 features 的 `x86_64`/`arm64`）与运行时的 `godotsharp_dirs.cpp`
（`Engine::get_architecture_name()`）。写错名字的表现是：Intel 机器上 .NET 部分静默失效
（引擎找不到数据目录 → 没有 libhostfxr）。脚本里用 `GODOT_ARCH` 映射表处理。

## 12. 分发与压缩（实测数字）

`.app` 本身是目录，**必须打包后再传**（直接拷目录会丢符号链接/权限）。三种分发形态都实测过：

| 方式 | 命令 | 体积 | 耗时 | 说明 |
|---|---|---|---|---|
| 原生目录 | — | **754 MB** | — | 可直接双击运行 |
| **DMG（首推）** | `python3 port_pvz_mac.py --dmg-only "<app>" --out dist`（内部就是 `hdiutil create -format ULMO -srcfolder <staging>`） | **340 MB**（ULMO/lzma）<br>406 MB（UDZO/zlib） | 94 s<br>20 s | 双击挂载 → 拖进「应用程序」；卷内含 `Applications` 软链 + `首次打开说明.txt`；ULMO 更小（需 macOS 12+，正好是本 app 的下限），UDZO 兼容性最好 |
| zip | `ditto -c -k --sequesterRsrc --keepParent "…app" pvz-mac.zip` | **401 MB** | 14 s | 对方双击即解压；适合网盘/聊天工具；解压后签名仍有效（已实测） |
| tar + zstd | `tar -cf - "…app" \| zstd -19 -T0 -o pvz-mac.tar.zst` | **352 MB** | 33 s | 最小；对方需 `zstd`（`unzstd`） |

DMG 的回路也实测过：挂载 → 卷内 `codesign --verify --strict` 通过 → **直接从只读卷运行**仍是四层信号全通
（引擎 → hostfxr → GodotPlugins → 游戏代码），所以"挂载即玩、拖走也能玩"两边都成立。

> 为什么只能压到一半：754 MB 里 501 MB 是 pck（BC 贴图 + ogg 已是高熵数据），几乎压不动；
> 压缩收益主要来自 170 MB 引擎二进制和 114 MB .NET 程序集。
> 想再小只能改内容（重导贴图/删多语言资源），收益有限且会引入风险，不建议。

首次打开：右键 → 打开，或 `xattr -dr com.apple.quarantine <包>`（ad-hoc 签名未公证）。
签名与打包无关：解压/挂载后的副本上 `codesign --verify --strict` 依然通过（已实测）。

**分发前必须确认的三件事**：① 对方是 Apple Silicon（除非用 `--universal` 出的包）；② 对方 macOS 12+；
③ 用压缩包/DMG 传，不要让中间人用 Windows 工具重新打包（会丢 unix 权限，表现是"已损坏"）。

## 13. 稳定性自测（`stability_test.py`）

```bash
python3 stability_test.py --app "../pvz/植物大战僵尸杂交版.app" --runs 8 --long 600 --no-network
```
判据：每轮启动后日志里出现游戏自身逻辑的输出（`ModManager` / 存档 / 资源加载）即算通过；
并统计 `~/Library/Logs/DiagnosticReports` 里新增的 `.ips` 崩溃报告。实测结果（0.30 交付包）：

| 项目 | 结果 |
|---|---|
| 冷启动 ×8 | **8/8 通过**，每轮 ~7.3 s 内四层信号全通（引擎 → hostfxr → GodotPlugins → 游戏代码） |
| 断网启动（代理指向死端口） | **通过** ： 游戏自身的联网检查失败也不影响进入游戏 |
| 挂机 600 s | **存活**，结束前 RSS ≈ 777 MB，无崩溃报告 |
| 新增崩溃报告 | **0 个** |

结论：**启动链路稳定**（8 次连续冷启动 + 10 分钟挂机，零崩溃）。要如实说明的边界：
- 早先在调试期观察到过 **1 次**偶发崩溃（`.ips` 显示 SIGBUS 在 .NET 终结器线程回调引擎原生码，
  当时正在加载战斗资源）；本次 10 轮连续测试**没有复现**，推测与"反复强杀进程 + 极短时间连续启动"
  这种非正常使用方式有关。真机长时间游玩（点进关卡打几局）仍需人工验证。
- 长跑日志末尾的 `Pages in use exist at exit` / `libc++abi: ... mutex lock failed` 之类是
  **被 SIGTERM 强杀时的收尾噪声**（脚本自测也会强杀），不是运行期故障。
- 强杀过的下一次启动会打一条 `[ModManager] 上次 Mod 加载未完成…零 Mod 安全模式` 警告，正常退出一次即消失。

## 14. 文件清单

交付目录：本目录（`mac/`）

| 文件 | 作用 |
|---|---|
| `port_pvz_mac.py` | 一键打包器（`--inspect` 只分析、`--dmg/--dmg-only` 出 DMG、`--universal` 出 Intel+ARM 通吃包、`--offline` 离线复跑、`--no-smoke` 跳过自测、`--engine-app/--dotnet-root` 指定本地依赖） |
| `stability_test.py` | 稳定性自测（多次冷启动 / 长时挂机 / 断网启动 / 崩溃报告统计） |
| `MIGRATION_NOTES.md` | 本文（迁移经验：原理、必修补丁、数据目录配方、下载技巧、验证判读、分发压缩、稳定性、疑难表、平台扩展） |
| `packinfo.py` | 单文件版"包体检"工具（输出 JSON：引擎版本/pck 头/程序集 machine/依赖清单），排查时比 `--inspect` 更好抓取 |

历史开发脚本（早期手工打包与解析工具，功能已被上面脚本覆盖）不随仓库发布：
`build_app.py`、`extract_d64.py`、`extract_cs.py`、`fetch47.sh`、`fetch_macos_zip.sh`、`pck.py`、`pck_list.py`、`pck_list2.py`。


## 免责声明

本文档与同目录脚本仅用于个人迁移研究。游戏本体、素材、程序集的版权归 EA / PopCap 及杂交版作者所有，仓库不包含这些文件。使用前请确认自己合法持有原版软件，因使用产生的后果由使用者自行承担。
