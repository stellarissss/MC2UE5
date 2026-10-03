# 层2（资源层）处理说明

路线C 第一阶段 · 层2：为 Minecraft 1.16.5 方块获取默认纹理，并生成供层3（UE5 装配）消费的材质清单。

## 复跑方式

```bash
bash scripts/fetch_mc_assets.sh          # 拉取纹理 + 元数据（幂等）
python3 scripts/build_material_manifest.py # 生成清单 + 覆盖度报告
python3 scripts/verify_material_manifest.py # 独立校验清单可被层3 消费
```

## 产出

| 路径 | 内容 |
|---|---|
| `assets/textures/block/*.png` | 694 张 1.16.5 原版方块纹理 + 41 个 `.png.mcmeta`（动画元数据） |
| `assets/metadata/models/block/*.json` | 1403 个方块模型定义 |
| `assets/metadata/blockstates/*.json` | 764 个方块状态定义 |
| `assets/material_manifest.json` | 315 条材质定义（层3 直接消费） |
| `assets/coverage_report.json` | 覆盖度核对报告 |

## 纹理来源

- 仓库：`https://github.com/InventivetalentDev/minecraft-assets`，分支 `1.16.5`
- commit：`1b7a1d48b0343c0b8da80604598707df428dc32a`（"Create/Update assets for version 1.16.5"）
- 全部经 **`ghfast.top` 镜像**加速；目录为 `assets/minecraft/textures/block`（无 `1.16.5/` 前缀——该仓库分支根目录即版本目录）
- 稀疏检出：`textures/block` + `models/block` + `blockstates`（共 3.0 MB）

## 纹理解析策略（按优先级）

1. **精确解析**（291 条，92.4%）：`blockstates/<name>.json` → 选代表性变体 → `models/block/<model>.json` → 沿 `parent` 链合并 `textures` → 读 `elements[].faces[].texture` 的 `#var` → 解引用为 `block/xxx` → `xxx.png`
2. **仅 textures 无 faces**（23 条）：`water` / `lava` 等只有 `particle`，直接取纹理
3. **启发式**：`<name>_top/_side/_bottom.png`，否则 `<name>.png`
4. **占位纯色**（1 条）：仅 `minecraft:barrier`

代表性变体的挑选优先命中**存档中该方块出现最多的属性状态**（如 `grass_block` 取 `snowy=false` 而非 `snowy=true`），保证清单反映存档实际形态。

## 关键坑（均已修复）

1. **模型路径双前缀**：blockstates 引用 `minecraft:block/grass_block`，直接拼 `models/block/` 会得到 `block/block/grass_block.json` → 全部解析失败静默退化为占位。需先剥 `block/` 前缀。
2. **纹理别名链**：`cube_bottom_top` 父模型的 textures 是 `{"down":"#bottom","up":"#top"}`，面引用 `#down` → 必须**递归解引用**才能拿到真实纹理，否则 `sandstone` 六个面全丢。
3. **多层 element 覆盖**：`grass_block` 有两个 element，第二个是 `#overlay`（草的绿色染色层）。朴素遍历会让 overlay 覆盖真正的侧面，导致 `sideTex` 变成 overlay 图。→ 优先取**完整立方体 element** 且先出现者。
4. **变体挑选按字符串长度**：`snowy=true` 比 `snowy=false` 短，导致 grass_block 选到雪地变体。→ 改用存档实际主状态。
5. **命名空间串味**：`barrier` 的模型引用 `minecraft:item/barrier`，原版 `textures/block/` 里**没有** barrier.png。basename 回退匹配会误命中同名文件。→ 只接受 `block/` 命名空间。
6. **占位纹理自我污染**：脚本重跑时上一轮生成的 `barrier.png` 会被当成原版纹理，使 placeholder 计数假性归零。→ 用 `metadata/generated_placeholders.txt` 记录并在下次运行前清理（幂等）。
7. **稀疏检出的路径前缀**：该仓库按版本分支组织，分支根即版本目录，不存在 `1.16.5/assets/...` 层级。

## manifest 字段说明

```json
{
  "blockName": "minecraft:grass_block",
  "texSource": "model:block/grass_block(variants:snowy=false)",
  "topTex": "grass_block_top.png",
  "sideTex": "grass_block_side.png",
  "bottomTex": "dirt.png",
  "alphaMode": "opaque",
  "emissive": false
}
```

| 字段 | 说明 |
|---|---|
| `alphaMode` | `opaque` / `masked`（alphaTest 裁切）/ `transparent`（半透明混合） |
| `emissive` | 布尔；为 true 时附 `emissiveColor`（近似十六进制色） |
| `texSource` | 溯源信息：`model:` / `model-textures:` / `heuristic:` / `placeholder:` |
| `isPlaceholder` | 仅占位纹理为 true（本清单中仅 `barrier`） |
| `animated` | 纹理带 `.mcmeta`（火焰/水/岩浆帧动画），UE5 侧需取首帧或做 Flipbook |
| `shapeHint` | `non_cube` 表示非立方体几何（台阶/栅栏/门/玻璃板等），层3 需特殊装配 |

## 层3（UE5 装配）注意事项

- **动画纹理**：14 个方块使用竖向帧序列（`water_still.png` 16×512 共 32 帧），需裁首帧或做 Flipbook。
- **非立方体**：133 条带 `shapeHint: non_cube`，不能按标准 1m³ 立方体装配。
- **`barrier`**：原版无方块贴图，当前为占位纯色；若需还原原版半透明黑紫效果应改用程序化材质。
- **染色方块**：草/树叶等在原版依赖 biome tint（`tintindex`）着色，本清单为未染色的基础纹理，颜色需层3 按 biome 复现。
