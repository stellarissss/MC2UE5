# MC2UE5 渲染重制方案（RENDER_PLAN）

> 状态：**执行中**。体素数据已确认可从仓库 LFS 取回（见 §3.1），
> 因此走**体素忠实几何 + 写实材质**路线。
> 起因：v0.7.0 的实机画面（满屏绿色曲面块 + MC 方块贴图）**违背项目目标**，
> 判定为 P0，渲染部分整体重做。
> 关联文档：[`README.md`](README.md)（目标与范围）、[`PHASE2_PLAN.md`](PHASE2_PLAN.md)（层2 契约）。

---

## 1. 目标（重新明确）

**实现 SYFZ MC 地图的 UE 游戏化 = 把 MC 地图「管道化迁移」为 UE 的「真实景观」。**

| 维度 | 来源 | 要求 |
|---|---|---|
| **几何** | MC 体素 | **逐块忠实**：校园由存档里的真实方块构成，不是图元近似 |
| **材质** | 真实世界 | **写实 PBR**：混凝土 / 砖 / 玻璃 / 木 / 瓦 / 土壤 / 植被，带法线与粗糙度 |
| **光照** | 真实世界 | **写实**：Lumen GI、虚拟阴影、真实太阳、曝光与调色 |

**几何形态的判定（关键）**：目标是「**形态忠实于 MC 地图 + 材质光照写实**」，
即业界熟知的「写实化 Minecraft」观感 —— 方块的**形体**保留（那是要复刻的地图），
但每块都以**真实材质**呈现（草是草、砖是砖、玻璃透光），在真实光照下读作
**真实校园**。

> 若改为「把方块形态也抹平成写实建筑轮廓」，那是**生成式重建**（另一个大得多的
> 课题，且会失去"复刻地图"的可核对性）——**不在本方案内**。

**明确不算达标的做法**（v0.7.0 实际就是这些，已作废）：

* 用几何图元当占位：`box` / `cone_on_cylinder` / `post` / `cross_billboard`；
* 直接用 MC 的 16×16 方块贴图铺在几何上；
* 「一个类别一个纯色」的材质区分；
* 忽略数据里已有的真实 `bbox`，用 `radius_cm` 反推尺寸。

---

## 2. 现状诊断（附实测证据，不是猜测）

### 2.1 道具几何是占位图元

`out/phase2/overworld/props/prop_placements.json` 的 1297 个实例，按类别使用：

| 类别 | 数量 | 图元 | 实际观感 |
|---|---|---|---|
| structure | 609 | `box` | 立方柱，**横截面是方的**，但真实 footprint 是 5×2 blocks |
| plant | 601 | `cross_billboard` | 交叉面片 |
| tree | 54 | `cone_on_cylinder` | **平滑圆锥+圆柱** ← 截图里的"圆滑绿丘" |
| prop | 30 | `post` | 柱子 |
| building | 3 | `box` | 立方体，`glass_frac = 0`（没识别出窗） |

`_prop_scale()` 用 `radius_cm` 反推尺寸（`2r` 见方），而数据里**有真实 `bbox`**
（如 `[5, 27, 2]`）被忽略了 —— 所以连"体量"都没还原。

### 2.2 贴图是 MC 方块贴图

`MC_Terrain` 的 BaseColor 来自 `grass_block_top / stone / sand` 三张 **16×16 MC
原始贴图**。16×16 贴图按 block 尺度平铺，必然呈现截图里的马赛克网格；
MC 的 `grass_block_top` 本身还是**灰度图**（靠运行时生物群系染色），
我们只能预乘一个固定绿色，于是整片地面是同一块亮绿。

### 2.3 光照是"能看见"而非"像真的"

DirectionalLight + SkyLight + SkyAtmosphere + Fog 齐全，但没有 Lumen GI、
没有接触阴影、没有曝光/调色，也没有宏观材质变化 —— 所以曲面没有立体感，
暗面被天光染成蓝色（截图里建筑的藏青侧面即为天光照亮的背光面）。

---

## 3. 数据来源（已解决）

### 3.1 体素数据在仓库里 —— 已确认并可取回

原本判定"本机没有原始体素几何"是基于工作副本，**结论错误**。核查 git 历史后确认：

* `voxel_data/full/overworld.bin` 在首次提交 `a21eab9` 中**以 Git LFS 形式入库**，
  在 `ef45e07` 被删除；工作副本里没有，但**对象仍在仓库的 LFS 存储中**。
* LFS 指针：`oid sha256:8dfa378c0f4838a22235b49633d5f253a3dfa32ead808158c70193d75a563621`，
  `size 58720789`（58.7 MB）。

