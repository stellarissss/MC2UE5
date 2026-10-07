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
| 探针读「导入后贴图尺寸」时 22 个全返回 **None**，看着像导入失败 | UE 5.8 Python **没有** `imported_size_x/y` 属性；正确访问器是 **`blueprint_get_size_x()` / `blueprint_get_size_y()`**。**实测 22 张全部 512×512** —— 是探针读错了属性名，不是导入失败 |
| `-ExecutePythonScript` 后面的 `--stage=x` **传不进脚本**（`sys.argv[1:] == []`） | UE 自己吞掉了参数 → 静默按默认 stage 跑（曾因此误入 meshes 阶段）。**用环境变量 `MC_STAGE=<stage>`**，并在日志首行打印实际解析到的 stage |
| `-game` 与无头编辑器**不加 `-nullrhi` 会崩** | 预存断言 `Assertion failed: NumAcceptedStaticMeshes >= 0 && MDCIdx < 0xffff [ShadowSetup.cpp:1611]`，**Python 还没跑就 exit=3**。所有无头编辑器/cook 一律加 `-nullrhi` |
| 清空组件 override 后再 `load_map`，override 会**从盘上读回来** | 清空必须发生在**保存之前**；保存后若要复核须用**新进程**重载（同进程内的复核会共享写缓存，本项目已被骗过一次） |

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

**④ 图集容量：~~24 是硬上限~~ —— 我写错了，实际上是 32（已核实并更正）**

`plan_layout` 把 `used_rows` **向上取整到 2 的幂**：
```python
used_rows = ceil(len(families) / cols)      # cols = 8
rows = 1
while rows < used_rows: rows *= 2            # ← 关键：取到 2 的幂
```
实测族数 → 行数：

| 族数 | 21 | **22–32** | **33** |
|---|---|---|---|
| `rows` | 4 | **4（不变）** | **8** |
| 图集高 | 2048 | **2048（不变）** | 4096 |

→ **22 到 32 族全部 `rows=4`、零位移**，**第 33 个族才触发 rows=8** 并平移所有既有族。
`glass`（索引 22）落在 row 2 / col 6，与既有族同行，`y0` 不变。
**所以可用家族名额是 10 个，不是 2 个** —— `bars` 与将来的族都不受排期限制。
（`build_atlas.py:77` 的原始注释其实写对了「rounds up to 4 exactly as 21 did」，
是后续分析把它误推成 24 上限；已在源码注释里写清。）
**并且** mesher 已走 `--uv-mode block`、每族各自采样 `T_MC_<family>`，
**图集排布对网格完全无影响** —— 所以真正的上限只有 `fam_idx` 的 uint8（255）。
**5 个染色玻璃仍做成 `glass` 的变体**（变体不占图集格，这个判断本身是对的）。

**⑤ 落地：blend mode 是材质级属性，所以必须是新族而不能是 `metal`/`fabric` 变体**

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

### 8.14.1 族贴图改从**源图**派生（commit `b4ff4e8`）—— 接缝 −31%，并证明镜像边是错的

**根因（§8.14 ③ 已定位）**：族贴图从**图集裁 504px 格子**，而 ambientCG 源图按 **1024px
设计成可平铺**，裁子窗口破坏平铺。修法：**直接从源图缩放 512 + 施加已标定的 `BAKE_GAINS`**。
`--source cc0` 现为默认，`--source atlas` 可一条命令回退。

**实测（我独立复算 10 个族，与交付者数字逐位一致）**：

| 族 | atlas 裁切 | **cc0 源图** | 改善 |
|---|---|---|---|
| **brick** | 12.87 | **3.98** | **−69.1%** |
| **leaves** | 33.49 | **7.03** | **−79.0%** |
| **metal** | 37.51 | **7.73** | **−79.4%** |
| **rock** | 17.70 | **4.73** | **−73.3%** |
| **quartz** | 9.87 | **3.71** | **−62.4%** |
| soil / bark / roof / concrete / grass | — | — | −46% ~ −32% |
| **合计（23 族）** | **305.60** | **210.98** | **−31.0%** |

**「缝 / 内部基线 > 1.1」的族：atlas 5 个 → cc0 0 个。** 即 cc0 下**没有任何族**
存在一个「比自身内部变化还突兀」的接缝。

