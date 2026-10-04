# MCReplica — 在你的 Windows 机器上运行导入与打包

本工程由沙箱（无 GPU、无 UE）生成，**沙箱内没有运行过 UE 编辑器**。
所有资产导入、世界装配、Nanite 构建都必须在你本机装有 **UE 5.5.4** 的机器上完成。

---

## 0. 前置条件（务必先看）

| 项 | 要求 |
|---|---|
| 引擎 | **UE 5.5.4**（与沙箱内 `wukakuki/unreal-engine:dev-5.5.4` 同版本） |
| 显卡 | 需真实 GPU + 渲染上下文（Nanite 网格构建、World Partition 流送都依赖它） |
| 内存 | 建世界峰值很高，见 §5 的量级估算；建议 ≥ 32 GB，系统盘留 ≥ 60 GB |
| 磁盘 | 工程本体 150 MB（voxel）+ 生成的 Content（纹理/材质/关卡）数 GB ~ 数十 GB |
| Python | 编辑器自带，无需另装 |

> **关于 `EngineAssociation`**
> `.uproject` 里写的是 `"EngineAssociation": "5.5"`。如果你用 Epic Launcher 装 5.5.4，
> 这个值通常可以直接匹配。若双击 `.uproject` 弹出「选择引擎」对话框，说明你本机的
> 关联名不同（Launcher 版常见为 `5.5` 或带后缀的自定义名）。两种处理方式：
> 1. 在弹窗里选你的 5.5.4，勾选「不再询问」，UE 会自动改写该字段；
> 2. 或手工编辑 `MCReplica.uproject`，把值改成你本机在
>    `注册表 HKEY_CURRENT_USER\Software\Epic Games\Unreal Engine\Builds` 里
>    对 5.5.4 登记的键名。

---

## 1. 把文件拷到本机

需要拷贝的三块（保持相对布局，脚本按相对路径找数据）：

```
<某个目录>/MC2UE5/
├── assets/                      必需：material_manifest.json + textures/block/*.png (736 张)
├── voxel_data/full/             必需：overworld.bin / nether.bin / end.bin (共 144 MB)
└── project/                     必需：MCReplica.uproject + Config/ + Content/Python/import_world.py
```

把 `MC2UE5` 整个目录拷到本机即可（例如 `D:\MC2UE5`）。

脚本按 `工程目录/..` 向上最多三级去找同时含 `assets/` 和 `voxel_data/` 的目录，
所以只要 `project/` 与 `assets/`、`voxel_data/` 同级摆放就能自动定位。
若想指定别处，设置环境变量 `MC2UE5_ROOT` 指向那个目录即可。

---

## 2. 打开工程

双击 `project\MCReplica.uproject`，或用：

```
"<UE安装目录>\Engine\Binaries\Win64\UnrealEditor.exe" "D:\MC2UE5\project\MCReplica.uproject"
```

首次打开会提示「启用插件」/「编译模块」——`.uproject` 里 `Modules` 为空（纯 Python 工程，
无 C++ 源码），因此**不会**有编译步骤。`PythonScriptPlugin` 已在 `.uproject` 中启用。
若编辑器提示缺少模块，选「是」让它重新生成项目文件即可。

> 不要用 `-game` 启动，也不要用 packaged exe：脚本需要编辑器上下文。

---

## 3. 运行导入脚本

### 3.1 第一次务必先 DRY_RUN

打开 `project\Content\Python\import_world.py`，确认顶部：

```python
DRY_RUN = True
```

`DRY_RUN=True` 只统计、不建任何资产，几秒~几十秒就能跑完，并在
`project\Saved\MC2UE5\dryrun_<维度>.txt` 写报告，同时打印到 Output Log。
先看报告里的 `total instances` / `HISM components if built` 是否与 §5 一致。

### 3.2 方式一：编辑器 Python 控制台（推荐，可看实时日志）

菜单 `Window（窗口）→ Output Log（输出日志）→ 切到 Python Console`，输入：

```python
import import_world; import_world.run()
```

