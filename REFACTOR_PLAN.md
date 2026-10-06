# MC2UE5 技术链审计与重构方案

> 目标（不变）：把 `SYFZ_1.16.5` 存档里的**校园**变成可在 UE5 里游玩、且视觉写实的关卡，
> 并且**校园布局与 MC 地图严格对应**（操场、大门、各栋楼的位置不能变）。

---

## 一、当前技术链（事实清单，2026-10-06）

### 1.1 数据流

```
SYFZ_1.16.5 (.mca)                     ← 不在本机（见 §6 硬约束）
        │  phase2/voxelio.py  ← 自研容器
        ▼
voxel_data/full/overworld.bin  (58 MB, 魔数 MC2WV2)
        │  project/Content/Python/import_world.py
        ▼
1,171,144 个方块实例 → 1,041 个 MCReplicaPropCluster(HISM)
        │  + 19 个材质实例 MI_*（母材质 M_MC_Surface）
        ▼
UE 关卡 /Game/Maps/MCReplica → package.bat → Win64 Shipping
```

### 1.2 自研代码量

| 部分 | 行数 | 说明 |
|---|---|---|
| `project/Source/MCReplica/` | 2,892 | 其中 `MCConsoleCommands.cpp` **1,464**（上帝文件） |
| `tools/*.py` | 2,249 | 13 个脚本 |
| `phase2/*.py` | 3,762 | 8 个模块 |
| **合计自研** | **≈ 8,900** | 全部为**本项目独有**逻辑 |

### 1.3 运行时（C++）职责

| 文件 | 职责 |
|---|---|
| `MCConsoleCommands.cpp` (1464) | GameMode、`BuildProceduralTerrain`(**死代码**)、`ApplyLook`(光照/曝光)、`MCTour`、`ParkAtStop`、`MCBlockCollision`、`RunRuntimeDiag`、资产自检、命令开关 |
| `MCReplicaCharacter.cpp` (321) | 第一人称角色（相机挂胶囊、眼高 165、FOV 90） |
| `MCFrameCapture.cpp` (176) | 视口截帧（**不可用**：`ReadPixels` 返回纯白） |
| `MCReplicaPropCluster.cpp` (173) | HISM 簇载体 |
| `MCMeshWindingLibrary.cpp` (99) | 自研 OBJ 绕序数学 |
| `MCReplica.cpp` (16) | 模块入口 |

### 1.4 已实测验证可用的部分（**保留**）

| 资产 | 价值 |
|---|---|
| `tools/focus_capture.py` | **PrintWindow + PW_RENDERFULLCONTENT** 截图，唯一可靠取像手段 |
| `tools/pick_spawn.py` | 出生点由体素数据**推导**（漏斗 278,399→38,340）；修掉了"出生在树里" |
| `tools/survey_landmarks.py` / `campus_map.py` | 地标普查与俯视图，是"与存档对应"的**验收依据** |
| `tools/fetch_cc0_textures.py` | CC0 素材抓取（md5 校验、许可登记） |
| `tools/block_families.py` | MC 方块 → 材质族的语义映射（19 族） |
| `tools/reg_sdk.py` | 本机 Windows SDK 注册丢失的绕行 |
| `tools/mc_build.sh` 等价流程 + 就地开关 | `-MCev/-MCfog/-MCsuns/-MCsky/-MCdist/-MCdiag` |
| C++ `ApplyLook()` | 光照/曝光的**唯一权威**（关卡里存不住，见 §2.3） |

### 1.5 已确认**必须废弃**的部分

| 资产 | 行数 | 废弃理由 |
|---|---|---|
| 1,171,144 个 HISM 方块实例 | — | **架构级错误**（见 §2.1） |
| `BuildProceduralTerrain` + `.u16` 高度图链 | ~200 | 与体素层重叠过（P0 卡顿主因），现已关闭，是死代码 |
| `tools/build_terrain_mesh.py` + `export_heightmaps.py` | 399 | 服务于高度图链，随之废弃 |
| `MCMeshWindingLibrary.cpp/h` | 176 | 自研 OBJ 绕序；标准 OBJ 格式 + UE 导入器会处理 |
| `MCFrameCapture.cpp` | 176 | 已被 `focus_capture.py` 取代 |
| `phase2/landscape.py / props.py / semantic.py / terrain.py` | 2,247 | 高度图/道具路线，被直接体素网格化取代 |
| `tools/make_character.py` | 347 | 第一人称下身体已隐藏，方块人网格无用途 |
| `tools/voxel_census.py` | 168 | 一次性盘点脚本 |
| 自研 OBJ→StaticMesh 导入 hack | — | UE 有标准 `AssetImportTask` |

---

## 二、问题诊断（"弯路"的根因）

### 2.1 根本错误：把每个方块渲染成一个实例