**一个例外，我裁决为可接受**：`wood` 绝对值 14.77 → **66.42**（+349.8%），
但**它的内部基线本身就是 63.59** —— 木纹是高对比纹理，66.42 只相当于它任意相邻两列的
正常差异，**ratio 1.04 ≈ 1**。所以 `wood` **没有接缝问题**，只是纹理对比度高。
→ **不做特例**（为单独一个族分来源会让「每族来源不同」，复杂度不值）。

**关键量化：镜像边不是在修平铺，而是在掩盖它。**
`--source cc0 --mirror yes` 会让合计缠绕缝从 **208 反向恶化到 296** —— 这是
「镜像边修不好平铺」的直接证据。所以 **cc0 路径必须不镜像**（`--mirror auto` 已如此默认）。
（§8.14 ③ 曾推测「镜像边只让边框↔内侧连续，却塞进 16px 镜像带」，这里得到了数字确认。）

**颜色不受影响**：cc0 均值 vs atlas 均值，**中位 `dE2000` 0.065、最大 0.180**（阈值 <2.0）。
即「同一组 `BAKE_GAINS` 施加在源图缩放结果上」等价于原来施加在图集格子上 ——
**已标定的颜色全部保住**，同时画质更好（1024→512 优于 504→512）。

### 8.14.2 `glass` 族 + 26 个变体全部落地（commit `2150ba8`）

`glass.png` 均值 **(58,68,74)**，与目标差 **0.00**；**色度 `C* = 5.59`**（要求 ≤10；
对照 `water` 19.72 → **玻璃确实是灰的，没变成蓝水**）；`R−B = −16.0`。
**`dE2000(glass, plaster) = 44.77`** —— 与规格文档预测的 44.8 **吻合到小数第一位**，
亮度差 109.9/255（要求 ≥60）→ **窗户不会在白墙上消失**。
`glass` 族体素 **10,864**（9,666 无染色 + 1,198 染色），与规格计数完全一致。

全库 **26 个变体**一起烘焙，**最大 `dE2000` = 0.151**（原 21 个因基底换成 cc0，
最大从 0.238 降到 0.074）。既有族 sha256 烘焙前后未变，且**连跑两次结果逐字节相同**
（grain 用 `crc32` 播种而非 `hash()` —— 后者每进程随机化，会导致不可复现）。

**`glass_pane` 现在落到 `glass` 族** ✓；4 个染色玻璃变体各自命中。
`family()` 回归：840 个调色板名中差异 **61 个 nameid，全部是玻璃类**，**非玻璃方块 0 差异**。


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

### 8.15 第一份真实视觉证据（`Q:/MC2UE5/shots/family_materials_*_20261007_*.png`）

**好消息：贴图平铺已生效。** 近景地面出现**明显的周期性纹理重复** ——
这正是 block UV（1 方块 = 1 次贴图重复）在工作的直接证据，
相比旧的「整块拉伸成平均色」是**实质进步**。新架构方向对了。

**但画面仍是一片暖褐。成因已实测，不是渲染坏了，是两件事叠加：**

**① 相机出生点就在运动场跑道上 —— 这是最关键的一条。**
出生方块 `(17,-215)`，我查了它周围的地表真值：

| 半径 | 地表族分布 | 方块 top |
|---|---|---|
| **20 格** | **`brick` 70.0%** / `fabric` 29.9% / `grass` **0.1%** | `pink_terracotta` 1120 / `lime_wool` 209 / `white_wool` 155 |
| 50 格 | `fabric` 37.7% / `brick` 35.5% / `grass` 17.5% | `pink_terracotta` 3550 / `grass_block` 1752 / `lime_wool` 1502 |

**出生点 20 格内 70% 是 `pink_terracotta` 跑道，草地只有 0.1%。**
所以开场视野里**没有草地、没有篮球场、没有教学楼** —— 只有跑道。
这也解释了为什么两张不同用途的截图（`field` 与 `novox_after`）**几乎一模一样**。

