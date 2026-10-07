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
| **S5** | 逐族平铺材质 + 标准导入 + **逐槽位**材质绑定 | 22 张族贴图 + 22 母材质 + StaticMesh | 每方块可见纹理细节；槽位与 `usemtl` 逐位一致 |
| **S5.5** | 颜色变体（MC 染色方块）→ 材质实例 tint | 19 个 `MI_MC_*` | 运动场呈绿/红，不是近白 |
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

## 八、S5 实施回填与新增发现（2026-10-07）

> 本节是 S1–S5 的实测回填。**下面每一条都有磁盘/像素证据**，不是推断。
> 与前文冲突处以本节为准。

### 8.1 阶段状态（实测）

| 阶段 | 状态 | 证据 |
|---|---|---|
| S1 分类 | ✅ | 1,949,579 体素 → 88 栋建筑 + 121 树；`out/classify/*.npy` |
| S2 平滑地形 | ✅ | 24 瓦片 / 676,656 三角面（旧方块层 14,053,728，**20.8×** 降幅）；p95 位移 0.397 m |
| S3 建筑对象化 | ✅ | 88 栋 → 158 OBJ / 277,014 面；bbox **88/88** 与 `structures.json` 精确一致 |
| S4 细节 + 水体 | ✅ | 6 类细节库 + `water_layer.obj`（13 个水方块，如实报告不隐藏） |
| S5 材质 | 🔄 | 见 8.2 / 8.3 —— 架构已改对，正在重导 |
| S5.5 颜色变体 | ⏳ | 见 8.4（新发现的 P1 缺陷） |
| S6–S9 | ⏳ | 未开始 |

### 8.2 S5 的真正根因：**不是素材偏暖，是 UV 模型错了**

前文 §2.5 与 §6.3 把「一片单色」归因为 CC0 素材偏暖、需要换源。
**这个归因是错的**，换源永远修不好它。真正的原因是寻址模型：

```
贪心合并产生一个 4×4 方块的大四边形
        │  scale_uvs() 把 UV 归一化后塞进该材质在图集里的那一格
        ▼
那个四边形整块显示一张 504 px 贴图
        → 每个 1 米方块看到的是这张贴图的"平均色"
        → 草 / 砖 / 跑道 / 路面全部塌陷成一片平色
```

`out/atlas/manifest.json` 里其实早就写着这条规则——
"a merged quad is STRETCHED onto its material's cell"——
只是没人注意到它**在 1 米尺度上正好把细节全抹平**。
（讽刺的是 `mesher.why_capped` 还写着「cap 是为了限制贴图密度」，说明当初已隐约察觉，但没有质疑模型本身。）

**修法（标准做法，不自研）**：每族一张 **Wrap 平铺贴图** + **方块单位 UV**（1 方块 = 1 次重复）。
网格本来就每族一个材质槽（OBJ 写 `usemtl <family>`，导入器逐组生成槽位），所以这条路不需要新架构。
- `tools/extract_structures.py` 加 `--uv-mode {atlas,block}`，block 模式**完全跳过** `scale_uvs`，
  UV 保持 `mesh_volume()` 的方块单位值。
- `tools/split_atlas_to_families.py` 把图集切成 22 张族贴图，镜像 16 px 边保证可平铺，
  再 LANCZOS 重采样到 **512×512（2 的幂）**——非 2 的幂配 Wrap + mip + BC 压缩会踩一整类坑。

实测（commit `54ad055`）：`bld_001_structure.obj` 的 `vt` u∈[0,4]、v∈[0,104]（方块单位），
`vt>2.0` 占 18.59%；`terrain_-144_-544.obj` u/v∈[0,127]，`vt>2.0` 占 97.66%。
（对照：atlas 模式 `vt∈[0.00098,0.99902]`，`vt>2.0 = 0` —— 行为未变，回归通过。）

### 8.3 地形**从未被赋过材质**（第二个独立缺陷）

