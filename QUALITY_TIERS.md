# 分级画质方案（Quality Tiers）

> 本文件定义 `MC2UE5` 面向不同硬件的五档画质，以及每档的确切开关。
> 目标：同一份资产在集成显卡上能跑、在高端独显上能开到最好。

---

## 1. 设计原则

三条硬约束，决定后面所有取舍：

1. **一份资产，多档运行。** 不为低配单独出一套产物。分级只改「运行期怎么渲染」，
   不改「磁盘上有什么」。这样低配机和高配机打开的是同一个 `.umap`。
2. **温和降级优先保观感。** 用户明确选择：低配档不砍几何、不砍地形，
   优先降阴影 / 后处理 / 剔除距离 / 分辨率。方块世界的观感主要来自
   几何轮廓和亮度层次，砍掉这两样比砍特效更伤观感。
3. **Nanite 在低配档保留。** 用户明确要求。本项目的 HISM 几何是 12 面单位立方体，
   Nanite 对它收益本就有限（<50 万面量级开 Nanite 可能反而掉帧），
   开关本身不是主要开销。真正的开销在**像素预算与流式内存**，所以低配档
   保留 `r.Nanite=1`，改用 `r.Nanite.MaxPixelsPerEdge`（4 → 1）和
   `r.Nanite.Streaming.StreamingPoolSize`（128 → 1024）分档——
   这两个才是真正吃帧时和显存的东西，性能从
   **Landscape LOD / 剔除距离 / 阴影 / 后处理** 拿。

---

## 2. 五档定义

| 档位 | 索引 | 目标硬件 | 目标帧率 |
|---|---|---|---|
| **Low** | 0 | Intel UHD 620 / Vega 8 等集成显卡，8 GB 内存 | 30 fps @ 720p |
| **Medium** | 1 | GTX 1650 / MX550 等入门独显，16 GB | 45 fps @ 1080p |
| **High** | 2 | RTX 3060 / RX 6600 等主流独显 | 60 fps @ 1080p |
| **Epic** | 3 | RTX 4070 / RX 7800 以上 | 60+ fps @ 1440p |
| **Cinematic** | 4 | RTX 4080/4090，离线截图与巡览 | 画质优先，不限帧率 |

`sg.*` 是 UE5 的可扩展性组（Scalability Group），取值 0-4。

> **顶层档的段名是 `@Cine`，不是 `@4`。** 引擎的
> `GetScalabilitySectionString`（`Scalability.cpp:273`）在请求等级等于
> 最大等级时拼出 `"<Group>@Cine"`，否则才是 `"<Group>@<level>"`。
> 所以写 `[ViewDistanceQuality@4]` 的段引擎**永远不会打开**——
> 顶层档静默失效，而且日志里什么都不说。本项目所有组都补齐了 `@Cine` 段，
> `tests/test_quality_tiers.py` 有一条断言专门守这个。

`sg.ResolutionQuality` 不遵循 0-4 等级：它直接是屏幕百分比，
并被 `Scalability::MaxResolutionScale == 100`（`Scalability.h:185`）钳制。

---

## 3. 各档开关矩阵

本表由 `apply_quality.py::TIERS` 与 `DefaultScalability.ini` 交叉核对，
两处数值必须一致（`tests/test_quality_tiers.py` 强制）。

