# MC2UE5

把 Minecraft Java 版存档重建为 UE5 渲染世界的两阶段管线。

参考论文：*Minecraft to 3D*（SIGGRAPH Posters '25, DOI [10.1145/3721250.3743044](https://doi.org/10.1145/3721250.3743044)）

---

## 这是什么

`SYFZ_1.16.5`（Minecraft 1.16.5 Java 版，seed `156786179664273985`，`was_modded=true`）经两层管线转成 UE5.5 工程：

| 层 | 输入 | 输出 | 状态 |
|---|---|---|---|
| **层1 解析** | region `.mca` 文件 | MC2WV2 体素流 `.bin` + `stats.json` + 方块状态表 | 完成，回归测试全绿 |
| **层2 语义重建** | MC2WV2（只读） | Landscape 高度图 PNG + 水体 + 物体实例 + 元数据 | 完成，回归测试全绿 |
| **UE5 装配** | 上述两者 | World Partition 关卡（HISM 方块层 + Landscape 地形层） | 脚本就绪，**须在装有 UE 5.5.4 的 Windows 机器上执行** |

坐标约定贯穿全流程：**`world block (x, y, z) → UE cm (x·100, y·100, z·100)`**
（Minecraft 约定 1 block = 1 m = 100 cm）。层1 的 HISM 方块层与层2 的 Landscape 地形层
因此共享同一坐标系，不需要任何偏移换算表。

---

## 为什么地形走 UE5 Landscape 而不是静态网格

初版实现按论文字面把体素 heightmap 做 4× 三线性上采样再导出 OBJ。对本存档的
overworld（4320 × 3375 blocks）来说那是 **5.3 亿三角形、约 90 GB 的 OBJ** —— UE5
导不进去，Nanite 也无法流送，且与层1 的 HISM 方块层无法保持 LOD 协同。

论文本身走的就是 **height-map** 路线，而 UE5 Landscape 正是为此设计的：16-bit 灰度
高度图 + GPU 侧双线性插值 + 内建 LOD + World Partition 分区。因此：

- **主输出 = Landscape 高度图**，全量建成区只需 1 张 745×1055 的 PNG（约 700 KB）
- **Nanite 网格 = 可选**（`--mesh`），仅用于近景特写或需要悬垂/洞穴细节的局部
- Landscape 网格**严格 1 顶点 = 1 block**（XY Scale = 100 cm），这样它才能和
  HISM 方块层逐块对齐

实测：建成区完整产出 **4.9 MB**，耗时 1.4 s。

---

## 快速开始

### 0. 环境体检

```bash
python3 scripts/doctor.py
```

检查运行时依赖、第二阶段依赖（scipy / skimage / cv2 / trimesh / PIL）、层1 产物完整性
（`error_count == 0`、`.bin` magic 与尺寸）、以及体素数与 `stats.json` 是否一致。
退出码 0 = 健康。

> 沙箱休眠会回滚 site-packages，`doctor.py` 把这类静默故障变成显式检查项。

### 1. 层1：解析存档

```bash
python3 parse/parse_world.py --save "<存档路径>/SYFZ_1.16.5" --full
```

产出 `voxel_data/full/{overworld,nether,end}.bin` + `parse/stats.json` + `parse/block_states.json`。
本存档实测：19,105 chunks、37,485,404 非空气体素、143.7 MiB。

### 2. 层2a：勘测建成区

```bash
python3 phase2/survey.py --dim overworld
```

单遍流式扫描（1.6 s），按方块列记录「最上方非空气方块」，据此定位人造建筑密集区。
本存档结果：`regions.campus = x[-144..303] z[-544..223]`，即 448×768 blocks 的校园。
全量 1458 万列中仅 3.2% 是人造物 —— 不做这一步就得为大片荒野付出代价。

### 3. 层2b：语义重建

```bash
python3 phase2/run_phase2.py --dim overworld --region campus --min-prop-blocks 6 --strict
```

四个阶段，各自可单独重跑（`--only` / `--skip`）：

| 阶段 | 做什么 | 产出 |
|---|---|---|
| `terrain` | 逐列取地表 → 去阶梯 → Landscape 高度图 | `landscape/*.png` + `landscape.json` |
| `water` | 逐列取最高液面，仅在四角皆有效处发 quad | `water/water.json`（本存档 6 个孤立水柱，无面片） |
| `props` | Union-Find 连通域 → 类别推断 → 地面对齐 → 朝向估计 | `props/prop_placements.json`（218 实例） |
| `debug` | 高程分层色带图 / 灰度图 / 语义标签叠加图 | `debug/*.png` |

