# MC2UE5 中间格式（层1 → 层3）

本文定义路线C第一阶段·层1（Python 解析层）输出给后续层（体素重建 / UE5 转换）的中间格式。
参考实现：`parse/parse_world.py`；样本：`voxel_data/sample_overworld.bin`。

## 0. 设计目标

- **稀疏**：只输出非空气方块（air / cave_air / void_air 一律丢弃）。空气占绝大多数，
  丢弃后体积下降约 1 个数量级。
- **自包含**：方块状态（name + properties）走一张**全局去重表**，体素流里只放索引。
- **流式可读**：层3 可以只读文件头 + chunk 表 + 需要的 chunk，不必全量载入。
- **紧凑二进制**：定长记录，随机访问友好，避免 JSON 的体积与解析开销。

## 1. 全局方块状态表（Global Block-State Palette）

全维度共用一张表，**唯一键 = (blockName, properties)**。

- `blockName`：如 `minecraft:stone`
- `properties`：规范化字符串，键按字典序排序，`k=v` 用 `;` 连接；无属性时为空串。
  例：`snowy=false`、`type=bottom;waterlogged=false`
- 表本身与体素数据**分离**，单独存一份（本存档共 **1146** 个状态，其中 1013 个带属性），
  已导出为 `parse/block_states.json`（`{name, properties, count}`）。

因为区分了 properties，`minecraft:grass_block{snowy=false}` 与 `{snowy=true}` 是两个不同状态，
层3 不会出现"丢失朝向/材质变体"的问题。

## 2. 体素数据文件（`.bin`）布局

四个段依次排列，偏移全部记录在文件头里，**无需顺序扫描即可随机访问**：

```
[0]      Header      64 B  固定
[64]     Palette     全局状态表（本文件用到的子集）
[..]     ChunkTable  每个非空 chunk 一条记录 + 局部调色板映射
[..]     VoxelStream 每个非空气方块 4 B
```

一个 `.bin` = 一个维度（`dimensionId` 区分），内部可聚合该维度的**全部 region**。

全量导出命令：

```
python3 parse_world.py --save <存档根> --full
# -> voxel_data/full/overworld.bin, nether.bin, end.bin
```

所有整数小端（little-endian）。

### 2.1 Header（64 B）

| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0  | char[8] | magic | `"MC2WV2\0\0"` |
| 8  | u32 | version | 格式版本，当前 `2` |
| 12 | u8  | dimensionId | `0`=overworld `1`=nether `2`=end |
| 13 | u8[3] | reserved | 置 0 |
| 16 | u64 | chunkCount | 本文件 chunk 数 |
| 24 | u64 | voxelCount | 本文件非空气方块总数 |
| 32 | u64 | paletteCount | 本文件调色板条目数 |
| 40 | u64 | paletteOffset | Palette 段起点 |
| 48 | u64 | chunkTableOffset | ChunkTable 段起点 |
| 56 | u64 | voxelDataOffset | VoxelStream 段起点 |

### 2.2 Palette 段

```
u32  count
repeat count:
  u16 nameLen;   u16 propsLen;   u8[nameLen] name;   u8[propsLen] properties
```

### 2.3 ChunkTable 段

每 chunk 一条记录：

```
i32 chunkX        // 绝对区块坐标（可为负）= regionX*32 + 区内 chunkX
i32 chunkZ        // 绝对区块坐标 = regionZ*32 + 区内 chunkZ
u16 paletteCount  // 该 chunk 用到的局部调色板大小
u16 pad
u32 voxelCount    // 该 chunk 的非空气方块数
u64 voxelOffset   // 绝对字节偏移，指向 VoxelStream 中的起点
u32 localToGlobal[paletteCount]   // 局部索引 -> 全局 Palette 索引
```

`chunkX/chunkZ` 是**绝对**区块坐标，因此单个文件可聚合任意多个 region，
层3 不需要知道 region 文件名：

```
worldX = chunkX * 16 + dx
worldZ = chunkZ * 16 + dz
worldY = 见 VoxelStream（已是绝对高度）
```