| 设置 | Low | Medium | High | Epic | Cinematic |
|---|---|---|---|---|---|
| `sg.ViewDistanceQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.AntiAliasingQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.ShadowQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.GlobalIlluminationQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.ReflectionQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.PostProcessQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.TextureQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.EffectsQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.FoliageQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.ShadingQuality` | 0 | 1 | 2 | 3 | 4 |
| `sg.ResolutionQuality` | 50 | 71 | 100 | 100 | 100 |
| `r.Nanite` | 1 | 1 | 1 | 1 | 1 |
| `r.ViewDistanceScale` | 0.4 | 0.7 | 1.0 | 1.0 | 1.2 |
| `r.Streaming.PoolSize` (MB) | 256 | 512 | 1024 | 2048 | 4096 |
| `foliage.DensityScale` | 0.0 | 0.4 | 0.8 | 1.0 | 1.0 |
| `grass.DensityScale` | 0.0 | 0.4 | 0.8 | 1.0 | 1.0 |
| `r.Nanite.MaxPixelsPerEdge` | 4 | 2 | 1 | 1 | 1 |
| `r.Nanite.Streaming.PoolSize` | 128 | 256 | 512 | 512 | 1024 |
| `r.LandscapeLOD0DistributionScale` | 1.8 | 1.4 | 1.0 | 1.0 | 0.85 |
| `r.LandscapeLODDistributionScale` | 1.4 | 1.2 | 1.0 | 1.0 | 0.9 |
| `r.Shadow.Virtual.Enable` | 0 | 0 | 1 | 1 | 1 |
| Lumen | 关 | 关 | 开 | 开 | 开 |

四点值得单独说明：

- **`foliage.DensityScale` 在 Low 档是 0.0，不是 0.3。** 校园地面层是
  HISM 方块场，低配档的目标是「弱机也能稳帧」；而方块层才是「地图感」的
  来源，草丛是最便宜的视觉损失项。
- **Nanite 全档保留，但每档调 `MaxPixelsPerEdge` 与流式池。** Nanite 的
  真实开销是像素预算和流式内存，不是开关本身；开关一关，改动的是资产管线
  而不是帧时。
- **Landscape 那两行是反向的：数字越小越精细。** 它们在 LOD 衰减公式的
  分母上，详见 §4.2。这是全表唯一一处「高档位数值更小」的设置。
- **分辨率封顶 100。** 见下。

> `sg.ResolutionQuality` 不是 0-4 等级，它直接是屏幕百分比，并被引擎在
> 100 处钳制。顶层档因此与其他高档同为 100——**这不是漏配，是天花板**。
> 要超采样必须走 `apply_quality.set_supersampling()`，它刻意独立于档位。

---

## 4. 项目级额外设置（`DefaultScalability.ini`）

除 `sg.*` 之外，以下几项对本项目收益最大。**每一项都必须写全五档**——
Intel 的引擎优化指南称之为 "CVar Leaking"：只在部分档位里设置的 cvar，
会在玩家切档时留下上一次档位的值，行为无法预测。

### 4.1 视距与 Nanite 预算

```ini
[ViewDistanceQuality@0]
r.SkeletalMeshLODBias=2
r.ViewDistanceScale=0.4
r.Nanite.MaxPixelsPerEdge=4
r.Nanite.Streaming.StreamingPoolSize=128

; …@1 / @2 / @3 依次为 0.7 / 1.0 / 1.0，MaxPixelsPerEdge 2/1/1，池 256/512/512

[ViewDistanceQuality@Cine]
r.ViewDistanceScale=1.2
r.Nanite.MaxPixelsPerEdge=1
r.Nanite.Streaming.StreamingPoolSize=1024
```

`r.ViewDistanceScale` 乘在每个 HISM 组件的剔除距离上（见 §5），两级相乘
得到最终距离。

> Cinematic 这里用 `1.2` 而不是 Epic 自带的 `10.0`。引擎默认在顶层档
> 基本关掉视距剔除，对本项目是个陷阱：它不会让场景更好看，只会撤掉
> 维持帧时间的那个预算。校园的轮廓靠中近景，远处的方块本来就不足一像素。

### 4.2 Landscape LOD 分布

地形是纯静态几何，远处降级肉眼几乎不可见，是仅次于 HISM 剔除的第二个
帧时杠杆。两个 cvar 在 `LandscapeRender.cpp` 中都声明为 `ECVF_Scalability`、
默认 `1.0`：