`tools/import_terrain_tiles.py` 只做「导入 + 摆放」，**从不设材质**。
所以 24 个地形瓦片一直挂着 UE 默认的 `WorldGridMaterial`，
后来被统一刷成 `M_MC_Atlas`（寻址 `TA_CLAMP`）。
这就是「地面和建筑出现同一种细网格」的来源——两者在采样同一张图集。

而且地形瓦片**当时没有 `usemtl` 分组**，整个地形只有 1 个材质槽，
草地 / 红色跑道 / 灰路面注定共用一种材质，校园不可能辨认。
已修：`terrain_smooth.py` 新增 `surface_family_map()`，
按 `terrain_h[i,j]` 层的族号分组（该层是 air 时向下搜 ≤3 格，兜底 `other`），
按族写 `usemtl`。实测 24 瓦片族数直方图 `{1组:5, 2组:6, 4组:4, 5组:4, 6组:1, 7组:1, 8组:3}`
—— 5 个纯 `grass` 瓦片是数据单调，不是 bug。

### 8.4 新发现的 P1 缺陷：**10% 的方块颜色被压平，运动场会是近白色**

`tools/block_families.py` 的映射**按材质类型分族，但不认 MC 的 16 色染色前缀**：

```
("wool", "fabric")        ← green_wool / lime_wool / red_wool 全部命中，共用一张近白贴图
("concrete", "concrete")  ← white_concrete / green_concrete / cyan_concrete 全部合并
("terracotta", "brick")   ← pink_terracotta 17,270 块 → 橙红砖色
```

实测（`voxelmat.npz` 的 `family`/`nameid` × `rolevolume.npy` 的 `role!=0`）：

| 项 | 数字 |
|---|---|
| 带染色前缀的方块 | **195,032 块 = 校园的 10.00%** |
| 却被压进的族数量 | **5 个**（concrete 139,384 / fabric 35,022 / brick 20,392 / plaster 95 / leaves 75 / bark 64） |
| 羊毛合计 | **21,542 块**（lime 9,347 + green 8,852 + red 3,117 + brown 223 + light_blue 3） |
| `fabric` 族贴图实测均值 | **RGB(178,180,184) —— 近白** |
| 需新增的 (族,颜色) 组合（≥100 块） | **19 个，合计 55,358 块（2.84%）** |

**后果**：`classify.py` 自述「运动场在这个存档里是羊毛」。
lime_wool + green_wool = 18,199 块全部落进近白的 `fabric` 族
→ **运动场会被渲染成近白色**，而用户明确点名「运动场必须能认出来」。
这是直接违反核心约束的缺陷，与 §5 验收标准第 1 条冲突。

**修法（S5.5）**：MC 的染色方块本来就是「同一张贴图 × 染料色」。
- `block_families` 输出带颜色的键 `<family>#<colour>`；
- 族母材质加 `Tint` 向量参数，`BaseColor = Texture × Tint`；
- 建 19 个材质实例 `MI_MC_<family>_<colour>`，用 MC 真实染料色；
- `import_family_materials.py` 的材质查找按名解析（先 `M_MC_<name>` 再 `MI_MC_<name>`），
  **脚本无需改动**即可支持。

另注：`assets/textures/block/*.png`（739 个）**是 git-LFS 指针，不是真 PNG**
（内容以 `version https://git-lfs...` 开头）。所以 MC 原版贴图本地不可直接读，
颜色变体只能走 tint 或另找源 —— 这与 §6.3「允许另找素材源」的决策一致。

### 8.4.1 颜色变体规格（ART-S5b，`docs/colour_variants.md`）—— 三处修正我原来的判断

**① 全部走「离线烘焙变体贴图」，不用运行时 tint**（推翻我原先「wool/concrete 用 tint」的倾向）。
理由三条，第一条是实测的：
- **ART-S5 期间已实测过 `Tint`：「实例上设对了、也连对了，改动了 0 个像素」** —— 这正是本项目
  最擅长出现的失败（所有非像素检查都通过，画面不动）。