**② 跑道的颜色变体尚未接线。** 像素实测近景地面 **(103.1, 86.5, 68.0)**，
`brick` 族均值 **(104, 90, 74)**，**`dE76` 仅 7.0** —— 几乎是精确匹配。
即跑道正用 `brick` 的橙红陶土色渲染，而 `brick_pink`（(160,77,78)）还没被用到。
→ **接线完成后这一整片会自动变成跑道该有的粉陶土色。**

**③ 近景那圈「沙丘」条纹 = `brick` 贴图的砖纹在每个方块上重复一次。**
`brick` 是砖墙贴图（含砂浆缝），铺在 53×151 格的平面上、贴着相机掠射查看，
就读成了沙丘/波纹。`brick_pink` 已做高斯模糊 12px 去砂浆缝（std 15.7 → 2.6，光滑黏土观感），
接线后会明显改善。**它同时反过来证明了一件事：每个方块面 = 一个纹理周期。**

**④ 由此暴露一个验收方法学问题：出生点是糟糕的取景位。**
6 m 眼高、贴着跑道看，看不到任何地标 → 无法做「与存档对应」的验收。
→ 已要求增加**多机位取景**（运动场外俯视 / 校园全景 / 教学楼主立面），
且**机位写进脚本参数**（可复现同机位前后对比，比一堆不同角度的图有用）。

**⑤ 一个待定的设计问题**：`pick_spawn.py` 把出生点选在了跑道内沿。
作为「校园」，出生点更合理的位置是**大门或广场**。但改出生点是玩法决策，
**记录下来待确认**，本轮不改（改它会牵动 `pick_spawn` 的漏斗逻辑）。

### 8.16 一个证据陷阱：修复与取证**时间上没重叠**（务必先读这条再看任何截图）

到目前为止**没有任何一张截图同时满足「组件 override 已清 + 旧体素层已关」** ——
而这正是唯一能评判新管线的组合。时间线：

```
12:36-37  voxOFF_0/1/2             ← 早于 override 修复 → 拍的是旧图集材质
12:38     meshes 阶段：清空 182 个 override + 保存关卡   ← 修复在这
12:39     family_materials_fixed   ← 体素层开着（logs/mclayers.txt: vox=1）
12:55     V2_on_0/1/2              ← 体素层仍开着
```

**像素测量证实**（这是发现陷阱的方法，值得复用）：
`family_materials_fixed_123916.png` 与修复**之前**的 `family_materials_121757.png`
**平均逐像素差仅 0.46** —— 几乎完全相同。而 `logs/mclayers.txt` 显示那次 `vox=1`。
→ 结论：**旧体素层在视觉上完全压住了新管线**，所以「修了没变化」。

同时**旧层开关本身已确认生效**：开/关体素层的两张图逐像素差 **34.20**。

**另外测到**：墙体 (89.8,77.3,64.3)、窗带 (90.7,78.3,65.5)、墙体下部 (95.6,82.1,68.1)
**三者几乎一样**，且都是暖褐（天空却是正确的蓝）。
砖 / 玻璃 / 混凝土本应差异很大 —— 「**所有表面塌成同一种暖褐**」是旧调色板全暖的特征，
即看到的是旧体素层，不是新族材质。

**教训（可复用的方法）**：
1. **取证前先核对「修复时间」与「截图时间」是否重叠** —— 否则会拿修复前的图当修复后的证据，
   而且因为画面「看起来一样」而误判「修复无效」。
2. **用平均逐像素差判断两张截图是否其实同机位同画面** —— 差 0.46 一眼就暴露了问题，
   比逐张肉眼看可靠得多。
3. **看 `logs/mclayers.txt` 确认该次运行的层开关状态**，不要从图猜。

**待办**：重跑「override 已清 + `-MClayers=-vox` + 多机位」这一组，作为新管线的第一份有效证据。

### 8.17 帧时：环形缓冲已上线，实测 413 fps / 2.4 ms（体素层开着）

`logs/mc_runtime.txt` 现已输出**分布**而非单次采样：
```
ft[n=165 min=2.4 p50=3.1 p95=6.0 max=9207.7 mean=59.4 class=ok]  frame=2.8ms fps=355
```
即 p50 **3.1 ms**、p95 **6.0 ms**（体素层 `vox=1`、`propInst=1,171,144` 时）。
`mean` 与 `max` 被少数启动期长帧拉高，**应当读 p50/p95，不要读 mean**。
对照 §8.11：`GetDeltaSeconds()` 那条**依旧不可引用**（被 `MaxDeltaTime` 夹到 400 ms）。