```ini
[ViewDistanceQuality@0]
r.LandscapeLOD0DistributionScale=1.8
r.LandscapeLODDistributionScale=1.4

[ViewDistanceQuality@1]
r.LandscapeLOD0DistributionScale=1.4
r.LandscapeLODDistributionScale=1.2

[ViewDistanceQuality@2]
r.LandscapeLOD0DistributionScale=1.0
r.LandscapeLODDistributionScale=1.0

[ViewDistanceQuality@Cine]
r.LandscapeLOD0DistributionScale=0.85
r.LandscapeLODDistributionScale=0.9
```

**方向是反的，这是本项目最容易搞错的一处。**
`ULandscapeLODStreamingProxy::GetLODScreenSizeArray`（`Landscape.cpp`）这样算
每一级 LOD 的屏幕尺寸阈值：

```cpp
const float ScreenSizeMult =
    1.f / FMath::Max(LOD0DistributionSetting
                    * CVarLSLOD0DistributionScale->GetFloat(), 1.01f);
for (...) { Result.Add(CurrentScreenSize); CurrentScreenSize *= ScreenSizeMult; }
```

cvar 在**分母**上，所以值越大衰减越快、降级越早。把「数字大 = 画质高」的
直觉套上来，整条档位梯度会倒过来——关卡照样能跑、照样出图，只是最高档
悄悄成了最糊的一档。

被钳制的是**乘积**而不是 cvar 本身（下界 `1.01`）。引擎默认
`LOD0DistributionSetting = 1.25`，因此 scale 低于 `1.01 / 1.25 ≈ 0.81` 之前
都还有效果。这就是 Cinematic 能取 `0.85` 的依据：乘积 `1.25 × 0.85 = 1.0625`，
仍在钳制之上。**若某个值让乘积 ≤ 1.01，那行就是死文本**，引擎不会给任何提示。

两个地形杠杆必须指向同一意图：`r.ViewDistanceScale > 1`（看得更远）必须配
`r.LandscapeLOD0DistributionScale < 1`（地形更精细）。Cine 档正是这样配的——
它看得更远，所以地形不能是画面里最糊的东西。否则就是「看得更远，但远处地形
比低档还差」，也就是顶级档的画质倒退。

### 4.3 阴影

```ini
[ShadowQuality@0]
r.Shadow.Virtual.Enable=0
r.ShadowQuality=0
r.Shadow.MaxResolution=512
r.Shadow.DistanceScale=0.4
```

低档直接关虚拟阴影：校园只有一盏平行光，阴影是仅次于 GI 的 GPU 开销。
Medium 档用传统 CSM（`r.ShadowQuality=3`）过渡，High 起开 VSM。

### 4.4 全局光照

```ini
[GlobalIlluminationQuality@0]
r.DynamicGlobalIlluminationMethod=0    ; 关 Lumen，退回 lightmap/AO
r.Lumen.Reflections.Allow=0
```

Lumen 是引擎里最贵的功能，而本项目地图**完全静态、单光源**——这是
收益最低而代价最高的一档组合。

### 4.5 贴图流式

```ini
[TextureQuality@0]
r.Streaming.MipBias=2.0
r.Streaming.PoolSize=256
r.Streaming.MaxTempMemoryAllowed=32
```

低配多是与 CPU 共享内存的集显，流式池要压到最小，否则它跟系统抢内存。

### 4.6 自动选档阈值

见 §7。这是本项目唯一一处**必须**写在 `DefaultScalability.ini` 而非
DeviceProfile 的配置。

---

## 5. 项目侧基准值（写入每个 HISM 组件）

无论哪一档，组件上都有一个基准值；`r.ViewDistanceScale` 再对它做乘法。
基准值按物体视觉重要性分档，依据是「物体在多远之外小到不足一个像素」——
方块世界里这条比任何拍脑袋的米数都可靠。

**方块层（layer 1）** 全部方块共用一条，因为它们尺寸相同（1 m 立方）：