- 变体命名契约本来就是 `<family>_<colour>` → `T_MC_fabric_lime`，**每变体一张贴图这笔开销
  无论如何都要付**，运行时 tint 省不掉它。
- 离线烘焙的 `dE2000 = 0.00`（精确命中），且能在 **UE 启动前**用 PNG 断言验证；
  运行时 tint 只能在引擎内验证，而"引擎内验证"正是上次骗过我们的那一环。

**② 运动场不是单色场地，而是 8 格交替的割草条纹。** `lime_wool` 与 `green_wool` **各 8 格宽交替**
（实测条宽 lime 8.0 / green 8.0 / white 1.4），外面套 13 行 `pink_terracotta` 跑道环带。
→ **`lime` 是场地的一半，不是边线**；若按我原先的想法把 lime 压向绿，条纹分离度（`dE` 25.8）归零，
**球场会变成一块没有图案的平板**。**零几何改动** —— 存档已经把图案摆好了。

**③ `sports` 族是错误定位（自纠）。** 它在存档里 **0 块**，而且**单色贴图原理上做不出割草条纹**
（条纹来自两种方块交替，不是贴图图案）。保留不动（删掉会移位 `ATLAS_FAMILIES` 索引，是 S9 的事），
但**别再指望它**。球场正确解法 = `fabric_lime` + `fabric_green`。

**任务书 19 项的四处修正（其中一处是我漏掉的最大项）**：

| 修正 | 说明 |
|---|---|
| **+ `concrete_white` 107,035 块** | **我漏了，而且是全校园第二大颜色问题**（占混凝土 77%、校园 5.5%）。混凝土底色 (112,112,110)，白混凝土现在只渲染出**一半亮度** |
| **+ `concrete_light_gray` 25,979** | 我漏了；与上项合计 95% 的混凝土 |
| **+ `fabric_white` 6,144** | 我漏了；fabric 底色 (178,180,184)，白羊毛偏灰，球场白线读不出来 |
| **− `brick_red` 130（删除）** | **误报**：`red_nether_brick_wall`(57)+`_stairs`(41)+`_slab`(32) = 恰好 130。地狱砖不是染色陶土，`red_` 只是前缀巧合。照做会把地狱砖染成亮红 |
| `fabric_blue` 248 → **70** | 其中 178 块是 `blue_ice`（冰不是染色织物） |

→ 变体总数 **21 个**（不是 19）。对账：195,032 −130 −178 −6 = **194,718 = 9.99%**。
**目标色用「实机外观色」而非「地图基色」**（`white_concrete` 地图基色 `#FFFFFF`，
实机 (207,213,214)；用前者增益 2.28 会削顶压成死白）。羊毛同理：染料 `lime_dye`(128,199,31)
→ 羊毛实机 (112,185,25)。

**新增缺陷（本规格修不了，需另案）**：`fabric` 族同时收了 `stained_glass(_pane)` 约 1,200 块，
而材质**不透明** → 染色玻璃现在是不透明近白块。上颜色变体只能变成"不透明彩色块"，
**要真透光需要透明/遮罩材质**（另有 `glass_pane` 6,057 + `iron_bars` 1,754 —— 窗户是立面的主要特征）。

### 8.5 静默失败（新增到 §2.3）