要改常量先在编辑器里改文件，再 `import import_world` 重新导入；
若报缓存，用 `import import_world; import import_world.reload()` 或重启编辑器。

### 3.3 方式二：命令行执行

```
"<UE安装目录>\Engine\Binaries\Win64\UnrealEditor.exe" ^
    "D:\MC2UE5\project\MCReplica.uproject" ^
    -ExecutePythonScript="D:\MC2UE5\project\Content\Python\import_world.py"
```

（PowerShell 用反引号 `` ` `` 续行。脚本路径建议给绝对路径。）
编辑器会启动、执行脚本；进度看 `project\Saved\Logs\MCReplica.log`。

### 3.4 真正建世界

看完报告、确认数字无误后，把 `DRY_RUN = False`，用同样任一方式再跑一次。
**这一步很慢**（见 §5），建议：

- 先只跑一个维度验证：`IMPORT_DIMENSIONS = ["overworld"]`；
- 内存吃紧就把 `CELL_SIZE` 降到 `256`（组件数约 ×3~4，但每 cell 更轻）；
- 中断后重跑会复用已存在的材质/网格，但已建 actor 不会自动清理——
  想重来请先删掉 `Content\Maps\MCReplica.umap` 与 `Content\__ExternalActors__`。

---

## 4. 脚本做了什么（装配策略）

| 环节 | 做法 |
|---|---|
| 关卡 | `LevelEditorSubsystem.new_level("/Game/Maps/MCReplica", is_partitioned_world=True)`，创建即 World Partition + OFPA |
| 网格 | 复制引擎自带 `/Engine/BasicShapes/Cube`（正好 100uu = 1m = 1 格）到 `/Game/Meshes/Cube1x1x1` |
| Nanite | `StaticMeshEditorSubsystem.get_nanite_settings` → `enabled=True`，`fallback_target=Relative_Error`、`keep_percent_triangles=1.0` |
| 纹理 | 736 张 PNG 批量 `AssetImportTask` → `/Game/Textures/Block/<name>`，Nearest / 无 mip / 不压缩 |
| 材质 | 每种方块一个 `MaterialInstanceConstant` → `/Game/Materials/Block/MC_<name>`，父材质 `MC_BlockMaster` |
| 六面贴图 | 母材质用世界法线 Z 分量混合三张贴图：`top=+Z`、`bottom=-Z`、`side=四壁`；每实例用 TopTex/SideTex/BottomTex 纹理参数覆盖 |
| 混合模式 | manifest `opaque`→Opaque，`masked`→Masked(+OpacityCutoff 0.5)，`transparent`→Translucent；11 个自发光块额外设 `EmissiveColor` |
| 分组 | 按 **(cellX, cellZ, blockName)** 分组；`CELL_SIZE=512`（格） |
| Actor | 每个空间 cell 一个 `Actor`，该 cell 内所有方块类型的 HISM 都挂在它上面 → WP 以 cell 为单位流送 |
| 实例 | `HISM.add_instances(transforms, False, True)`，世界空间；方块中心 = `(x+0.5, y+0.5, z+0.5) * 100cm` |
| 拆分 | 单组 > `MAX_INSTANCES_PER_HISM=500000` 时拆成多个 HISM（下界一格有 ~550 万 netherrack，不拆会炸） |
| 维度 | 三个维度 XZ 原点重叠，脚本按 `DIMENSION_Z_OFFSET_CM` 上下错开 ±200 km，避免互相穿插 |
| 内存 | 位置以 6 字节/块打包缓存（`struct <HHH`），不用 Python 元组，避免 3700 万个元组吃掉数 GB |

**关于 Nanite 与 HISM 的取舍**：Nanite 网格一旦开启，**实例化渲染路径会退化**
（UE 会提示 Nanite 网格被用于 ISM/HISM 时不使用 Nanite 剔除）。对 3700 万个 12 面方块来说，
真正的收益来自 HISM 的层级 LOD + 每组件一次绘制，而非 Nanite。所以脚本仍然开启 Nanite
（对齐需求、且未来把方块合并成非实例网格时有用），但**不要指望它是性能来源**。
若追求极致，可 `ENABLE_NANITE = False`，改用普通 LOD 网格 + HISM。

---

## 5. 量级估算（本存档实测）

`scripts/hism_estimate.py` 直接扫 `voxel_data/full/*.bin` 统计（沙箱已跑）：

| 维度 | 体素数 | 文件 | 空间 cell | HISM 组件 |
|---|---|---|---|---|
| overworld | 14,513,150 | 56.0 MiB | 34 | 654 |
| nether | 19,585,776 | 74.8 MiB | 6 | 207 |
| end | 3,386,478 | 12.9 MiB | 10 | 69 |
| **合计** | **37,485,404** | **143.7 MiB** | 50 | **930** |

- 组件数 = Σ `ceil(每组实例数 / 500000)` ≈ 930（`CELL_SIZE=512`）。
- `CELL_SIZE=256` 时 cell 数约 ×4、组件数约 ×2.5~3（约 2.5–3k），流送更平滑但更慢。
- 内存：HISM 实例缓冲大致每实例数十字节～百字节量级，3700 万实例**很吃内存**，
  这是本方案最大的风险点。分维度跑（`IMPORT_DIMENSIONS` 只留一个）是有效缓解。

---

## 6. 打包 Win64

### 6.1 用现成脚本（本机 Windows / Windows 容器）

`/workspace/package_win64.sh` 是给 **Linux 容器** 用的（挂载工程到镜像里跑 `RunUAT.sh`）。
在 Windows 上跑它需要 bash 环境（Git Bash / WSL），且脚本内写死了容器镜像，
**不建议直接用**。Windows 上请直接用下面的等价命令。

### 6.2 等价的 Windows 命令（推荐）

```
"<UE安装目录>\Engine\Build\BatchFiles\RunUAT.bat" BuildCookRun ^
    -project="D:\MC2UE5\project\MCReplica.uproject" ^
    -platform=Win64 -clientconfig=Shipping ^
    -cook -build -stage -pak -archive ^
    -archivedirectory="D:\MC2UE5\project\Packaged\Win64"
```

产物在 `project\Packaged\Win64\`。

> **为什么不能在本 Linux 沙箱出 Win64**：UE 5.5 已移除原生 Linux→Win64 交叉编译
> （UBT 的 Windows 工具链发现被 `BuildHostPlatform.Current.Platform == Win64` 门控，
> 镜像内也没有 MSVC/Windows SDK）。详见 `UE5_WIN_CROSSCOMPILE.md`。所以 Win64 必须在
> Windows 上出，或者在 Windows 容器里跑上面的 RunUAT。

### 6.3 打包前注意

- 打包前确保世界已建好并保存（`Content\Maps\MCReplica.umap` + `__ExternalActors__`）。
- Cook 阶段会按 World Partition 网格切包，3700 万实例 cook 可能耗时数小时。
- 想先验证链路，可先只 cook overworld 一个维度。

---

## 7. 已知限制（本版）

1. **non_cube 方块按立方体近似**——133 种（台阶/楼梯/门/栅栏/工作台等）本版都画成整方块，
   形状、朝向、half（下半格）全部丢失。`material_manifest.json` 里有 `shapeHint: non_cube` 标记，
   后续可据此换成对应模型。
2. **biome tint 未染色**——草方块/树叶的 biome 着色（草绿/森林绿/雪原…）没做，
   草和树叶用的是原贴图基色。层1 已把 biome ID 统计进 `stats.json` 但没写进 `.bin`，
   要染色需扩展层1 在 chunk 表追加 256 B biome 段。
3. **帧序列纹理只取首帧**——water/lava 等是 16×N 的动画贴图，本版只导入单帧，不播放动画。
4. **方块状态被压平成方块名**——`block_states.json` 有 1146 个状态（含 properties），
   但装配按 `blockName` 归组，所以 `snowy=true/false`、`facing` 等变体共用同一材质。
   材质外观无差异，但若后续要按朝向旋转方块，需要回到状态级别。
5. **无碰撞体**——脚本建的 HISM 没生成物理碰撞（`use_simple_as_complex` 等未配置），
   目前只能看不能走。
6. **材质图在脚本里构建**——六面混合的母材质图由 Python 拼装，是全脚本里最脆的部分。
   若日志出现「face-blend graph could not be built」，材质会退化为默认外观，
   但**几何与实例化仍然正确**，可手工在材质编辑器里补图（参数名 `TopTex`/`SideTex`/`BottomTex`）。
7. **World Partition cell size 可能需手设**——`unreal.World` 在 5.5 没有暴露
   `world_partition` 属性，脚本会尝试按路径拿 WorldPartition 对象并设 `cell_size`；
   拿不到就在日志里提示，请手工在 `World Settings → World Partition` 设为
   `51200 cm`（= 512 格 × 100 cm）。
8. **未在真机验证**——沙箱无 GPU/无 UE，以上脚本仅通过 `py_compile` 与离线逻辑测试
   （`scripts/test_import_world.py`，21 项，含对真实 nether.bin 的全量解码），
   编辑器相关分支（资产创建/HISM/流送）**未执行过**，首跑请留日志。

---

## 8. 复现（沙箱侧，可重跑）

```bash
# 1) 全量导出三维度体素（已执行过，约 3.5 s）
python3 parse/parse_world.py --save "<存档根>" --full --jobs 16
#    -> voxel_data/full/{overworld,nether,end}.bin

# 2) 统计 HISM 组件数（可选，与编辑器 DRY_RUN 结果应一致）
python3 scripts/hism_estimate.py --cell 512 --max-per-hism 500000

# 3) 离线测试导入脚本的纯 Python 逻辑
python3 scripts/test_import_world.py

# 4) 语法检查
python3 -m py_compile project/Content/Python/import_world.py
```

依赖：`nbt==1.5.1`、`numpy>=1.24`（见 `scripts/requirements.txt`）。

> **格式提示**：旧的 `voxel_data/sample_overworld.bin` 是 v1 格式且 y>15 的高度已丢失，
> 已废弃不可用。层3 只消费 `voxel_data/full/*.bin`（MC2WV2）。

---

# 第二阶段：语义重建层（Landscape + 水体 + 物体）

本节对应仓库根目录的 `phase2/`，产出由 `Content/Python/import_phase2.py` 装配。
**装配顺序建议：先跑完 `import_world.py`（HISM 方块层），再跑 `import_phase2.py`
（Landscape 地形层）** —— 两者写进同一个 World Partition 关卡、共享同一坐标系，
但地形层必须在方块层之后跑，这样视口里能立刻看到「地形托住方块」的效果。

## 9. 先跑 DRY_RUN

打开 `project\Content\Python\import_phase2.py`，确认顶部：

```python
DRY_RUN = True
DIMENSION = "overworld"
USE_EXISTING_LEVEL = True
LEVEL_PATH = "/Game/Maps/MCReplica"
```

### 9.1 DRY_RUN 是真正只读的

`DRY_RUN=True` 时脚本**不会**：

- 打开或创建任何关卡（不 `load_level`、不 `new_level`）
- 创建任何资产（不 spawn actor、不建材质）
- 保存任何 package（不 `save_dirty_packages`）

它只读取 `out/phase2/<dim>/` 下的元数据与 PNG 并打印一份计划。
这一点很重要：第一阶段尚未验收，dry run 绝不能有替换 `MCReplica` 关卡的风险。
该行为由 `tests/test_import_phase2_offline.py::test_dry_run_never_touches_world`
结构性锁定。

### 9.2 DRY_RUN 会做的校验

`DRY_RUN=True` 会做这些事：

- 读 `out/phase2/overworld/landscape/landscape.json`
- 校验尺寸是否满足 `components × quads + 1`（UE5 的硬约束，不满足会拒绝放置）
- 校验 `xy_scale_cm == 100.0`（与层1 方块层对齐的必要条件）
- **抽样扫描整张 PNG**（约 64 行），按 UE5 真实解码公式验证高度落在记录范围内
- 检查目标关卡是否存在（只报告，不打开）
- 打印水体与物体的统计

> 为什么值得多花这几十毫秒：UE5 的 Landscape 高度解码是
> `z_cm = offset + (v - 32768) / 128 × z_scale_cm`（除以 **128**，不是 65535；
> 满量程 = **512 × z_scale_cm**）。编码错一个系数，地形会整体高出 128 倍
> 而**不报任何错**。抽样扫描能在进引擎前抓住它。

抽样是**跨全图**而非只看四角：本存档的建成区是台地，四角高度都是 y=4，
只看四角的话，一张上下颠倒的高度图也能通过检查。

确认无误后改成 `DRY_RUN = False` 再跑。

## 10. 产物对应关系

| phase2 产物 | UE5 里的样子 | 包路径 |
|---|---|---|
| `landscape/overworld_00_00.png` | Landscape actor（745×1055 顶点） | `/Game/P2/Landscape/` |
| `water/water_overworld.obj` | StaticMeshActor + 半透明材质 | `/Game/P2/Water/` |
| `props/prop_placements.json` | 每类一个 HISM（按 256 格空间 cell 分组） | `/Game/P2/Props/` |

**本存档实测（建成区 720×1040 blocks）**：

- Landscape：745×1055 顶点，24×34 组件 @31 quads，XY Scale = 100 cm
- 高度范围 y = [4, 63] blocks，跨度 59 blocks
- **Z Scale = 11.5234375**，满量程 = 512 × 11.5234375 = 5900 cm = 59 blocks
- Actor 位置 = `(-27200, -67200, 3350)` cm，Scale3D = `(100, 100, 11.5234375)`
- 物体：218 实例（tree 51 / plant 86 / structure 74 / building 3 / prop 4）
- 水体：6 个孤立水柱，形不成 2×2 面片 → 无网格（数据实情，非缺陷）

> ⚠️ Z Scale 是 100 cm 单位还是厘米，取决于引擎的 Transform 面板语义。
> 本项目按 **厘米** 处理（`z_scale_cm = span × 100 / 512`）。若你在
> UE 5.5.4 里看到地形高度不对，第一个要核对的就是这个值：它必须让
> `512 × Z Scale` 恰好等于 `y_span × 100` cm。

## 11. 已知限制（第二阶段）

1. **Landscape 高度导入依赖引擎版本。** 脚本依次尝试
   `LandscapeEditorObject.import_height_data` → `LandscapeSubsystem.import_heightmap_from_file`
   → `LandscapeEditorSubsystem.import_heightmap_from_file`。
   若三者都不可用，脚本会**明确报 `needs_manual_import`**，并打印手工导入
   所需的全部参数（Section Size / Components / XY Scale / Z Scale / Actor 位置），
   **不会**静默留下一个平地冒充成功。
   若遇到这种情况请告知我具体引擎版本，我按该版本 API 调整。

   手工导入路径：`Landscape 模式 > Import > Import from File`，参数见上表。

2. **物体模型是配方不是文件。** `builtin:tree:cone_on_cylinder` 这类没有实际网格，
   脚本会跳过并提示。接真实模型：
   ```bash
   python3 phase2/run_phase2.py --dim overworld --region campus \
       --models library --model-root D:\cc0_models
   ```

3. **只做了 overworld 建成区。** nether / end 未跑第二阶段（景观完全不同：
   熔岩湖 / 末地浮岛）。命令一样，换 `--dim` 即可；先跑各自的 `survey.py` 定范围。

4. **水体材质是最简半透明面。** 深度渐变需要渲染目标或 SceneDepth，那是美术活不是数据导入。

5. **尚未在真实编辑器中验证。** 脚本经过离线验证（PNG 解码与 PIL 逐字节一致、
   高度往返误差 ≤0.9 mm、篡改检测有效），但从未在 UE 5.5.4 里执行过。
   §13 给出首次本机验证步骤。

## 12. 第二阶段的沙箱侧复现

```bash
# 1) 环境体检
python3 scripts/doctor.py

# 2) 勘测建成区（单遍流式，约 1.6 s）
python3 phase2/survey.py --dim overworld

# 3) 语义重建（约 1.4 s，产出 4.9 MB）
python3 phase2/run_phase2.py --dim overworld --region campus \
    --min-prop-blocks 6 --strict

# 4) 回归测试（锁住开发中真实出现过的 bug）
python3 tests/run_tests.py

# 5) import_phase2 离线验证（用真实产物，stub 掉 unreal）
python3 tests/test_import_phase2_offline.py

# 6) 画质分级的引擎契约（阈值位置、@Cine 段名、100% 钳制、启动默认档）
python3 tests/test_quality_tiers.py

# 7) HISM 剔除与地形 LOD 配置（淡出带、按类分档、降级不中断导入）
python3 tests/test_hism_culling.py

# 8) 语法检查
python3 -m py_compile project/Content/Python/import_world.py
python3 -m py_compile project/Content/Python/import_phase2.py
python3 -m py_compile project/Content/Python/apply_quality.py
```

`test_import_phase2_offline.py` 是**不需要引擎**的那一半：它用真实的
`landscape.json` 与 heightmap PNG 验证纯标准库 PNG 解码器（与 PIL 逐字节比对）、
高度解码与编码端互为逆运算、篡改的编码会被拒绝、`DRY_RUN` 不可能碰到关卡。

`test_quality_tiers.py` 与 `test_hism_culling.py` 同样是离线的，但它们守的是
另一类 bug——**配置看起来完全合理、引擎却根本不读**的那一类。举三个真实
踩过的例子：

- `PerfIndexThresholds_*` 写在 `DefaultDeviceProfiles.ini` 里，引擎完全忽略
  （只在 `DefaultScalability.ini` 的 `[ScalabilitySettings]` 段被读）；
- 顶层档写成 `[ViewDistanceQuality@4]`，引擎只打开 `@Cine` 段；
- `sg.ResolutionQuality=150`，被引擎在 100 处静默钳制。

这三处都不会报错，只是让分级系统**不起作用**。本机装 UE 之前先跑这四套
测试，能省掉一轮往返。

## 13. UE 5.5.4 首次本机验证步骤

按顺序做，每步都有明确的通过判据。**不要跳过第 1 步。**

### 13.1 Dry run（只读，安全）

1. 打开 `project\Content\Python\import_phase2.py`，确认 `DRY_RUN = True`
2. 编辑器 Python 控制台（`Output Log > Python Console`）执行：
   ```python
   import import_phase2; import_phase2.run()
   ```
   或命令行：
   ```cmd
   UnrealEditor.exe MCReplica.uproject ^
       -ExecutePythonScript="Content/Python/import_phase2.py"
   ```
3. **通过判据**：输出里每个 tile 都出现一行
   ```text
   overworld_00_00.png 373x528  sampled y=[...] blocks  OK
   ...
   DRY_RUN: 4 tile(s) validated, nothing spawned
   ```
   四行 tile 全是 `OK`，且 `DRY_RUN` 计数等于 4。
   `sampled y` 落在 `[4, 63]` 方块区间内——**只有 4 个 tile 合起来
   至少有一块存在真实起伏**才算通过：分块后单块可能恰好是平地，
   这是正常的（见 `tests/test_import_phase2_offline.py`）。
4. 关掉编辑器，确认**磁盘上没有任何新资产**——dry run 不该产生任何 `.uasset`。

### 13.2 正式导入

1. 确认第 1 步无误后，把 `DRY_RUN` 改为 `False`
2. 再次执行 `import_phase2.run()`
3. **通过判据**：输出里 `heightmaps_imported` 等于 `tiles`，即 **4**。
   如果是 0，会明确打印 `needs_manual_import` 以及手工参数，按 §10 的表填写。

### 13.3 对齐验证（最关键的一步）

地形和方块层必须严格对齐。校园被切成 **2×2 共 4 个 Landscape actor**
（每块 360×520 方块、373×528 顶点、12×17 component @ 31 quads），
这样每个 actor 能独立流送与剔除；单个巨型 Landscape 做不到这点。

抽查三个已知地物：

| 检查 | 期望 |
|---|---|
| Landscape actor 数量 | **4**（`overworld_00_00` / `00_01` / `01_00` / `01_01`） |
| 各 actor 位置 (X, Y) | `(-27200, -67200)`、`(8800, -67200)`、`(-27200, -15200)`、`(8800, -15200)` cm |
| 各 actor Scale3D | `(100, 100, 11.5234375)`，四块**完全一致** |
| 校园围墙（地图坐标约 x∈[-144,303], z∈[-544,223]） | 墙体方块应贴在地面上，不悬空、不陷入 |
| 操场与中轴道路 | 道路低于周边草地，与 debug 图 `overworld_height_color.png` 一致 |
| 4 块之间的接缝 | 无错位、无重叠、无可见台阶 |

最容易出错的是 **XY Scale 被改动**。若你看到地形整体缩放或偏移，
检查 Scale3D 的 X/Y 是否仍是 100；**不要用移动 actor 的方式去对齐**，
那会让地形与 HISM 方块层产生累积漂移。

四块必须用**同一套** Scale3D。任何一块的 Z Scale 不同，接缝处就会出现
台阶——`import_phase2.py` 逐块读取各自的高度跨度来算 Z Scale，所以
`landscape.json` 里每个 tile 都有 `height_cm_meta`；若某块的地形高度
跨度与其它块差异极大，值得回头查 `phase2` 的分块是否切在了异常地形上。

### 13.4 画质分级验证（本次新增，沙箱无法覆盖）

五档系统的配置正确性已由 `tests/test_quality_tiers.py` 与
`tests/test_hism_culling.py` 保证（引擎契约、剔除带、启动默认档），
但**帧率与观感必须在本机实测**。完整设计见 `../QUALITY_TIERS.md`。

先在编辑器 Python 控制台确认配置被正确读到：

```python
import apply_quality
apply_quality.report()      # 应打印出 adapter / benchmark index / 启发式档位
apply_quality.benchmark()   # 跑一次引擎基准（约几秒，会卡顿），然后自动应用
apply_quality.report()      # 这次 benchmark index 应不再是 None
```

逐档检查（`apply_quality.set_tier("Low")` … `"Cinematic"`）：

| 检查项 | 判据 |
|---|---|
| Low 档帧率 | 在目标弱机（集显）上 ≥ 30 fps |
| Medium 档帧率 | GTX 1650 级别 ≥ 45 fps @ 1080p |
| `r.Nanite` | **每一档都应为 1**（用户明确决定：低配保留 Nanite） |
| HISM 剔除 | 走出约 400 m 时方块应**淡出消失**，不是突然硬 pops |
| 低配档视距 | 0.4 缩放后仍能看到约 160 m，**不应出现空洞或缺面** |
| 打包后启动 | 全新安装、未跑基准时，**默认落在 Medium 而非 Epic** |
| 手动锁定 | `set_tier("High")` 后重启仍为 High（持久化生效） |
| 超采样 | `set_supersampling(150)` 生效；切档**不会**改动它 |

打包后请额外确认：全新安装（删掉 `%LOCALAPPDATA%` 下的
`Saved/Config` 与 `~/.mcreplica/quality.json`）首次启动落在 Medium 档。
这是「未知硬件的安全兜底」是否真的生效的唯一检验。

### 13.5 保存与打包

1. `File > Save All`（或让脚本的 `save_level()` 执行）
2. 按 §6 打包 Win64

### 13.6 出问题时

把以下信息发我，我能直接定位：

- UE 的完整版本号（`Help > About`）
- Output Log 里 `import_phase2` 的全部输出
- 若报 `needs_manual_import`，说明三条 API 路径都失败了，需要按版本调整
- 视口截图（尤其是地形与方块层交界处）