这是**唯一最重要的架构错误**，也是其余症状的源头。

SIGGRAPH '25 论文《Minecraft to 3D》作者对这种做法有一句直接评价 ——
原文（作者自述项目动机页）：

> "A raw export is either **an absurd triangle mess (every block turned into
> geometry)** or a brittle file that technically imports into Blender/Unreal/Unity
> but looks jagged, heavy, and unusable."

我们正是走了这条被作者**明确放弃**的路。代价是实测的：
- 1,171,144 实例 / 1,041 个 HISM 组件，每帧要遍历全部实例做剔除；
- 剔除距离被迫从 400 m 收到 80 m（否则帧时崩），于是整场取景看不到校园；
- 材质必须挂在实例上，一旦引用悬空就静默回退默认材质（白棋盘）。

正确的做法是**全局共识的标准算法：可见面剔除 + 贪心网格化（greedy meshing）**。
`0fps.net` 的《Smooth Voxel Terrain》是规范参考：对 16³ 区块，剔除后约减少 16×
面数，贪心合并后再降一个量级（示例中 6 个 quad 即最优）。

### 2.2 图层职责混乱

`.mca` 解析、语义重建（高度图/水体/道具）、OBJ 生成、UE 导入装配 —— 四件事
分散在 `phase2/`(8 模块) 与 `tools/`(13 脚本) 与 `import_world.py`，
**没有单一管线入口**，每步的产物路径靠约定而非契约。

### 2.3 三个"静默失败"（已定位，属工具链缺陷）

| 现象 | 根因 |
|---|---|
| 材质 cook 时报 `Failed to compile`、不给出任何原因 | 母材质图含 `MaterialExpressionWorldPosition`；引擎只报结果不报位置 |
| 存关卡后 PostProcessVolume 设置回旧值 | World Partition 关卡的保存路径不持久化该 Actor，而 `save_current_level()` 返回 True |
| Shipping 编译 `Result: Failed (OtherCompilationError)` 且**日志无任何 error 行** | `UnrealGame/Shipping/MCReplica` 每目标中间件损坏；单文件 `cl.exe` 直编却通过 |

### 2.4 光照/曝光：物理正确值是错的

实测：太阳 100,000 lux（晴天物理正确）+ EV100 15 → **整帧纯白**。
1-bit 实验（全部方块换纯品红仍为白，且 EV 11→17 无变化）证明表面被推到远超饱和。
改为 **10 lux** 后画面正常。原因：物理公式假设 EV100 链在起作用，而本管线的
曝光不控制全局亮度；且 PBR 反照率远高于 18% 灰卡，更早削波。

### 2.5 素材源整体偏暖

实测 12 个 CC0 族的 diffuse 均值，**R 比 B 高 18~56**（全暖）：
`grass(109,96,61)` / `leaves(129,94,58)` 是**橄榄褐而非绿**；
`fabric(194,170,156)` 暖米色（**庭院地面就是它**）；
`quartz(177,156,121)` 暖大理石。另试 4 个"名字中性"的候选同样偏暖。
→ **换贴图解决不了**，必须依靠调色或换素材源。

---

## 三、目标架构

原则：**能用标准方案就不自研**；每个关注点只有一个权威；管线有单一入口。

### 3.0 路线决策（已确认）

| 决策 | 选择 | 影响 |
|---|---|---|
| 视觉路线 | **平滑地形 + 物体替换（SIGGRAPH 学术路线）** | 地面 → 连续高度场；建筑等物体 → 独立对象 |
| 存档 | 本机/GitHub/网盘**均不可得**（见 §6.1）→ 用现有 `.bin` | 摄取层留 Amulet 接口 |
| 素材源 | **允许引入 CC0 之外**（ambientCG 等中性系列） | 根治色偏，不靠补偿 |

**这个选择反而让架构变简单**，因为项目里**已经有**平滑地形链：
`phase2/landscape.py`(889) + `tools/build_terrain_mesh.py`(327) +
`export_heightmaps.py`(72) 且**实测渲染成功过**（早期那张绿色网格截图就是它）。
当年 P0 卡顿的根因，恰恰是**同时**跑了平滑地形与 1.17M 体素层；
学术路线直接取消体素层，冲突从根上消失。