### 8.18 更正 §8.16：褐色**不是**旧体素层，就是新管线本身（我错了）

`engineering-lead-3` 在**打包 Shipping** 上实测：`-vox` 关掉 vs 开着，
逐像素差 `max 41`、`px>8 = 1370`（**0.134%**）—— 仅噪声底（620 px）的 2.2 倍。
→ **体素层几乎完全被新地形/结构埋住**；画面里那一片褐色**就是 S3/S4/S5 的新管线**。
我在 §8.16 从「开/关体素层差 34.20」推出「体素层遮蔽一切」是**错的** ——
那个 34.20 是**早期图**（那时新网格刚绑材质），不是当前状态。
**教训：不要把早期截图测出的数字当作当前状态的证据；每次都要用当次 build 重新测。**

### 8.19 pak 已修好（`engineering-lead-3`，commit `c319041`）

用 `UnrealPak -List` 读容器索引（**不是** grep 原始字节 —— 索引与载荷都压缩，字节搜索两个方向都无效）：

| 路径 | `dist_verify2` 条目 |
|---|---|
| `Maps/MCReplica` | 1 ✅ |
| `MC/Structures` | 316 ✅（158 × uasset+uexp）|
| `MC/Terrain` | 48 ✅（24 × 2）|
| `MC/Families` | 34 ✅（17 材质 + 17 贴图）|
| `MC/Atlas` | **0 —— 正确** |

**两处纠正我的参考数字**（以实测为准）：
1. **`MC/Atlas` 现在必须为 0** —— 12:37 起网格已改绑 `M_MC_<family>`，关卡里没有任何东西
   再引用 `M_MC_Atlas`，cooker 正确剔除。我要求它出现在 pak 里是**旧架构的判据**。
2. **族材质 cook 了 17 个，不是 22** —— 22 个在磁盘上，只有 17 个被网格引用
   （结构 15 + 地形 10 的并集）。缺席的 5 个是 `asphalt/path/roof/sports/tiles`，**零引用**，
   被正确剔除。`sports` 零引用与 §8.4.1 ③ 的判断一致。
   cook：`Packages Cooked: 823`、`Failed to compile Material: 0`、**`MaterialShader Assets Built: 17`**。

### 8.20 当前真正的阻断点：**族反照率没有到达屏幕**

三项独立证据指向同一个结论（且**不是**「没平铺」）：
1. 地形按三角形面积 **82.1% 是 `grass`**，而 `out/families/grass.png` 均值 **(84,104,66) 是绿的**；
   渲染出的地面 **(105.1, 88.3, 69.5) 却几乎正好是 `soil` 纹理 (104.1, 90.0, 74.1)**。
2. **全帧绿色像素 0.0%**；旧体素管线同一场景是 **36.2%**。
3. 「整块拉伸成平均色」**解释不了褐色** —— grass 单格拉伸后的平均**仍然是绿的**。

**资产链已逐环验证自洽**（`engineering-lead-3` 读过，非推断）：
cooked map → 158+24 网格 → cooked mesh 引用正确的 `M_MC_*`（`bld_001_structure` 13 个，
顺序与 OBJ `usemtl` 首现一致）→ `M_MC_grass`→`T_MC_grass`、`M_MC_soil`→`T_MC_soil`（无串号）
→ 22 张源 PNG 各不相同 ✓。**我已独立核过地形 OBJ**：`terrain_-144_-544.obj` 是
**4 个整片材质组**（`usemtl` 语句恰好 4 条），`grass` 占 **97.9%** 面 —— **OBJ 侧正确**。

**所以损失在 UE 渲染路径**（材质运行时求值 / 三角形→section 材质索引 / look 管线）。
已排除：曝光（`applylook.txt` = `sun=10 sky=0.250 ev=0.50 fog=0.0015`，正是文档值且
PostProcessVolume 已找到）、空采样器（那会是**白**不是棕）、体素层遮挡、图集（已不在链路里）。

**下一步用判别性实验定位**（见 §8.21），不继续猜。