| 项 | 值 | 说明 |
|---|---|---|
| `instance_start_cull_distance` | 30000 cm（300 m） | 开始淡出 |
| `instance_end_cull_distance` | 40000 cm（400 m） | 完全剔除 |
| `instance_count_per_leaf` | 64 | 簇树叶节点密度，引擎默认 32 |

淡出带宽 100 m 是刻意留的：只设 end distance 会在剔除边界出现硬 pops，
在平坦光照的校园里非常明显。100 m 的过渡带在 1 m 网格下足够长，实例数量
又少到不心疼。

> 校园尺度约 720 × 1040 方块 = 72000 × 104000 cm。400 m 的剔除距离是
> **视野距离**，不是地图尺寸——从校园任一点看过去都够用，同时把远端
> 那些已经不足一个像素的方块留下来变成纯浪费。

**物件层（layer 2，phase 2 语义分类）** 必须分档，因为不同类的尺寸差两个
数量级，用同一个距离要么把草丛画到地平线，要么把大楼在还看得清时就删掉：

| 类别 | 淡出起点（cm） | 剔除终点（cm） | 依据 |
|---|---|---|---|
| `building` | 40000 | 55000 | 地图主角，轮廓可读范围最远 |
| `tree` | 30000 | 42000 | 中景剪影支柱，校园观感的骨架 |
| `structure` | 20000 | 30000 | 中等体量 |
| `prop` | 10000 | 14000 | 小装饰，近处才认得出 |
| `plant` | 6000 | 9000 | 草花级，走几步就过了 |
| *（未列出的类）* | 12000 | 16000 | 兜底取中值：未知物件更可能是小家具而非高塔 |

### 5.1 地形 LOD 基线

`lod0_screen_size` 设为 `1.0`（引擎默认），写在 Landscape actor 上，
作为**所有档位共同的基线**；各档的差异由 §4.2 的两个 distribution cvar 乘上去。
`lod_blend_range` 保持 1.0（在整个 section 范围内混合），31 quad 的 section
就是 31 m 过渡带，足以藏住 pops。

> **曾经写错过一次，值得留档。** 这一节原本的理由是
> 「`r.LandscapeLOD0DistributionScale` 是 console cvar，不持久化，值只活在
> 日志里」，于是只用 actor 属性。**这个判断是错的**：手动在控制台输入的 cvar
> 确实不持久，但写在 `[ViewDistanceQuality@N]` 段里的会——它们由 scalability
> 组驱动并随 `GameUserSettings.ini` 保存，这正是那两个 cvar 声明
> `ECVF_Scalability` 的原因。
>
> 后果不是配置冗余，而是**低配档根本没有地形降级**：`lod0_screen_size` 是
> 全关卡单值，五档拿到完全相同的地形 LOD，Low 档就得硬扛 ~79 万个地形顶点。
> `tests/test_hism_culling.py` 里的
> `test_landscape_lod_is_tiered_in_ini` 就是为这个缺口加的。
>
> 教训和本轮其他几处一样：**「机制 X 不持久」不能外推到「所有设置 X 的方式」。**
> 要区分「谁来写这个值」，而不是只看「值最终落在哪」。

---

## 6. 落地方式（三层分工）

这三层各管一件事，**互不越界**。把职责搞混是本项目最容易犯的错，
因为其中一层的常见用法在另一层完全无效，而且不报错。

| 层 | 文件 | 职责 | 不负责 |
|---|---|---|---|
| 定义 | `Config/DefaultScalability.ini` | 每档是什么；引擎自动选档的阈值 | 谁被选中 |
| 兜底 | `Config/DefaultDeviceProfiles.ini` | 未跑基准时的安全默认档；可按名选取的档位 | 自动检测 |
| 决策 | `Content/Python/apply_quality.py` | 运行时真正选档；玩家手动覆盖 | 定义档位内容 |

### 6.1 引擎到底从哪读自动选档阈值（源码结论）

这一节是本轮最重要的修正，代价是三处返工。

