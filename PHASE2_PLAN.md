# MC2UE5 第二阶段：语义重建（Landscape + 水体 + 物体）

> 状态：**产物已生成并通过离线验证；尚未在 UE 5.5.4 编辑器内运行过。**
> 最后更新：第二阶段建成区全量跑通，`--strict` 零问题，两套测试全绿。

---

## 1. 目标与边界

把 Minecraft Java 1.16.5 存档 `SYFZ_1.16.5` 的 overworld **建成区**重建为
UE5.5 可渲染内容，并与第一阶段的 HISM 方块层共存于同一张关卡、同一套坐标系。

**范围内**

- 自动定位的 overworld 校园建成区（含 8 chunk 边距）
- 地形高度场 → UE5 Landscape
- 水体表面网格 + 半透明材质
- 植被 / 建筑 / 构筑物 / 道具的连通域实例 + 地面拟合
- 全部语义标签以 CPU 规则生成，`SemanticProvider` 可插拔

**范围外（明确不做）**

- 野外地形、nether、end
- 全量 OBJ 网格地形（见 §2）
- 3D U-Net 语义分割（接口已留，默认关闭）
- 内置 recipe 的程序化模型（只输出 placement，不生成空 actor）

---

## 2. 架构决策：为什么是 Landscape 而不是 OBJ

最初方案是导出全量三角网格。按 `upsample=4` 折算，**5.3 亿三角形、数百 GB**，
UE5 无法编辑也无法有效流送。改为 UE5 原生 Landscape 后：

| | OBJ 路线 | Landscape 路线（采用） |
|---|---|---|
| 建成区产物 | 数百 GB | **4.9 MB** |
| 几何 | 5.3 亿三角形 | GPU 插值 + 多级 LOD |
| 编辑 | 不可编辑 | Landscape 模式可直接雕刻 |
| 流送 | 无 | World Partition 原生支持 |
| 精度 | 顶点级 | 1 vertex = 1 block（本项目足够） |

Nanite / 高精网格保留为**可选近景路线**，主路线不依赖它。

---

## 3. 坐标契约（第一、第二阶段共用）

```text
1 Minecraft block = 1 m = 100 UE cm
```

```text
world block (x, y, z)  ->  UE cm (x * 100, y * 100, z * 100)
```

两层共享 `/Game/Maps/MCReplica`，`DIMENSION_Z_OFFSET_CM` 必须与
`import_world.py` 完全一致。任何"导入后再用 actor scale 修正尺寸"的做法都会让
Landscape 与 HISM 方块层产生累积漂移，**明令禁止**。

---

## 4. 建成区范围

`survey.py` 流式扫描全 overworld（只存每列顶部信息，不建稠密体素数组），
按方块名启发式 + 32/64 block 密度格 + `scipy.ndimage.label` 自动定位：

```json
{ "campus": { "x": [-144, 303], "z": [-544, 223], "blocks": [448, 768] } }
```

加 8 chunk 边距后的实际流水线范围：

```text
chunk range = (-17, -42, 27, 22)
block range  = x[-272 .. 447], z[-672 .. 367]
block size   = 720 x 1040
```

---

## 5. Landscape 规格

### 5.1 尺寸合法性

引擎要求：

```text
size            = components * quads_per_component + 1
quads_per_component = section_size * sections_per_component
合法 section_size = 7, 15, 31, 63, 127
```

非法尺寸会导致 section 切分、组件告警与 World Partition 流送异常。
`solve_components()` 枚举求解，并在同等拟合度下**优先更大的 section size**
（`max_components_per_axis=64`），避免过度切分。

### 5.2 建成区最终配置

```text
world blocks      : 720 x 1040
Landscape 顶点    : 745 x 1055
组件              : 24 x 34
quads/component   : 31
sections/component: 1
XY Scale          : 100 cm        (1 vertex = 1 block)
tile              : 1
```