`--strict` 让任何退化产出以非零码退出。

### 4. 回归测试

```bash
python3 tests/run_tests.py                      # 主套件
python3 tests/test_import_phase2_offline.py     # 导入脚本离线验证（无需引擎）
```

两套测试，锁住开发中真实出现过的每一个 bug（详见各文件头注释）：

- **层1**：位打包 round-trip、假错误归零、范围统计与 `.bin` 一致
- **层2**：Landscape 高度编码往返、**重采样不缩放世界**、分辨率合法性、XY Scale = 100
- **层2**：连通域（U 形 / 环形 / 3D 壳体）、**地面对齐用世界坐标**、水体网格按需生成顶点
- **导入层**：PNG 解码器与 PIL 逐字节一致、高度解码与编码端互逆、
  篡改编码被拒绝、`DRY_RUN` 结构上不可能碰到关卡、Z Scale 传参链正确

> 最后一条尤其重要：曾经 `_apply_landscape_scale` 被重复定义，
> 后者把 `quads`（31）当 Z Scale 传了出去，地形会高出 512 倍且**不报任何错**。
> 现在有专门的测试守住这条参数链。

### 5. UE5 装配（**须在 Windows + UE 5.5.4 上执行**）

见 [`project/README.md`](project/README.md)，§13 是首次本机验证的分步清单。

---

## 目录结构

```
MC2UE5/
├── parse/
│   ├── parse_world.py           层1：region → MC2WV2
│   ├── block_states.json        1146 个方块状态
│   ├── stats.json               各维度统计（回归测试盯着它）
│   └── INTERMEDIATE_FORMAT.md   MC2WV2 格式规范（层2 唯一契约）
├── phase2/
│   ├── survey.py                建成区勘测（流式单遍）
│   ├── voxelio.py               MC2WV2 读取器（零耦合层1）
│   ├── semantic.py              11 类语义 + 可插拔 provider
│   ├── terrain.py               heightmap 提取 / 去阶梯 / 网格 / OBJ
│   ├── landscape.py             UE5 Landscape 尺寸求解 + 高度编码
│   ├── water.py                 水面提取与平面网格
│   ├── props.py                 连通域 / 分类 / 地面对齐 / 模型 provider
│   └── run_phase2.py            端到端流水线
├── tests/
│   ├── run_tests.py                    回归测试（层1 + 层2）
│   └── test_import_phase2_offline.py   导入脚本离线验证（stub 掉 unreal）
├── scripts/
│   ├── doctor.py                环境体检
│   ├── build_material_manifest.py
│   └── hism_estimate.py         HISM 组件数估算
├── assets/                      material_manifest.json + 736 张方块贴图（LFS）
├── voxel_data/full/             *.bin（143.7 MiB，可重建，不入库）
├── out/
│   ├── survey/<dim>/            勘测产物（中间态，不入库）
│   └── phase2/<dim>/            层2 产物（4.9 MB，入库）
├── PHASE2_PLAN.md               层2 规划与契约（坐标 / 尺寸 / 高度编码）
└── project/                     UE5 工程
    ├── MCReplica.uproject
    ├── Config/
    └── Content/Python/
        ├── import_world.py      层1 → HISM 方块层
        └── import_phase2.py     层2 → Landscape / 水体 / 物体层
```

**Git LFS 分层**：源码、JSON 清单、debug 图与 landscape heightmap 走普通 Git
（可在 GitHub 直接预览、能 diff 核对）；736 张方块贴图与将来的 UE 资产走 LFS。

`voxel_data/`（144 MB 体素导出）与 `out/survey/*.npy`（84 MB 稠密中间数组）
**不入库**：两者都能由 `parse_world.py --full` 与 `survey.py` 从存档一键重建，
而存档本身才是 source of truth。入库只会在存档更新时产生 144 MB 的无意义二进制
diff。需要复现时按 `README.md` §快速开始 的步骤 1、2 重跑即可。
详见 [`.gitattributes`](.gitattributes) 与 [`.gitignore`](.gitignore)。

---

## 设计决策