| 现象 | 根因 |
|---|---|
| ~~组件级 `set_material(i, ...)` 无效，绘制走资产的 `static_materials`~~ **← 误判，见 §8.12** | 真实原因**不是渲染路径**，而是 `save_dirty_packages` 对 `.umap` 是空操作：改的组件材质从未落盘，同时一个早已存在的图集 override 一直在赢。被误归因成"组件 override 无效" |
| `EditorLoadingAndSavingUtils.save_dirty_packages` 对 `.umap` **是空操作** | 必须用 `LevelEditorSubsystem.save_current_level()`；且保存后要**重新 `load_map` 再数一遍 actor**才算证据 |
| UE 5.8 Python 的 `StaticMesh` **没有** `set_material`/`get_num_materials` | 实测每次调用抛 AttributeError；槽位只能通过 `static_materials` 数组访问 |
| 材质槽位只刷 slot 0 时其余槽仍是 `WorldGridMaterial` | `bld_001_structure` 13 槽里 12 槽是引擎默认灰，15 个可见族里 14 个糊成一片 |

### 8.12 阻断性 bug：组件 override 覆盖资产槽位（引擎源码定论）

`FStaticMeshComponentHelper::GetMaterial()`
（`Q:/UE/UE_5.8/Engine/Source/Runtime/Engine/Public/StaticMeshComponentHelper.h:133-164`，
`UStaticMeshComponent::GetMaterial` 转调它）：

```cpp
// If we have a base materials array, use that
if (OverrideMaterials.IsValidIndex(MaterialIndex) && OverrideMaterials[MaterialIndex])
    OutMaterial = OverrideMaterials[MaterialIndex];
// Otherwise get from static mesh
else if (Component.GetStaticMesh())
    OutMaterial = Component.GetStaticMesh()->GetMaterial(MaterialIndex);
```

**→ 组件 override 非空时优先；只有该槽 override 为 null 才回退到资产的 `static_materials`。**
同一模式在 `StaticMeshComponent.cpp:2911-2925`（`GetEditorMaterial`）与 `GetUsedRayTracingOnlyMaterials`
重复出现三次，是确定语义，不是分支怪癖。

**实测后果（actor 普查）**：

| 对象 | 资产槽位 | 组件 override | 实际渲染 |
|---|---|---|---|
| 158 个结构 actor（370 槽） | ✅ 全部 `M_MC_<family>`，逐位正确 | ❌ **每槽都是旧 `M_MC_Atlas`** | **旧图集** —— 22 个新族材质**完全没被用上** |
| 24 个地形 actor（90 槽） | ✅ 逐位正确 | slot 0 = `M_MC_Atlas`，其余 null | grass（占地形 quads **82%**）用旧图集；soil/rock/greystone 正确 |

**这是一个「资产全对、画面不变」的 bug** —— 最难查的一类：它会让 S5 的全部工作**视觉上等于没做**，
「整片单色」原样保留，而且没有任何报错。**修法**：清空这 182 个 actor 的 `override_materials`
（资产槽位已是唯一权威），**并在 `run_meshes()` 里固化为管线步骤** —— 否则任何人重导网格都会让它静默复发。

### 8.6 工程卫生（S6–S9 的具体化）

| # | 问题 | 实测 | 处置 |
|---|---|---|---|
| 1 | 旧体素层仍在关卡里 | `mc_runtime.txt` 11:50：`propISM=1041 propInst=1171144` | S6 删除；`MCLayerControl.h` 的 `bVox=true` 默认要改 |
| 2 | 上帝文件 | `MCConsoleCommands.cpp` **1,614 行**（前文记 1,464，已增长） | S7 拆为 GameMode / Look / Diagnostics / Tour |
| 3 | `docs/` **未被 git 跟踪** | `git ls-files docs/` 为空；5 份文档只在本机 | 立即入库（含本文件引用的 `s5_material_spec.md`、`qa_s5_report.md`） |
| 4 | **两套 `tools/` 树** | `repo/tools` 35 个 · `Q:/MC2UE5/tools` 13 个，**零重叠**；且有重复功能（`fetch_lfs.py` vs `fetch_lfs_object.py`/`fetch_lfs_raw.py`） | 收敛到 `repo/tools/`，硬编码绝对路径改为参数 |
| 5 | **6 份并列计划文档** | `README` / `PHASE2_PLAN` / `QUALITY_TIERS` / `RENDER_PLAN` / `REFACTOR_PLAN` / `RELEASE_NOTES` | 收敛为「本文件 = 唯一权威计划」+ README 现状描述，其余归档 |
| 6 | Shipping 包内容为空 | `dist52/MCReplica/Content/` 只有 `Paks/`；`Paks/` 目录**为空** | 未解决；UAT stage 不复制项目 Content |
| 7 | 无单一管线入口 | S8 未做 | `tools/pipeline.py` 一条命令跑全链 |