`PerfIndexThresholds_*` **只在 `DefaultScalability.ini` 的
`[ScalabilitySettings]` 段被读取**，来源是
`Engine/Source/Runtime/Engine/Private/Scalability.cpp:150-152`：

```cpp
const FString ArrayKey = FString(TEXT("PerfIndexThresholds_")) + GroupName;
TArray<FString> PerfIndexThresholds;
GConfig->GetSingleLineArray(TEXT("ScalabilitySettings"), *ArrayKey,
                            PerfIndexThresholds, GScalabilityIni);
```

值是一个空白分隔数组，**第一个 token 是索引类型**（`CPU` / `GPU` /
`Min`），后面三个是 0→1、1→2、2→3 的分界：

```ini
PerfIndexThresholds_ViewDistanceQuality="Min 30 120 400"
```

> **踩过的坑**：原先在 `DefaultDeviceProfiles.ini` 里写了
> `PerfIndexThresholds_Universal=MCReplica_Medium 30 120 400`，
> 以为 DeviceProfile 能按性能指数映射到档位。**引擎完全不读它**——
> 没有这条代码路径，值也不含 profile 名。文件看起来完全合理，
> 机器就是默默停在默认档。`tests/test_quality_tiers.py` 现在有一条
> 专门的反向断言：DeviceProfile 里出现 `PerfIndexThresholds` 即失败。

### 6.2 Windows 上没有「按硬件选 DeviceProfile」这回事

`WindowsDeviceProfileSelector` 的 `GetRuntimeDeviceProfileName()` 只返回
平台名加可选的 RHI 后缀，即 `Windows` 或 `Windows_<RHI>`：

```cpp
FString ProfileName = FPlatformProperties::PlatformName();
FString TmpProfileName = ProfileName + TCHAR('_') + GetSelectedDynamicRHIModuleName(false);
if (AvailableProfiles.Contains(FString::Printf(TEXT("%s DeviceProfile"), *TmpProfileName)))
    ProfileName = MoveTemp(TmpProfileName);
```

按 GPU 家族匹配的 `MatchProfile`（`SourceType=SRC_GpuFamily` 等）是
**Android 专属**，由 `AndroidDeviceProfileSelector` 提供。Windows 上
没有任何机制能按硬件挑 profile。

所以 DeviceProfile 在本项目里的真实职责只有两个：

1. **未跑基准时的地板**。打包后所有组默认 Epic(3)，本项目把 `[Windows DeviceProfile]`
   钉在 **Medium**——这是本项目真能跑的档位。
2. **可按名选取的档位**。`MCReplica_Low` … `MCReplica_Cinematic` 供脚本
   或 `-MCReplicaTier=` 命令行显式激活。

> 顺带一个 RHI 段命名的坑：后缀是 **RHI 模块名**，即 `Windows_D3D12` /
> `Windows_Vulkan`，**不是** `Windows_DX12`。写错的后果是安全的——
> 查找落空、机器退回 `[Windows DeviceProfile]` 的 Medium 地板。
> 本项目没有 RHI 专属需求，因此**不声明**任何 RHI 段，
> 而不是猜一个名字装作在配置什么。

### 6.3 运行时决策（`apply_quality.py`）

三层从外到内嵌套，越靠前越具体：

1. **玩家手动锁定**（`set_tier()`），持久化到 `~/.mcreplica/quality.json`，
   跨启动、跨重装生效；
2. **引擎基准指数**（`GameUserSettings` 的 `LastCPUBenchmarkResult` /
   `LastGPUBenchmarkResult`）；
3. **适配器名 + 显存启发式**，用于「从没跑过基准」的新装机器。

没有任何一步会抛异常。全都探测失败时 `auto()` 返回 `None`，
把 DeviceProfile 的 Medium 地板原样留着。

**为什么不自动跑基准**：`run_hardware_benchmark()` 会让游戏卡住数秒。
在每次启动时无声地做这件事是敌意设计。基准结果存在
`GameUserSettings` 里跨启动有效，所以每台机器只需跑一次，
由 `apply_quality.benchmark()` 在加载界面显式触发。