```
[1] 摄取 INGEST      ── voxel .bin（含方块名+坐标）→ 统一体素数组
                        〔接口预留 Amulet-Core：拿到 .mca 即可替换，下游不变〕
        ▼
[2] 语义分类 CLASSIFY ── 规则化分割（替代论文的 3D U-Net，本机无法训练）：
                        terrain 地面 / structure 建筑 / vegetation 植被
                        / water 水体 / detail 细节部件
        ▼
[3] 地形 TERRAIN     ── **平滑连续高度场**网格（非方块阶梯）
                        复用 phase2/landscape + build_terrain_mesh（已验证可用）
        ▼
[4] 物体 OBJECTS     ── 每个建筑连通域 → 独立对象网格，**保留原始体量与位置**
                        细节部件（窗/门/栏杆/楼梯）→ 按规则替换为真实几何
        ▼
[5] 水体 WATER       ── 独立平面层（论文做法）→ 交给引擎水体着色器
        ▼
[6] 材质 ATLAS       ── 每材质类一张图集（CC0/ambientCG + 许可清单）
                        → 每对象材质槽 ≤4
        ▼
[7] 装配 + 导入      ── UE 标准 AssetImportTask(OBJ)；母材质 = 纯
                        TextureSampleParameter2D（**已验证可编译的形状**）
        ▼
[8] 构建 BUILD       ── 单一入口 tools/pipeline.py
        ▼
[9] 验收 VERIFY      ── tools/shoot.py 多机位截图 + campus_map.py 对照存档
```

### 3.0.1 关于"物体替换"的诚实范围（**重要**）

论文的替换是"3D U-Net 识别 → 从模型库挑高质量模型"。其**权重未公开**，
本机无法复现训练。因此本项目采用**规则化等价实现**，能力边界如下：

| 论文做法 | 本项目做法 | 对应性 |
|---|---|---|
| CNN 识别结构类别 | 按**建筑材料 + 连通域**规则分割 | 不依赖训练，可验证 |
| 从库中挑替代模型 | 建筑**保留自身体量与位置**，仅"换皮"（真实 PBR 材质 + 真实世界 UV） | **严格保持** |
| 全部物体替换模型 | **细节部件**（窗/门/栏杆/楼梯/招牌）按规则换成真实几何 | 位置保持，观感提升 |

即：**大体积保持不动（守住对应性），小部件真实化（拿到观感）**。
这既满足核心约束，又是可实现的标准做法。

### 3.1 预计收益

| 指标 | 现在 | 目标 | 依据 |
|---|---|---|---|
| 几何方式 | 1.17 M 方块实例（含全部内部面） | 平滑地形 + 独立对象 + 真实部件 | 学术路线 |
| 绘制对象 | 1,041 HISM / 1.17 M 实例 | 1 个地形网格 + N 个对象 | 标准 UE |
| 剔除距离 | 被迫 80 m | 可放到 400 m+ | 方块层取消 |
| 材质风险 | 图脚本 + 19 实例，静默失败频发 | 图集 + 纯 TextureSample 母材质 | 已验证可编译 |
| 代码量 | ≈8,900 行自研 | 预计 **−60%** | §1.5 整表废弃 + 复用已有地形链 |
| 管线入口 | 分散 3 处 | 1 个 `tools/pipeline.py` | — |

**注意**：平滑会削弱方块阶梯（论文自述局限）。为守住"与 MC 地图对应"，
**地形用平滑，但建筑的体量与位置严格按体素数据保留** —— 对应性由建筑的
轮廓和位置保证，不由地面的阶梯保证。这是本路线下对应性的**主要保障点**。

### 3.2 关于"学术级管线 Minecraft to 3D"的取舍

论文方案：3D U-Net 语义识别 → **平滑高度图** → 外部模型库**替换**物体；
另导出独立水体平面。**我们采用**：
- ✅ 分层（地形 / 物体 / 水体分离）
- ✅ 平滑连续地形（代替方块阶梯）
- ✅ 物体替换（以"换皮 + 细节真实化"实现，见 §3.0.1）
- ✅ 独立水体平面
- ❌ 不采用其 CNN（权重未公开，本机无法训练）
- ⚠️ 不追求其"整体平滑掉阶梯"（会破坏对应性，见 §3.1 注）

---

## 四、分阶段实施计划

按**学术路线**重排。S1–S4 是主干（地形 + 物体 + 水体），S5 起是工程卫生。

| 阶段 | 内容 | 产出 | 验收 |
|---|---|---|---|
| **S0** | 冻结现状：审计报告 + 全量推送 | 本文档 + `19a467b` 已推 | 远程含全部提交 |
| **S1** | 语义分类 `tools/classify.py`：地形 / 建筑 / 植被 / 水体 / 细节 | 每类掩码 + 统计 | 操场地表归 terrain、楼体归 structure |
| **S2** | 平滑地形 `tools/terrain_smooth.py`（复用已有链） | 连续高度场 OBJ | 无方块阶梯；与存档高低一致 |
| **S3** | 建筑对象化 `tools/extract_structures.py`：连通域 → 独立网格 | 每建筑一个 OBJ，**体量位置不变** | 楼数/位置与 `campus_map` 一致 |
| **S4** | 细节部件真实化 + 水体层 | 窗/门/栏杆真实几何 + 水面 | 立面可辨；水面独立 |
| **S5** | 图集 + 标准导入 + 材质 | 图集 PNG + StaticMesh + 纯母材质 | cook 零编译失败 |
| **S6** | 装配替换：**删除 1.17 M 体素层** | 干净关卡 | 帧时 < 16 ms @ 400 m |
| **S7** | C++ 拆分上帝文件 | `MCGameMode/MCLook/MCDiagnostics/MCTour` | 无 > 400 行文件 |
| **S8** | 单一管线入口 `tools/pipeline.py` + `tools/shoot.py` | 一条命令跑全链 | 从体素到截图 |
| **S9** | 删除废弃代码（§1.5 全表） | 仓库瘦身 | 自研行数 −60% |