### 8.21 新发现的两个真缺陷

1. **`-MClayers` 只认第一个逗号前的 token**（`MCLayerControl.cpp:177` 的 `FParse::Value`
   在逗号处截断，`ParseIntoArray(",")` 只拿到一个元素）。实测：`-MClayers=-struct` → `struct=0` ✓；
   `-MClayers=-vox,-struct` → **`struct` 仍 =1**；`-MClayers=-terrain,-shadow` → **`shadow` 仍 =1**。
   → **任何多 token 的 A/B 都是无效的**（只量了第一个 token）。
2. **`terrain(vis=0 col=0)` 是失效探测器** —— 它按 `Name.Contains("overworld")` 认地形，
   而新瓦片叫 `terrain_*`（与 §8.5 那个 `Terrain_`/`Prop_` 前缀 bug 同类）。
   另：`Saved/Crashes` 里的崩溃**全部来自编辑器路径**（`UnrealEditor.exe -game`，
   `ShadowSetup.cpp:1611` / `D3D12RenderTarget.cpp:599`）；**打包 Shipping 一次都没崩**。
   → 取证一律走**打包 Shipping**，编辑器的 `-game` 是更脆的路径。

### 8.22 帧时（打包 Shipping，体素层开着）

`ft[n=334 min=1.6 p50=3.0 p95=3.3 max=4.6 class=ok]`，各次 p50 **2.4–3.3 ms（约 300–400 fps）**，
无 plateau/spike。**性能不是当前瓶颈，颜色才是。**

### 8.23 灰色系族的色调确实塌了（我目视 + 实测确认，两种说法各对一半）

`engineering-lead-3` 报告「至少 12 个族色值非常接近，尤其 `concrete/greystone/granite/rock` 四个几乎完全一样」；
而更早 `quality-lead-2` 说「`fabric/greystone/rock` 三个族视觉上区分度没问题」。
**两者不冲突 —— 量的是不同东西。** 我做了 6 族并排对照（`out/qa/grey_trio_sheet.png`）并实测：

| 族 | 均值 | 图案 | `std` |
|---|---|---|---|
| `concrete` | (112,112,110) | 拉丝 / 刮痕 | 9.2 |
| `greystone` | (104,106,104) | 卵石 + **棕锈** + 缝 | 12.0 |
| `rock` | (96,94,92) | 深色粗糙 | 10.4 |
| `granite` | (110,110,112) | 蓝灰光滑砖 | 7.5 |
| `plaster` | (178,178,174) | **几乎全平** | **0.7** |
| `quartz` | (196,198,200) | 米白砖 | 11.0 |

**结论：图案彼此不同，但 `concrete`/`greystone`/`rock`/`granite` 的色调几乎一样且全近中性。**
远景下这四族会读成**同一种材料** —— 所以 `engineering-lead-3` 在**色调**上是对的，
`quality-lead-2` 在**图案**上是对的。

**两个附带发现**：
1. **`plaster` 几乎无纹理（`std` 0.73）** —— 它用于墙面（3,000+ 块），
   结果是**墙会是死平板**。这是与「立面可辨」直接冲突的质量缺陷。
2. **`greystone` / `rock` 明显偏棕**（对照图第 2、3 格肉眼可见棕锈）——
   MC 的 `cobblestone` 是**灰**的。这是画面「一片褐」的合理贡献者之一，
   也说明 §2.5「素材偏暖」的老问题在 **cc0 源**里仍然存在（只是不再是均值 `R−B` 那种形式）。

→ 处置：**排入 `eng-python-2` 的队列**（在 `bars` 之后）：重新给这四族选源
（拉大**色调**分离度，不只是图案），并给 `plaster` 换成有纹理的源。
**注意：这不阻塞当前阻断点**（族反照率根本没上屏），但它是「校园可辨认」的下一步。

### 8.24 `bars` 族落地（已独立验证）+ 变体全清单

| 项 | 实测 | 目标 |
|---|---|---|
| `bars.png` | 512×512 **RGBA** | ✓ |
| RGB 均值 | **(78.0, 80.0, 82.0)** | (78,80,82)，**偏差 0.0** |
| alpha | `==0` 63.81% / `==255` 36.19% | — |
| **不透明覆盖率** | **0.3619** | **0.36** ✓ |
| **缠绕缝** | **wrap_h 0.00 / wrap_v 0.00** | 应接近 0 ✓（杆与档居中对称，**天然可平铺**）|