### 8.7 目标与验收（重申，不变）

**目标**：把 `SYFZ_1.16.5` 的校园变成可游玩、视觉写实的 UE5 关卡，
**且布局与 MC 地图严格对应**（操场 / 大门 / 各栋楼位置不能变）。

**当前离目标最近的三个具体缺口**：
1. 逐族平铺材质尚未完成一次成功的端到端渲染（S5 收尾中）；
2. 颜色变体未做，运动场会是近白色（S5.5）；
3. 旧体素层仍在关卡里（S6），性能与取景仍受它牵制。

### 8.8 验收仪器：`tools/reference_map.py`（commit `f7554fa`）

§8 之前所有关于「对应性」的讨论都缺一把尺子。已补上：按 **MC 方块真实颜色**逐列着色，
1 方块 = 1 像素，输出 `out/ref_map.png`（448×768）+ `out/ref_map_4x.png` + `ref_map_legend.json`（74 项）。

**它给出了地表的真实构成**（344,064 列，按列顶面方块统计）：

| 方块 | 列数 | 占比 |
|---|---|---|
| `grass_block` | 203,011 | 59.00% |
| **`pink_terracotta`** | **16,798** | **4.88%** |
| `brick_slab` | 12,989 | 3.78% |
| **`lime_wool`** | **9,240** | **2.69%** |
| **`green_wool`** | **8,787** | **2.55%** |
| `cobblestone_slab` | 8,772 | 2.55% |
| `smooth_sandstone` | 7,722 | 2.24% |
| `end_stone_bricks` | 7,578 | 2.20% |
| `white_concrete` | 7,128 | 2.07% |
| 各类树叶（acacia/jungle/oak/birch/spruce） | ~14,848 | ~4.3% |

**这把 §8.4 的优先级从「P1」提升为第一优先级**：剔掉草地之后，
**地表第二显眼的方块就是运动场**（`pink_terracotta` 跑道 4.88% + 两种羊毛场地 5.24%，合计 **10.12%**）。
而这两样恰好是 §8.4 里被压平的颜色 —— 即**校园里最大的非草地地貌，现在一定是错的**。

### 8.9 校园结构（`ref_map_4x.png` 目视，作为对应性验收目标）

这是「校园必须与存档对应」的**验收目标**，UE 截图必须能在这几点上对得上：

1. **一个 400 m 红色环形跑道**，内部是**绿色足球场**，场地带**深浅相间条纹**（= `lime_wool`/`green_wool` 交替），
   中线圆、罚球区、球门线齐全；
2. 场地东侧有一片**绿色地面大字**（校名 Latin 缩写），由染色羊毛拼成；
3. 运动区西北侧有 **4 个绿色篮球场**（lime 羊毛 + 白线）；
4. **西/北侧成组的教学楼**：白色墙面 + **棕红色屋面**（屋面 `brick_slab`/`end_stone_bricks` 系）；
5. 校区**西边界是一条弧形道路**（`cobblestone_slab`/`smooth_sandstone` 系）；
6. 草坪环绕整个校区；树列沿路与楼间分布。

**运动场是这张图上最不可能看错的地标**，也是验收的第一检查点。

### 8.10 S4.5（新增）：植被层缺失 —— 旧体素层暂时不能关