**取 min 而不是平均**：`idx = min(cpu, gpu)`。本项目是 CPU 瓶颈
（160 万 HISM 实例的簇树遍历，代价随实例数和视距走，不随像素走），
快 GPU 配慢 CPU 照样掉帧。用平均值会在中端笔记本上判出偏高的档，
而这正是「自动设置感觉坏了」最常见的成因。

**为什么走 console 而不是 `GameUserSettings` 的 setter**：部分组
（尤其 `ResolutionQuality`）的 setter 会静默 no-op，而 console 命令
正是引擎自己的 Scalability 菜单底层用的方式，因此不会与 ini 段脱节。

**分辨率的 100% 天花板**：`sg.ResolutionQuality` 会镜像到
`r.ScreenPercentage`，但被 `Scalability::MaxResolutionScale == 100`
钳制（`Scalability.h:185`）。所以 Cinematic 档的分辨率是 100 而不是更高，
**超采样必须另走一路**——`set_supersampling()` 直接设
`r.ScreenPercentage`，且刻意不并入任何档位：它是截图用的战术选择，
不该在玩家切档时悄悄生效。

### 6.4 打包后的路径

- 启动默认档：`DefaultScalability.ini` 的 `[ScalabilityGroups]` 段（Medium = 1），
  加上 `[Windows DeviceProfile]` 的地板值。两者都是引擎原生，零代码。
- 自动选档：`apply_quality.py` 需要 Python 插件，`MCReplica.uproject` 已启用
  `PythonScriptPlugin`。
- 玩家手动切换：`apply_quality.set_tier(n)` 把档位名写进
  `~/.mcreplica/quality.json`，下次启动由 `auto()` 读回；`set_tier(None)` 清除。
  落盘的键是**档位名**（`Low` / `Medium` / …）而不是数字，便于手改和排查。

> 刻意的边界：`apply_quality.py` 定位为「编辑器内诊断 + 一键调档」，
> 不是游戏内设置菜单。游戏内菜单属于玩家界面工作，不在本次范围。

---

## 7. 自动选档的判据

阈值写在 `DefaultScalability.ini` 的 `[ScalabilitySettings]`，
按组区分 CPU/GPU 倾向：

```ini
[ScalabilitySettings]
PerfIndexThresholds_ViewDistanceQuality="Min 30 120 400"
PerfIndexThresholds_ShadowQuality="Min 30 120 400"
PerfIndexThresholds_GlobalIlluminationQuality="Min 30 120 400"
PerfIndexThresholds_TextureQuality="Min 30 120 400"
PerfIndexThresholds_EffectsQuality="Min 30 120 400"
PerfIndexThresholds_FoliageQuality="Min 30 120 400"
PerfIndexThresholds_ResolutionQuality="GPU 30 120 400"
PerfIndexThresholds_AntiAliasingQuality="GPU 30 120 400"
PerfIndexThresholds_ReflectionQuality="GPU 30 120 400"
PerfIndexThresholds_PostProcessQuality="GPU 30 120 400"
PerfIndexThresholds_ShadingQuality="GPU 30 120 400"
```

分组依据（这是本项目最重要的一条调参理由）：

- **`Min`**：视距、阴影、GI、贴图、特效、植被。这些组的开销随
  HISM 实例数与簇树遍历走，是 CPU 侧成本，快 GPU 救不了。
- **`GPU`**：分辨率、抗锯齿、反射、后处理、着色。这些是纯像素成本，
  弱 CPU 不构成瓶颈，为此降档是白白损失画质。

参考点（`SynthBenchmark` 指数，100 = 常规好 CPU/GPU）：

| 机器 | 指数 | 落档 |
|---|---|---|
| i5-8265U + UHD 620 | ~22 | Low |
| i5-12400 + GTX 1650 | ~85 | Medium |
| i7-12700 + RTX 3060 | ~240 | High |
| i9-13900 + RTX 4070 | ~600 | Epic |