原始 720×1040 无法被 31 quads 整除，多出的 24 / 14 格用**边缘填充**而非缩放
——`resample_to_grid()` 保持原始区域严格 1:1 索引映射，全域缩放会在远端造成
数米漂移。

### 5.3 高度编码（关键，务必读）

UE5 把 16-bit 高度图样本 `v` 先映射为**有符号**局部高度，再乘 Z Scale：

```text
local(v) = (v - 32768) / 128        # -256 .. +255.992
z_cm(v)  = actor_offset_z_cm + local(v) * z_scale_cm
```

因此：

- 满量程 = **512 × z_scale_cm**（不是 z_scale_cm）
- `v = 32768` 恰好落在 actor 的 Z 位置
- 量化步长 = `z_scale_cm / 128` cm

编码端由此推出：

```text
z_scale_cm     = y_span_blocks * 100 / 512
actor_offset_z = y_min_blocks * 100 + 256 * z_scale_cm
```

建成区实际取值：

```text
y_min = 4.0, y_span = 59.0
z_scale_cm     = 11.5234375
actor_offset_z = 3350.0 cm
range          = 5900.0 cm
step           = 0.0900 cm
```

> **除数是 512，不是 65535。** 用 65535 归一化会让地形高出 128 倍，而且
> 在视口里之前完全看不出来。这个错误曾真实存在过（`z_scale` 被写成 `span`
> = 59，误按"Z Scale 是 100 cm 单位"理解），现在由
> `tests/run_tests.py::test_landscape_height_roundtrip` 与
> `tests/test_import_phase2_offline.py` 双重锁定。

唯一真值函数是 `phase2/landscape.py::ue_decode()`，编码端与导入端都调用它，
两侧不可能再各写一份公式而悄悄分叉。

---

## 6. 与第一阶段的共存

| | 第一阶段 | 第二阶段 |
|---|---|---|
| 承载 | HISM 方块层 | Landscape / 水体 / 物体 |
| 关卡 | `/Game/Maps/MCReplica` | 同一张 |
| World Partition cell | 512 | 512（props 256，粒度更细） |
| 资产路径 | `/Game/...` | `/Game/P2/...` |
| 坐标系 | `BLOCK_CM = 100` | 同一常量 |

第二阶段**只增不改**：不删除、不替换任何 HISM actor，不修改第一阶段资产目录。
`/Game/P2/` 的隔离保证重跑不会与第一阶段冲突。

---

## 7. 物体处理

`props.py` 用 6 邻接稀疏 3D 连通域（`scipy.sparse.csgraph.connected_components`）
提取实例，分类为 `tree` / `plant` / `building` / `structure` / `prop`，
逐个做局部地面最小二乘平面拟合与二维协方差 yaw 估计。

建成区结果：

```json
{ "count": 218,
  "by_class": { "tree": 51, "building": 3, "structure": 74,
                "plant": 86, "prop": 4 },
  "ground_aligned": true }
```

性能：20 万体素 → 176,809 连通域约 0.52 s（纯 Python 并查集不可接受，
必须走 scipy）。

模型来源可插拔：`BuiltinModelProvider`（程序化 recipe）与
`LibraryModelProvider`（本地 CC0 模型目录）。**builtin recipe 没有真实 mesh 时
UE5 脚本明确跳过**，绝不生成空 actor 冒充成功。

---

## 8. UE5 集成顺序

```text
1. import_world.py     第一阶段 HISM 方块层 -> /Game/Maps/MCReplica
2. import_phase2.py    Landscape + 水体 + props -> 同一张关卡
```

`import_phase2.py` 顶部开关：

```python
DRY_RUN = True     # 只读校验，不打开/创建关卡，不建资产，不保存
```

`DRY_RUN=True` 是**真正只读**的：不 `load_level`、不 `new_level`、不
`save_dirty_packages`。这一点由 `test_dry_run_never_touches_world` 结构性锁定——
第一阶段尚未验收，dry run 绝不能有替换关卡的风险。