实测：`leaves`/`_log` 方块 `role != 0` 合计 **47,671 块**，其中落在 88 栋建筑标签内 = **0**。
`extract_structures.py` 只网格化「属于 88 栋建筑」的体素，`classify.py` 的 121 棵树不在其中。
→ **新管线的植被产出 = 0**；关卡里唯一的树来自旧的 `MCblk_` 层（1041 个 actor）。

**因此 S6（删除体素层）的前提被修正**：必须先补植被层（S4.5），否则关掉旧层 = 校园所有树消失。
`MCLayerControl` 的 `-MClayers=-vox` 是**可逆**开关，验收截图暂时用它，
但**要写明「此图无树，属预期而不是 bug」**。

### 8.11 已判定可用的仪器 vs 坏仪器

| 用途 | 可用 | 不可用（禁用） |
|---|---|---|
| 取像 | `Q:/MC2UE5/tools/focus_capture.py`（PrintWindow + PW_RENDERFULLCONTENT） | `MCFrameCapture`（`Viewport->ReadPixels` 返回纯白，§1.3 已判定废弃） |
| 对应性 | `tools/reference_map.py`（真实色俯视） | 旧 `render_topdown.py`（高度着色，不含材质）、旧 `campus_map.py`（ASCII，且曾维护第二套分类） |
| 帧时 | 环形缓冲的时间分布 | `GetDeltaSeconds()` 单次采样 —— 被 `MaxDeltaTime` 夹到 400 ms，日志里 18 个采样值全是 400.0 ms，**不可引用** |

**`MCFrameCaptureGameMode` 输出的 `VERDICT nothing lit` 不是场景证据**，
它是一次坏仪器的自述。视觉结论一律以 `focus_capture.py` + 像素测量为准。

### 8.13 玻璃/铁栏规格（ART-S5b·2，`docs/glass_spec.md`）+ 一处计数方法纠错

**① 计数方法纠错（我的错，影响面比看起来大）**

`voxelmat.npz` 的 `names` 表里 **840 个 nameid 只对应 260 个不同名字串**，
**122 个名字串被多个 nameid 指向**（`iron_door` 30 个、`iron_bars` 16 个、`glass_pane` 16 个、
`acacia_leaves` 14 个）。所以**按 nameid 求和会严重漏计**，必须**按名字串聚合**。

| 项 | 我先前报的 | 实测（按名聚合） | 性质 |
|---|---|---|---|
| `iron_bars` | 1,754 | **8,161** | 我引用的是 **列顶面**计数（`ref_map` 的口径），**不是方块总数** |
| `glass_pane` | 6,057 | **9,422** | 只统计了其中一个 nameid |
| 色变体总量 | 55,358 | 55,580 | 差 0.4%，**结论不变** |

→ `iron_bars` 占 `metal` 族 **8,161 / 20,232 = 40%**，是该族**第一大**成分（超过 `iron_block` 3,561）。
「铁栏」不是边角料。

**② 关键发现：建筑是空心壳（填充率 20.6%）→ 否掉「真透明玻璃」**

实测建筑填充率 **20.6% / 21.3%**（实体应为 100%）。所以**真半透明会露出空荡内部与孤立楼板**，
在大尺度读成「废弃 / 穿帮」，而**不是**「装了玻璃」。
→ **玻璃做「不透明 + 反射高光」**，不做半透明。这也使它能**零新节点**实现。

**核心指标**：玻璃现在渲染成 `fabric`(178,180,184)，与白灰泥(177.8,178,174) 的
**`dE2000` 仅 4.3、亮度差 0.2/255** —— **窗户在白墙上完全消失**。改成 (58,68,74) 后 `dE` 44.8、亮度差 119.8。

**③ `alphaMode` 元数据早就存在，是新管线把它丢了**