取回方式（本机网络下实测可用）：

1. `git-lfs` 的 batch 端点在 github.com 上被代理阻断，但 **经 `ghfast.top` 可达**：
   `POST https://ghfast.top/https://github.com/stellarissss/MC2UE5.git/info/lfs/objects/batch`
   （带 `Authorization: Basic <base64(user:token)>`），返回带签名的 `href`；
2. `media.githubusercontent.com` 与 `raw.githubusercontent.com` 都**只给指针**，
   不能用 —— 必须走 batch 端点拿签名 URL。
3. 脚本：`Q:\MC2UE5\fetch_voxel.py`（校验 sha256 + size 后才落盘）。

**⇒ 「忠实立面」路线不再被阻塞，本方案按此执行。**

### 3.2 可用的解码设施（已存在，无需新写）

| 设施 | 作用 |
|---|---|
| `parse/INTERMEDIATE_FORMAT.md` | MC2WV2 格式规范（Header 64B / Palette / ChunkTable / VoxelStream） |
| `phase2/voxelio.py` | `VoxelFile`、`ChunkCursor.get(cx,cz)`、`palette_index_of()`、**`build_dense_tile(..., remap=)`** |
| `parse/block_states.json` | 1146 个方块状态（含 properties） |
| `assets/material_manifest.json` | 方块 → 材质清单 |
| `parse/parse_world.py` | 层1 原实现（存档 → MC2WV2），无需重跑 |

`build_dense_tile` 的 **`remap`** 参数正是为"把 1146 个状态压成少数语义类"设计的 ——
这就是方块 → 真实材质的映射入口。

### 3.3 另一条被堵死的路：Landscape（已按用户决定放弃）

UE 5.8 的 Python 没有 Landscape 创建接口（无 `LandscapeEditorSubsystem`
/ `LandscapeEditorObject`），无头流程无法建 Landscape 并导入高度图。
**决策 D4 = 维持运行时 ProceduralMesh。**

---

## 4. 重制方案

### 工作流 A · 地形（写实地表）

1. **几何**：保留运行时 ProceduralMesh + `.u16` 高度场（真实高度，已验证）。
   适度加密（当前 1 顶点 = 4 blocks 的碰撞代理是刻意减面，视觉层可到 1 block）。
2. **材质**：重做 `MC_Terrain` 为**写实地表材质**：
   * 按 **坡度 + 高度 + 噪声** 混合 4 类真实材质层：草皮、裸土、岩石、砾石/路面；
   * **多层多尺度**：远尺度宏观色（消除平铺感）+ 近尺度细节法线；
   * **真实贴图**（CC0）而非 MC 16×16；法线 + 粗糙度 + AO；
   * 消除"马赛克网格"：贴图平铺周期要远大于 block（建议 2–4 m），并加宏观
     色噪声打破周期性。
3. **接受标准**：站在校园里，地面读起来像真实草地/道路，看不出 16×16 网格。

### 工作流 B · 建筑与道具（体素忠实几何）— **核心重做**

**不再使用图元配方。** 直接从 `overworld.bin` 重建校园的**真实方块几何**：

1. **提取**：按 `regions.campus`（x[-144..303] z[-544..223]）取 chunk 范围，
   用 `ChunkCursor.get()` / `build_dense_tile()` 得到密集体素。
2. **面剔除（必须）**：只发射**与空气相邻的面**。整块 6 面会让三角形数爆掉；
   面剔除后只剩外表面，量级可接受。
3. **按材质族合并**：同一材质族的方块合并成一个 mesh（或一个 HISM 批次），
   使 draw call 与材质数可控。材质族由 `remap` 决定（见工作流 A2）。
4. **保留原貌**：门窗、阳台、屋顶、台阶都是存档里的真实方块 —— 这就是"复刻"。
5. **性能**：优先 HISM 分块（`MCReplicaPropCluster` 已有类似机制）+ 视距剔除；
   必要时对远离视线的区域降级为合并静态网格。

预期收益：截图里的"绿色曲面块"变成**真实校舍**（墙是墙、窗是窗），
且**与 MC 存档逐块对应**，可核对。

### 工作流 C · 植被（真实树木）

替换 `cone_on_cylinder`：
* 树干：锥/圆柱但带树皮材质与渐细；
* 树冠：**多团簇球体/面片卡**（不是单个圆锥），按 `bbox` 尺寸；
* 树叶：带 alpha 的卡片 + 次表面散射（SSS），风吹摆动可选；
* 600 个 `plant`：换成真实草/灌木卡片，带 alpha 与 rand yaw/scale。