**顺序理由**：先做地形与物体（观感与对应性的主要来源），再动材质/装配
（已知风险区，且现有链路已能出图），最后清理。这样**每个阶段都能出可对比的截图**。

---

## 五、验收标准（可测量）

1. **对应性**：`campus_map.py` 俯视图与存档一致；操场/大门/最短楼位置可在截图中确认。
2. **性能**：整场取景（剔除 400 m）帧时 < 16 ms。
3. **渲染**：`cook` 材质编译失败数 = 0；截图中可见天空渐变、立面细节、地面材质、正确阴影。
4. **工程**：单一管线入口；无 > 400 行的源文件；自研行数下降 ≥50%。
5. **可复现**：`pipeline.py` 从体素数据到截图一条命令。

---

## 六、硬约束与风险

### 6.1 存档不可得（已彻底查找，结论：不可得）

已查找范围（**全部为空**）：

| 位置 | 方法 | 结果 |
|---|---|---|
| 本机磁盘 | 全盘找 `region/`、`level.dat`、`*SYFZ*` | 无 |
| 仓库工作树 | `git ls-files` 匹配 `region/`、`*.mca`、`level.dat` | 未跟踪 |
| Git LFS | `git lfs ls-files` | **空**（LFS 只用于本项目贴图） |
| 全部文档/脚本 | 正则搜 `百度\|网盘\|pan\.baidu\|蓝奏\|quark\|aliyun\|123pan` | 无任何网盘链接 |
| GitHub 仓库 | API 列全部 16 个仓库 | 无存档 |
| GitHub Releases | API 列 MC2UE5 全部 5 个 release | 仅 3 个 Win64 构建 tar.gz，无存档 |

`parse/stats.json` 记录的原始路径是容器内路径
（`/workspace/太原师院附中MC复刻存档/…/SYFZ_1.16.5`），说明存档当年是从
**外部挂载**进来的，从未入库。

**后果**：Amulet-Core / Mineways / jMc2Obj **均无法使用**（三者都需要世界目录或
`.mca`）。**唯一数据源**是 `voxel_data/full/overworld.bin`（含完整方块名与坐标，
58 MB）。摄取层会做成可插拔：一旦拿到存档，换 Amulet 实现即可，**下游不变**。

### 6.2 视觉目标（已确认）

按 **平滑地形 + 物体替换**。风险与对策：
- 论文自述"重平滑会抹掉方块阶梯与装饰图案" → 与"校园必须对应存档"存在张力。
  **对策**：地形平滑，**建筑体量与位置严格按体素数据保留**（见 §3.1 注），
  对应性由建筑轮廓保证。
- 论文的模型替换需 CNN → **对策**：规则化实现（§3.0.1），大体积保持、小部件真实化。

### 6.3 素材偏暖（已确认可另找源）

实测 12 个 CC0 族 diffuse **R 比 B 高 18~56**（全暖）：`grass(109,96,61)`、
`leaves(129,94,58)` 是**橄榄褐而非绿**；`fabric(194,170,156)` 暖米色
（**庭院地面就是它**，视锥内 2,710 列）；`quartz(177,156,121)` 暖大理石。
另试 4 个"名字中性"候选同样偏暖。→ 已获批引入 **ambientCG 等中性系列**
（concrete / plaster / marble / asphalt）替换偏暖族。

---

## 七、与旧文档的冲突（待统一）

| 文档 | 记载 | 实际 |
|---|---|---|
| `README.md` / `project/README.md` | UE 5.5.4 | **UE 5.8.3** |
| `README.md` | 地形走 Landscape + HISM 方块层 | 实际走 OBJ→StaticMesh；本次重构改为 **区块网格** |
| `RENDER_PLAN.md` | 世界空间 triplanar 材质 | **实测该图编译失败**，已回退为逐块 UV |
| `QUALITY_TIERS.md` / `PHASE2_PLAN.md` | 分层管线 | 本次 S1 起由 `pipeline.py` 取代 |

---
*本文档为 S0 交付；S1 起每阶段完成后回填实测数据。*