`assets/material_manifest.json` **已声明** `glass: transparent`、`glass_pane: masked`、
`iron_bars: masked`、`ladder: masked`；旧的 `import_world.py:686` 也**消费过**它。
但 `grep -rn "shapeHint\|alphaMode" tools/` **零命中** —— 新的 per-family 管线丢掉了这份信息
（`greedy_quads(mask, cap)` 把每块都当立方体，`iron_bars` 因此被合并成**整片实心金属板**）。
→ **修复不必重新发明，把既有元数据接回管线即可。**

**④ 图集容量：`glass` + `bars` 正好用满 24 格，仍然 3 行，零位移**

```
cols=8:  ceil(22/8)=3 行 → ceil(23/8)=3 行 → ceil(24/8)=3 行   全部零位移 ✅
         ceil(25/8)=4 行 → plan_layout 的 (rows-1-row) 平移所有既有族的 v  ✅⚠️
```
**而且**：mesher 已切到 `--uv-mode block`，网格存的是**方块单位 UV**，
每族还各自采样 `T_MC_<family>`（不再采样图集）→ **图集排布对网格完全无影响**，
所以 24 不是硬约束。新增族只需**追加到 `ATLAS_FAMILIES` 末尾**（`fam_idx` 是 uint8，上限 255）。
**5 个染色玻璃必须是 `glass` 的变体**（`glass_white` 等），不占图集格。

**⑤ 落地：blend mode 是材质级属性，所以必须是新族而不能是 `metal`/`fabric` 变体**

`iron_bars` 要 masked，但 `metal` 族里还有 `iron_block` 3,561（**必须不透明**）；
`fabric` 必须保持不透明（羊毛 21,542 + 地毯 + 床）。共用材质会把铁块也变镂空。
`("glass","fabric")` 本来就是占位规则（`block_families.py:31` 自己写着 *"placeholder until a glass material lands"*），本规格是来兑现它的。

**⑥ 两档方案（建议节奏：保守档 → 验收 → 完整档）**

| | 保守档（**并入颜色变体同一批**） | 完整档（随后） |
|---|---|---|
| 内容 | `glass` 不透明新族（Roughness 0.10，BaseColor (58,68,74)）+ `glass_<colour>` 变体 | `bars` 新族 + `BLEND_MASKED` + alpha 镂空 |
| 代价 | **新节点 0、新 shader 变体 0** —— 与 21 个变体**同一套机制** | 1 个新 shader 变体 + alpha 管线 |
| 收益 | 窗户 100% 可见（解决「立面可辨」主要问题） | 铁栏近距离观感 |

**`bars` 的最小节点清单 = 1 条新连线 + 2 个材质属性，不需要任何新节点类型**：
`TextureSample.A → MP_OPACITY`（唯一图形改动）、`BlendMode = BLEND_MASKED`、
`OpacityMaskClipValue = 0.333`。玻璃的反射**只调已冻结图形里那个 `Roughness` 常数**（设 0.10），不加节点。
**`TwoSided` 不需要**（mesher 产完整立方体，内外都有正面）。

**⑦ 对颜色变体表的一处偏离（已裁决：接受）**
`glass_white` 用**乳白 (205,208,206)** 而非纯白羊毛色 (233,236,236) —— 白玻璃若用近白，
在白墙上依然会消失，白做。

**⑧ 优先级（同意 art-director 的拆分）**
颜色变体 **+ 不透明玻璃**同批（**同一套机制、近乎零边际成本**），**铁栏 alpha 单独排**。
数量级：色变体影响 194,718 块（9.99%）vs 玻璃 9,666（0.50%）vs 铁栏 8,161（0.42%）；
但**单位成本换来的辨识度，窗户的杠杆率高于大多数单个颜色变体** —— 一块深色玻璃
就能让整面白墙读出窗格节奏。所以是**并入第一批**的理由，不是延后的理由。

### 8.14 颜色变体交付 + 接缝根因（commit `a412739` / `dd47740`）