资源来源见 §5 决策 D2（自建程序化 vs CC0 资产包）。

### 工作流 D · 光照 / 大气 / 后处理（"像真的"的关键）

* 太阳角度与色温（早上/下午的斜射光比正午更真实）；
* **Lumen** 动态 GI + 反射（UE5 默认路径）；
* **接触阴影** + 虚拟阴影贴图；
* **曝光**：手动 EV 或限制自动曝光的 min/max，避免过曝；
* 后处理：Bloom、颜色分级（LUT）、AO、镜头微光；
* 雾：指数高度雾保留，但密度要低，避免压平远景。

### 工作流 E · 管线与验证

* `build_release_level.py` 拆分为：`build_terrain.py` / `build_buildings.py` /
  `build_vegetation.py` / `build_lighting.py`，各自可单独重跑；
* 每个阶段产出**可客观断言**的检查（材质 BaseColor 已连接、网格三角形数、
  实例数与源数据一致、无材质编译失败）；
* 视觉验证沿用 `tools/focus_capture.py`（**不加 `-nullrhi`**）；
* **不再接受"截图看着还行"作为验收**。

---

## 5. 已确认的决策（用户拍板）

| 编号 | 决策点 | **结论** |
|---|---|---|
| **D1** | 原始体素数据 | **在 GitHub 仓库（LFS）里** → 取回后走**逐块忠实**几何（工作流 B） |
| **D2** | 材质/模型来源 | **CC0 资产包**（Poly Haven / Quaternius / Kenney） |
| **D3** | 写实档位 | **写实优先**：Lumen GI + 虚拟阴影 + 真实 PBR + 曝光/调色 |
| **D4** | 地形路径 | **维持运行时 ProceduralMesh** |

### 由 D2/D3 派生的具体要求

* **材质**：真实扫描/贴图类 PBR（含法线、粗糙度、AO），**不是 MC 16×16 贴图**。
* **植被**：CC0 树木/灌木模型 + 带 alpha 的叶簇，替换 `cone_on_cylinder` / billboard。
* **光照**：Lumen（动态 GI + 反射）、虚拟阴影贴图、接触阴影、手动/受限自动曝光、
  Bloom + 颜色分级；雾密度降低以免压平远景。
* **性能基线**：目标机 RTX 4070 12 GB；需实测帧率并记录。

---

## 6. 执行顺序（已开始）

| # | 步骤 | 状态 |
|---|---|---|
| 1 | 取回 `overworld.bin`（LFS，校验 sha256） | 下载中 |
| 2 | **校园体素普查**：方块类型分布、按材质族的体量、面剔除后的面数估算 | 待数据到位 |
| 3 | **材质族映射表**：`remap` 把 1146 状态 → N 个真实材质族 | 待普查结果 |
| 4 | **体素几何构建**：面剔除 + 材质族合并 → C++/Python 构建器 | 待 3 |
| 5 | **CC0 写实材质库**：导入各族的 PBR（albedo/normal/roughness/AO） | 可与 4 并行 |
| 6 | **光照重做**：Lumen + VSM + 曝光 + 后处理 | 可与 4–5 并行 |
| 7 | **植被**：CC0 树木/灌木替换图元 | 待 4 |
| 8 | 打包 + 实机截图验证 + 帧率实测 | 待 4–7 |

**阶段验收（可客观断言，不看"感觉"）**：

* 几何：校园方块数与 `overworld.bin` 在该区域的非空气方块数**一致**；
* 材质：无材质编译失败；每个材质族 BaseColor 有输入；
* 视觉：实机截图出现**可辨认的校舍立面**（门窗、屋顶），不再是统一绿色体块；
* 性能：帧率实测并记录。

---

## 7. 关于「《黄原》污染」

已核查：仓库与记忆中**没有**《黄原》/loess/水沙的任何引用
（`grep -rniE "黄原|huangyuan|loess|黄土|水沙"` 零命中）。

但**设计取向确实是那一类**：`_prop_materials()` 的自述是
「Deliberately plain … separates the classes by hue and left at that」，
`build_release_level.py` 的 props 段落也写着"silhouette 是invented 的"。
这是**沙盒占位思路**（先让画面"有东西"，细节留待以后），
用于技术验证阶段合理，**用于本项目是错的** —— 本项目要的就是观感本身。

该文件自首次提交（`8934902`）即如此，早于本次会话；无论成因如何，
**本次重制把它整体替换掉**。