**当前族/变体清单**：`ATLAS_FAMILIES` = **24**（末位 `bars`）；`out/families/` **51 张 PNG**
= 24 基族 + **27 变体**（21 颜色 + 5 染色玻璃 + `fabric_gray`）。
QA 对照图已按 S6 建议**移出族目录** → `out/qa/`（`_qa_contact_sheet.png`、
`_qa_variant_contact_sheet.png`、`grey_trio_sheet.png`）。

### 8.25 运动场颜色的完整修复链（还剩 3 步，务必按序）

`engineering-lead-3` 指出一个关键事实：**26/27 个染色变体一个都没进 UE** ——
它们是**数据侧产物**，还没流过 UE 导入管线。**这就是 `sports` 缺陷仍然开放的原因。**
完整链路：

| # | 步骤 | 归属 | 状态 |
|---|---|---|---|
| 1 | 烘焙 `<family>_<colour>` 贴图 | `eng-python-2` | ✅ 27 个已完成 |
| 2 | `family_key()` 接进 `extract_structures` → OBJ 开始发射 `usemtl fabric_lime` | `engineering-lead` | ⏳ **未完成**（这是当前的卡点）|
| 3 | `import_family_materials.FAMILIES` 改 manifest 驱动 → 导入脚本才会拾取变体 | `eng-ue` | ⏳ 未完成 |
| 4 | 重导网格 + `M_MC_<variant>` 材质创建 + 保存关卡 | `eng-ue` | ⏳ 依赖 2/3 |
| 5 | cook + 截图验收 | `engineering-lead-3` | ⏳ 依赖 4 |

**结论：运动场的粉跑道与绿条纹，现在只差第 2、3 步接线。** 在这两步完成前，
任何截图里的球场颜色都不会变 —— **不要把「球场还是橙红+近白」当作新 bug 重复排查。**

### 8.26 一次 commit 卫生事件（处置：不回退历史）

`engineering-lead-3` 的 `9f6621a` 除自己的 docs 外，还扫进了 `tools/split_atlas_to_families.py`
（88 增删）—— 因为**该文件的作者已把自己的改动 `git add` 进 index**，而它用的
`git commit`（不带路径）会提交整个 index。

**处置决定：不回退历史。** 理由：
1. **无数据丢失** —— 该文件作者（`eng-python-2`）在 working tree 里还有**更多未提交改动**
   （而且此刻 `block_families.py` 也在改），它的下一次提交会自然覆盖历史里那个快照；
2. **这是活跃共享仓库、多人在并发提交** —— 对 tip 做 `git reset --soft` 有与队友提交
   **竞态**的风险，代价远大于「历史里多一次快照」；
3. 多出的快照是**工具文件**，不影响构建与产物，且是**前向可修正**的。

**纪律（全员）**：提交必须限定路径 —— `git commit -- <paths>`（或 `git commit -o`），
**不要用裸 `git commit`**，否则会连别人已 staged 的内容一起提交。
`engineering-lead-3` 已自查并承诺改用限定路径；它在前两个 commit（`c319041`、`94c75a6`）
里核对是干净的。

### 8.27 决定性测量：多机位下**绿色全部为 0.0%**（推翻「暖色族为主」的说法）

`eng-ue` 报告「182 个 actor 里绝大多数只解析到 brick/concrete/fabric/quartz/gravel/wood/metal
这些暖色族，**只有 6 个地形瓦片含 grass**」，并据此推断「画面偏褐与族构成本身是暖色族一致」。
**这个前提是错的，而且我已用两个独立测量推翻它：**

**① 地形瓦片 24/24 全部含 `grass`**（我直接解析每个 OBJ 的 `usemtl`）：
```
含 grass 的瓦片数: 24 / 24
materials_by_family: grass quads=277488 tiles=24   ← 82% 的地形面
每瓦片材质组数直方图: {1组:5, 2组:6, 4组:4, 5组:4, 6组:1, 7组:1, 8组:3}
```