> **v1 → v2 的两处不兼容改动（务必注意）**
>
> 1. **区块坐标改为绝对值**。v1 存区内坐标 0..31 且依赖 region 文件名去还原，
>    无法把多个 region 合成一个文件。v2 直接存绝对区块坐标。
> 2. **体素字改为存绝对 Y**。v1 只存了 4 bit 的 *section 内局部* Y
>    （`dy = pos//256`），而 `sectionY` 从未被写入文件，
>    导致 **y > 15 的方块高度全部丢失**（本存档实际 y 范围 0..133）。
>    v2 直接存绝对 `worldY`，用 9 bit。
>
> 旧的 `voxel_data/sample_overworld.bin` 是 v1 格式（且 y 被截断），
> 不可与 v2 混用；层3 请只消费 `voxel_data/full/*.bin`。

### 2.4 VoxelStream：每个非空气方块 4 B

用 1 个 u32 同时打包坐标与调色板索引（v2）：

```
bits  0- 3  : dx   (0..15)   chunk 内 X
bits  4- 7  : dz   (0..15)   chunk 内 Z
bits  8-16  : y    (0..511)  绝对世界高度
bits 17-31  : localPaletteIndex (0 .. 2^15-1)
```

即 `(dx) | (dz<<4) | (y<<8) | (localIdx<<17)`。
`localIdx` 先经该 chunk 的 `localToGlobal[]` 映射，再去全局 Palette 取 `(name, properties)`。

选择 4 B 定长而非"每方块一条变长记录"的理由：定长可 `memcpy`/零拷贝映射，
层3 读取与并行分块都最简单；4 B × 3748 万 ≈ **143 MiB**（见第 4 节）。

## 3. 空/特殊情况的约定

- **全空气 chunk 不写入**，chunk 表不含该 chunk（层3 视作空区块）。
- **单色调 section**（`Palette` 只有 1 项且无 `BlockStates`）：按 4096 个非空气方块展开写出。
- **biome**：`Biomes` 为 256 字节（16×16，索引 `z*16+x`）的生物群系 ID 数组，
  本层已解析并统计进 `stats.json` 的 `biome_id_histogram`，
  但**未**写入 `.bin`（体素格式暂不含 biome；如层3 需要，建议按 chunk 追加一个 256 B 段）。
- **TileEntities / Entities**：本层不解析（层3 若需门牌/容器/红石等元数据，另行扩展）。

## 4. 体积估算（基于本存档实测）

实测非空气方块总数 **37,485,404**，因此：

| 范围 | 体素数 | 预估体积 |
|---|---|---|
| 样本（2 region，主世界局部） | 24,456 | **98.5 KiB**（实测 100,826 B） |
| 全量三维度 | 37,485,404 | **≈ 143 MiB** |

调色板与 chunk 表的开销很小：主世界 17,029 chunk 的 chunk 表约
`17029 × (24 + 4×局部调色板)` ≈ 0.5–1 MiB 量级。

结论：**全量导出完全可行**（143 MiB），不存在"上亿方块塞内存"的问题；
但仍建议层3 按 region 粒度流式消费，避免一次性建完整世界体素数组。

## 5. 层3 消费建议

1. 读 64 B header，按 `chunkTableOffset` 定位 chunk 表。
2. 读 Palette 段，得到 `(name, properties)` 数组。
3. 按 chunk 遍历：取 `localToGlobal`，把 `voxelOffset` 处 `voxelCount×4` 字节
   `mmap`/分块读出，展开为 `(worldX, worldY, worldZ, stateIdx)`。
4. `stateIdx -> (name, properties)` 查全局表，转成 UE5 的 FName/Material/FStaticMesh。
5. 需要按 section 切片时按 `sectionY = worldY >> 4` 分桶（每个 section 16³=4096 格）。

参考读取实现见 `scripts/read_sample.py`（不依赖解析器内部状态，独立按本文档解码）。