**① 21 个变体已烘焙，`dE2000` 最大 0.2376（阈值 <1.0）**。已独立复核抽样 5 个，最大偏差 **0.10**。
`family()` 对 **840 个调色板方块名 + 320 个合成组合逐位相同（零回归）** ——
新增 `family_key()` 返回变体名、`family()` 保持返回基础族名，`FAMILIES` 元组未变。
这个「纯新增 146 行、0 行删除」的设计是关键：把变体名灌进 `ATLAS_FAMILIES` 会移位索引。

变体解析对 10 个真实方块实测全部正确：`red_nether_brick_wall`→`brick`（不命中 `brick_red`）、
`blue_ice`→`fabric`（不命中 `fabric_blue`）、`white_concrete_powder`→`gravel`（不命中 `concrete_white`）。

**② `leaves` 的 alpha 已补回（`dd47740`）。** 根因链两段：
`fetch_ambientcg.py:76` 下载 **JPG**（`_1K-JPG.zip`）→ 源无 alpha，透明背景压成纯黑
（`Leaf003_Color.jpg` **69.45%** 像素正好 `(0,0,0)`）；`build_atlas.py` 的 `.convert("RGB")`
+ `Image.new("RGB",...)` 再丢一次。修法：读写 RGBA + `ALPHA_KEY={"leaves":(8,40)}`
从 `max(RGB)` 阈值重建 alpha + 镜像边对 4 通道一起做。
结果：alpha `==0` 65.90% / `==255` 28.50% / 不透明 **31.44%**（源叶片 30.02% ✓）。
**复核发现：只有 `leaves` 有大面积黑底（67.79%），其余 21 族 0.00%**
→ 「改下 `_1K-PNG.zip` 再重烤图集」降级为**已知限制**，不值得拿已标定的 22 族去冒险。

**③ 接缝：我的断言是错的，但顺着它查出了实质问题。**

我给「接缝 ≤ 内部基线」这条断言时**没有先验证它对现有贴图是否成立** —— 实测**对任何
现有贴图都不成立**（既有 `brick.png` 缠绕缝 12.87 vs 基线 4.62）。
按实测如实报告而非凑指标，是对的。

**实质问题与根因**：族贴图是从**图集裁切**出来的（裁 504px 格子 → LANCZOS 到 512）。
而 ambientCG 源图**按 1024px 设计成可平铺**，**裁一个 504px 子窗口必然破坏平铺性**。
**镜像边并不能修好它**：`arr[:w] = arr[w:2w][::-1]` 只让「边框 ↔ 内侧」连续，
却在边框处塞进一条 **16px 的镜像带** —— 既没得到真平铺，又引入镜像伪影。

**更好的路（顺带提画质）**：网格已走 `--uv-mode block`、**每族各自采样 `T_MC_<family>`，
不再采样图集**，所以族贴图**没有理由再从图集裁切**。直接从 **ambientCG 源图**（1024，
作者保证可平铺）缩放 512，并把 `build_atlas.py` 里已标定的每族 gain 施加到源图即可：
1024→512 比 504→512 源分辨率更高，且**平铺性应当实测下降**。正在测（含颜色 `dE` 对比）。

**这条为什么值得修**：网格是**每方块面 = 一个纹理周期**（方块单位 UV），
所以贴图不平铺 → 接缝会在**每个 1 米格线**上出现，全域都是潜在可见的网格线。
**用数据决定是否上「边界交叉淡入」**（那要动全部 22 张已标定贴图），不预先实现。

**④ 一个可复用的坑**：**PIL 的 LANCZOS 在 RGBA 上会按 alpha 加权重采样 RGB**
→ 会把已标定的颜色**悄悄改掉**，且只在抠图族上出现。必须 **RGB 与 alpha 分开缩放再 merge**
（这样 RGB 与原来逐字节一致）。已在实现里落地。

---
*本文档为唯一权威计划。S0 定架构，S1–S4 已回填，S5 见 §8，S4.5 见 §8.10，S5.5/5.6 见 §8.13–8.14。*