**② 4 张多机位截图（含 `buildings`/`tall_block`/`west_band`/`field_axis`）下半帧色相统计**：

| 截图 | 整帧均值 | 绿% | 褐% | 蓝% |
|---|---|---|---|---|
| `stop1_buildings` | (122,119,113) | **0.0** | 100.0 | 0.0 |
| `stop2_tall_block` | (124,121,115) | **0.0** | 82.5 | 9.4 |
| `stop3_west_band` | (127,125,120) | **0.0** | 99.4 | 0.0 |
| `stop4_field_axis` | (133,122,107) | **0.0** | 99.4 | 0.0 |

**若「暖色族为主」是对的，绿色应当是「少」而不是「恰好 0.0%」。** 4 张不同机位全部为 0.0%，
与 `quality-lead-2` 的独立计算（相机视锥内 grass 占 **42–46%**）叠加起来，
**唯一自洽的结论是：族反照率丢失是彻底的，不是取景或族构成问题。**
（`stop0_sports_field` 那张撞了 `ShadowSetup.cpp:1611` 预存断言，是崩溃窗口，无效。）

**③ 顺带确认绑定链已端到端打通**：182 个 actor 的**组件 `GetMaterial()`（绘制路径，优先取
override）全部解析到 `M_MC_<族>`，引用 `M_MC_Atlas` 的 actor = 0** ——
即 §8.12 的 override 修复已生效。**所以损失不在「用哪个材质」，而在「材质怎么求值」。**
这让 §8.20 的 section 探针假设更值得先排除（也可能问题在材质图运行时）。

### 8.28 `leaves`/`bars` 的 masked 材质已落地并断言（`eng-ue`，commit `974b083`）

| 断言 | `M_MC_leaves` 实测 |
|---|---|
| 源 alpha 非全不透明 | `==0` **68.39%** / `==255` 30.85% / 不透明 **31.61%** ✓ |
| `opacity_mask_clip_value` | **0.3330** ✓（磁盘重载读回）|
| `blend_mode` | **`BLEND_MASKED`** ✓ |
| `OpacityMask` 输入节点 | `MaterialExpressionTextureSampleParameter2D` ✓ |
| 编译错误 | 0 ✓ |

**唯一图形改动是「把已有 `TextureSampleParameter2D` 的 `A` 接到 `MP_OPACITY_MASK`」** ——
不新增节点类型，这是本项目的关键风险控制。

`bars`：alpha `==0` 63.81% / 不透明 **36.19%**（要求 30–40% ✓）、clip 0.333、masked、roughness 0.30。
`glass`：**`BLEND_OPAQUE` + roughness 0.10**（规格明确否决玻璃半透明）。
新增断言：**没有 masked 声明的族必须是 `BLEND_OPAQUE`** —— 防止有人误把别族设成 translucent 引入排序伪影。

**24/24 纹理 `TA_WRAP`、24/24 材质编译 ok、5 节点、`RESULT: PASS`。**

### 8.29 一次 commit 污染：**是我自己造成的，不是 `engineering-lead-3`**

我先前把责任归给了 `engineering-lead-3` 的 `9f6621a`（它确实扫进了
`tools/split_atlas_to_families.py` 88 行）。但 `eng-python-2` 随后质询到我头上，我核查后确认：

**我的 `178af62` 扫进了 `tools/block_families.py`（9 行）+ `tools/build_atlas.py`（14 行）——
这正是 `eng-python-2` 暂存在 index 里、准备等 `bars` 图形烘焙完成后一起提交的工作。**

原因与 `9f6621a` 相同：**协作者已 `git add` 进 index，而我用了裸 `git add <我的文件> && git commit`**
—— `git add` 只加我的文件，但 `git commit`（不带路径）**会提交整个 index**。

**处置**：不回退历史（理由见 §8.26，逐字适用）。工作区内容未丢失
（`block_families.py` 仍在改，`bars` 与玻璃变体都已落盘并被验证）。
真相是**两个人各犯了一次同类错误**，而我先前只报了别人那次。

**纪律升级（我自己的）**：**永远用 `git commit -- <paths>`（或 `git commit -o`）限定路径，
绝不用裸 `git commit`。** 我已连续两次踩到，这条对我在这个多人并发仓库里是硬要求。