**Cinematic 不会被自动选中**，与引擎行为一致（阈值只覆盖 0..3）。
这也让「自动」和「手动」的语义对齐：手动选 Cinematic 是明确的额外请求。

> Intel 的引擎优化指南建议现代项目把阈值整体上调到 `GPU 150 260 550`
> 量级。上表是本项目的**起点**而非定论——真机跑过之后应当按实测调整。
> 官方也提醒：Development 配置下的基准分数会比 Test 配置低约 30%，
> 定阈值应以 Test 配置为准。

---

## 8. 验证方式

沙箱无 GPU、无 UE，无法实测帧率。验证是分层的：

| 层次 | 验证内容 | 在哪验证 | 状态 |
|---|---|---|---|
| 引擎契约 | 阈值位置、`@Cine` 段名、100% 钳制、启动默认档 | `tests/test_quality_tiers.py` | 已通过 |
| 剔除配置 | 淡出带顺序、低配仍可用、按类分档、降级不中断导入 | `tests/test_hism_culling.py` | 已通过 |
| 产物正确性 | Landscape 分块覆盖、高度解码范围 | `tests/test_import_phase2_offline.py`（46 项） | 已通过 |
| 管线正确性 | 分块、重采样、坐标配准 | `tests/run_tests.py` | 已通过 |
| 实际帧率 | 各档在目标硬件上的 fps | **必须在 Windows + UE 5.5.4 实测** | **未验证** |

**未实测的部分不会被声称为「已验证」。** 帧率与观感验收清单写在
`project/README.md`。

---

## 9. 已知限制

- **Nanite Landscape 的取舍**：UE 5.3 起支持 Nanite Landscape，但多份实测
  表明在 RTX 3060 级别及以下硬件上比传统 Landscape **慢约 20%**。
  本方案的 `Landscape` actor **未启用 Nanite**，靠传统 LOD 拿性能。
  有意选择，不是遗漏——这与 §1 第 3 条并不矛盾：那条说的是
  **`r.Nanite` 开关本身**（影响 HISM 与 fallback 路径）在低配档保留，
  这里说的是 **Landscape actor 的 Nanite 优化**不启用。两者是不同层次的东西。
- **HISM 剔除距离是静态基准**：不随相机动态调整。动态密度衰减需要
  HLOD 或自定义 C++ 组件，超出 Python-only 范围。
- **属性名跨版本漂移**：`instance_start_cull_distance` 等 Python 属性名
  在引擎版本间变过（`ld_max_draw_distance` 在 4.25 前叫
  `max_draw_distance`）。所有可选属性都是 best-effort 写入：失败则
  **按 key 去重告警一次**并继续导入——缺一个剔除距离只掉帧，
  让导入崩掉则丢掉整个关卡。
- **超采样不随档位走**：见 6.3，引擎在 100% 处钳制。
- **地形 LOD 的分档值下界是引擎钳制，不是 1.0。** 乘积
  `LOD0DistributionSetting × Scale` 被钳在 1.01，所以 `Scale` 低于
  `1.01 / 1.25 ≈ 0.81` 之后**再怎么调都没有效果**，且引擎不给任何提示。
  这也是本方案只在 Cinematic 档用到 0.85 的原因——再往下就是自我安慰了。
- **`bUseScalableLODSettings` 未启用。** `LandscapeProxy` 上还有一组
  `FPerQualityLevelFloat` 字段（`ScalableLOD0ScreenSize` 等），打开
  `bUseScalableLODSettings` 就能把 LOD 分布直接做成引擎原生的每档数组，
  比现在的「actor 基线 + ini cvar」更贴合引擎设计。本方案没走这条路，
  因为 Python 侧写 `FPerQualityLevelFloat` 的行为缺少文档与实测验证，
  按本项目「不确定的机制不写进生产配置」的原则留给实机验证阶段。
  一旦验证通过，§4.2 的两组 cvar 可直接由该机制取代。