手工 Landscape 导入 fallback（当三条 API 路径都不可用时）：

```text
Landscape 模式 > Import > Import from File
  文件            : out/phase2/overworld/landscape/overworld_00_00.png
  Section Size    : 31 quads
  Sections/Comp   : 1
  Components      : 24 x 34
  XY Scale        : 100
  Z Scale         : 11.5234375
  Actor 位置      : (-27200, -67200, 3350) cm
```

脚本若无法编程导入会**明确报错**并给出上述参数，绝不把 flat Landscape
伪装成成功。

---

## 9. 产物清单

```text
out/phase2/overworld/
├── landscape/
│   ├── overworld_00_00.png     745x1055 16-bit heightmap
│   └── landscape.json          尺寸 / 缩放 / 高度编码元数据
├── water/water.json            6 个液柱，不构成表面 quad，故无 OBJ
├── props/prop_placements.json  218 个实例变换
├── debug/
│   ├── overworld_height_color.png
│   ├── overworld_height_gray.png
│   └── overworld_surface_labels.png
├── heightmap_smooth.npy        （中间态，不入库）
├── terrain.json
└── phase2_report.json
```

建成区完整产物 **4.9 MB**。

---

## 10. 验证状态

| 检查 | 结果 |
|---|---|
| `--strict` 全流程 | 零问题 |
| `tests/run_tests.py` | ALL PASS |
| `tests/test_import_phase2_offline.py` | 46 / 46 |
| `tests/test_quality_tiers.py` | ALL PASS |
| `tests/test_hism_culling.py` | ALL PASS |
| Landscape 尺寸合法性 | 745×1055 = 24×31+1 × 34×31+1 ✓ |
| 高度往返误差 | ≤ 0.0009 blocks（0.9 mm，一个量化步长） |
| 尺度保持重采样 | 原始区域与源数组 `array_equal` ✓ |
| 纯标准库 PNG 解码 | 与 PIL 逐字节一致（264 采样点 0 失配） |
| 篡改检测 | 512× 错误编码被正确拒绝 ✓ |
| UE 5.5.4 编辑器内 | **未验证** |

### 10.1 性能配置（离线已定档，待实测）

本阶段落地的三处性能配置，全部写进了自动化测试——因为它们错的时候
**不报错**，只是帧时悄悄变差：

| 位置 | 配置 | 详见 |
|---|---|---|
| `import_world.py::_add_hism` | 方块层剔除 30000→40000 cm 淡出带，per-leaf 64 | `QUALITY_TIERS.md` §5 |
| `import_phase2.py::_spawn_hism` | 物件按 5 个语义类分别设定剔除距离 | 同上 |
| `import_phase2.py::_apply_landscape_lod` | actor 基线 `lod0_screen_size=1.0`、`lod_blend_range=1.0` | 同上 §5.1 |
| `DefaultScalability.ini` | 两个 Landscape LOD 分布 cvar 按五档差异化 | 同上 §4.2 |

> 剔除距离此前是**完全缺失**的——`~1.6 M` 个 1 m 方块实例一路画到远平面。
> 这是本项目最大的单一性能杠杆，也是本阶段最实质的改动。

---

## 11. 下一步

1. 在 Windows + UE 5.5.4 上以 `DRY_RUN=True` 跑首次校验
2. 核对输出的组件数 / XY Scale / Z Scale 与 §5.2、§5.3 一致
3. 切 `DRY_RUN=False` 正式导入
4. 验证 Landscape 与第一阶段 HISM 精确对齐（抽样比对同一 block 坐标的 Z）
5. 跑 `apply_quality.benchmark()` 后用 `set_tier()` 逐档验证帧时与观感，
   确认 §10.1 的分档假设在真实硬件上成立
6. 保存关卡，打包 Win64