### 8.30 映射假设的定量排除：**「正确映射」被灾难性排除，两者都指向 `soil`**

用源 OBJ 的**三角形面积加权** × 族贴图实测均值，算各映射假设预测的画面颜色
（`engineering-lead-3` 做了地形侧；我补了结构侧，两侧合起来才有判别力）：

| | `correct` 映射预测 | 实测画面 | 最佳单族匹配 |
|---|---|---|---|
| **地形**（1,353,312 tri，grass 82.0%） | (95,**109**,78) **绿** · `dE76` **25.0** | (105,88,70) | **`soil` (104,90,74) · `dE76` 4.6** |
| **结构**（554,028 tri，concrete 18.4%） | (135,130,127) 浅灰 · **`dE76` 81.7** | (95.5,84.0,71.6) | **`soil` (104,90,74) · `dE76` 10.7** |

**「正确映射」被灾难性排除**：结构侧预测**浅灰**、实测**褐**，`dE76` 高达 **81.7**；
地形侧预测**绿**、实测褐。**两侧都最接近同一个 `soil`。**

这很反常 —— 结构各网格的 `usemtl` 顺序互不相同，
「全都→第 N 个槽」不可能同时让地形和结构都变成 `soil`。
**所以更像是：所有东西都在用同一个材质（`soil` 色）。**

**同时又排除了一层**：我逐个 grep 二进制确认 **24 个 `M_MC_*` 材质各自引用的纹理都是自己那张**
（`M_MC_grass`→`T_MC_grass`、`M_MC_soil`→`T_MC_soil`…，无串号）。
加上网格引用正确（§8.27）与资产槽位正确，**材质定义、资产槽位、网格引用三者都对**
→ **损失只能在绘制 / 运行时求值这一层。**

**下一步判别实验**（已放行，按数据设计）：
- **实验 A**：把 `M_MC_soil` 的 BaseColor 换成**纯品红**（优先只改 `TextureSampleParameter2D` 的
  **贴图指针**，**不加节点** —— 加节点在本项目有静默编译失败前科）。
  **全屏变品红** → 证实「所有东西都在用 soil」；**只有土壤区域变** → 褐色另有来源，转实验 B。
- **实验 B**：再把 `M_MC_grass` 换成棋盘格 → 「草地变」= 贴图到了屏幕；
  「草地不变」= grass 的面根本没被绘制或被别的材质吃了。

**注意实验顺序**：**先 A 后 B**。若「一切皆 soil」成立，先改 grass 会得到**误导性的空结果**。

### 8.31 `-MClayers` 多 token bug 已修（`7d90565`，根因取自引擎源码）

`FParse::Value` 的 FString 重载 `bShouldStopOnSeparator` **默认 `true`**（`Parse.h:71`），
`Parse.cpp:299` 据此把终止符集设为 `",) \r\n\t"` —— **所以值在逗号处就断了**。
修复 = 调用处多传一个 `false`。实测：
```
before  -MClayers=-vox,-struct ->  spec=vox=0 struct=1 terrain=1     ← 第二 token 丢
after   -MClayers=-vox,-struct ->  spec=vox=0 struct=0 terrain=1     ✅
after   -MClayers=-vox         ->  spec=vox=0 struct=1 terrain=1     ✅ 单 token 不受影响
```
**→ 此前所有多 token 的 `-MClayers` A/B 结论一律作废，需重跑**（单 token 的历史结论仍有效）。
`terrain(vis=0)` 失效探测器也修好：`terrain(vis=0)` → **`vis=24`**（24 片瓦片；`col=0` 是对的，
本关卡不放 `C_*` 碰撞网格）。

**方法论教训（值得长期保留）**：**用已知有缺陷的工具做出的测量，必须作废重做，
不能在新结论里沿用。** 「按资产名子串匹配」的 census 在改名时会**静默变谎**，
而「0」会被读成「几何不存在」—— 理想情况下未知前缀应当**报错**而不是报 0。

---
*本文档为唯一权威计划。S0 定架构，S1–S4 已回填，S5 见 §8，S4.5 见 §8.10，S5.5/5.6 见 §8.13–8.31。*