**层2 只依赖层1 的磁盘契约，不 import 层1 代码。**
`voxelio.py` 按 `parse/INTERMEDIATE_FORMAT.md` 独立实现 MC2WV2 解码器。层1 重构时，
只要磁盘格式不变，层2 继续工作。

**语义分类是可插拔的，不是阻塞项。**
`SemanticProvider` 抽象出 3D U-Net 接口。默认 `RuleSemanticProvider` 是无需训练的
CPU 规则分类器（精确名白名单优先，子串仅兜底 —— 子串匹配曾把 `bedrock` 误判成
`PROP`，因为 `"bed"` 是它的子串）。权重就绪后改一行配置即可切换 U-Net，下游零改动。

**heightmap 路线优先于体素等值面。**
14.5M overworld 体素中 99% 是高度场（dirt/grass/bedrock/sand）；Marching Cubes 会把
埋藏的内部结构全部外化。论文明确写的是 "resample the stepped block surface into a
smooth height-map"。Surface Nets 保留在 `terrain.py` 中，供 overhang 占比高的局部回退使用。

**连通域用 `scipy.sparse.csgraph`，不手写 union-find。**
手写版本在 20 万体素上是 0.5 s 量级且难以正确处理邻接；scipy 一次 C 层遍历搞定。
（第一版手写实现有个真实 bug：基于 lexsort 的邻居扫描会把 U 形和环形撕成两半 ——
当一列里有多个 cell 时「与前一个元素比较」就失效了。）

**不做无谓的预放大。**
Landscape 网格是 1 顶点 = 1 block，所以去阶梯在 block 空间做，而不是先 4× 上采样。
建成区稠密高度图因此只有 3 MB，而非 48 MB。

**Landscape 高度编码只有一个真值函数。**
UE5 把 16-bit 样本 `v` 映射为有符号局部高度再乘 Z Scale：

```text
local(v) = (v - 32768) / 128            # -256 .. +255.992
z_cm(v)  = actor_offset_z_cm + local(v) * z_scale_cm
```

满量程是 **512 × z_scale_cm**，`v = 32768` 恰好落在 actor 的 Z 位置。所以本项目取
`z_scale_cm = y_span × 100 / 512`、`actor_offset_z = y_min × 100 + 256 × z_scale_cm`。

**除数是 512，不是 65535。** 归一化用 65535 会让地形高出 128 倍，而且视口里
看不出来、不报任何错。编码端（`phase2/landscape.py::ue_decode`）与导入端
（`import_phase2.py::_decode_height_cm`）调用同一个公式，两侧不可能悄悄分叉。

**XY Scale 恒为 100，靠数据保证而非事后修正。**
Landscape 尺寸求解时把多余格数用**边缘填充**吸收，而不是缩放网格去迁就组件尺寸；
`resample_to_grid()` 保持原始区域严格 1:1 索引映射。全域缩放会在 700+ block 的
远端造成数米漂移，而 XY Scale 一旦偏离 100，地形就与层1 的 HISM 方块层错位。

---

## 已知限制

1. **未在本沙箱运行过 UE 编辑器。** 沙箱无 GPU、无 UE。`import_world.py` 和
   `import_phase2.py` 都经过语法检查与纯逻辑单元测试（PNG 解码、高度编码往返、
   tile 校验的拒绝行为），但**从未对真实引擎 API 执行过**。
2. **物体模型是「配方」而非文件。** `BuiltinModelProvider` 输出
   `builtin:tree:cone_on_cylinder` 这类图元配方，没有实际网格。接真实 CC0 模型：
   `--models library --model-root <Poly Haven/Kenney/Quaternius 目录>`。
3. **语义分类弱于 U-Net。** 能区分「树 vs 建筑」，不能区分「橡树 vs 白桦」——
   `composition` 里保留了方块名直方图，供下游细化。
4. **建成区只有 6 个孤立水柱**，形不成 2×2 面片，故无水体 OBJ。这是数据实情
   （喷泉/井），不是缺陷；`water.json` 的 `note` 字段记录了这一点。
5. **non_cube 方块按立方体近似**（层1 遗留，见 `project/README.md` §7）。

---

## 环境

Python 3.11，依赖见 `scripts/requirements.txt`：

```
numpy  scipy  scikit-image  opencv-python  trimesh  Pillow  nbtlib  torch(CPU)
```

`torch` 仅在切换到 U-Net provider 时需要；默认规则分类器不依赖它。