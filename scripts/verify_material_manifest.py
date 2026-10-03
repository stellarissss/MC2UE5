#!/usr/bin/env python3
"""
校验 material_manifest.json 是否可被层3 直接消费：
  1. JSON 格式合法、必需字段齐全
  2. 所有引用纹理真实存在于 assets/textures/block/
  3. 所有纹理可被 PIL 打开，且尺寸合理（16x16 或动画序列）
  4. 覆盖度：用 stats.json 实际出现的 blockName 核对
  5. 抽查若干已知方块的面纹理是否符合原版（回归断言）
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
TEX_DIR = os.path.join(ASSETS, "textures", "block")
MANIFEST = os.path.join(ASSETS, "material_manifest.json")

REQUIRED = ("blockName", "topTex", "sideTex", "bottomTex", "alphaMode", "emissive")
ALPHA = {"opaque", "masked", "transparent"}

errors = []
warnings = []

# ---- 1. 格式与字段
try:
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
except Exception as e:
    print("FATAL: manifest 无法解析: %s" % e)
    sys.exit(1)

if not isinstance(manifest, list):
    errors.append("manifest 顶层不是数组")

seen_names = set()
for i, e in enumerate(manifest):
    for k in REQUIRED:
        if k not in e:
            errors.append("#%d 缺字段 %s" % (i, k))
    if e.get("blockName") in seen_names:
        errors.append("#%d blockName 重复: %s" % (i, e.get("blockName")))
    seen_names.add(e.get("blockName"))
    if e.get("alphaMode") not in ALPHA:
        errors.append("#%d alphaMode 非法: %s" % (i, e.get("alphaMode")))
    if not isinstance(e.get("emissive"), bool):
        errors.append("#%d emissive 非布尔: %s" % (i, e.get("emissive")))
    if e.get("emissive") and "emissiveColor" not in e:
        warnings.append("#%d emissive=true 但无 emissiveColor: %s" % (i, e.get("blockName")))
    for k in ("topTex", "sideTex", "bottomTex"):
        v = e.get(k)
        if v and not v.endswith(".png"):
            errors.append("#%d %s 非 .png: %s" % (i, k, v))

print("字段校验: %d 条, 错误 %d, 警告 %d" % (len(manifest), len(errors), len(warnings)))

# ---- 2. 纹理文件存在性 + 3. 可打开性/尺寸
from PIL import Image

bad = []
dims = Counter()
for e in manifest:
    for k in ("topTex", "sideTex", "bottomTex"):
        fn = e.get(k)
        if not fn:
            bad.append((e["blockName"], k, fn, "空"))
            continue
        p = os.path.join(TEX_DIR, fn)
        if not os.path.exists(p):
            bad.append((e["blockName"], k, fn, "文件不存在"))
            continue
        try:
            with Image.open(p) as im:
                im.load()
                dims[im.size] += 1
        except Exception as ex:
            bad.append((e["blockName"], k, fn, "无法解码: %s" % ex))

print("纹理文件校验: 引用总数 %d, 异常 %d" % (len(manifest) * 3, len(bad)))
print("  纹理尺寸分布(前6): %s" % dims.most_common(6))
nonstandard = [(s, c) for s, c in dims.items() if s[0] != 16]
if nonstandard:
    warnings.append("非 16 宽纹理(动画序列): %s" % nonstandard)

# ---- 4. 覆盖度
stats = json.load(open(os.path.join(ROOT, "parse", "stats.json"), encoding="utf-8"))
used = set()
for dim in (stats.get("dimensions") or {}).values():
    for item in (dim.get("top30_block_types") or []):
        used.add(item["name"])
bs_states = json.load(open(os.path.join(ROOT, "parse", "block_states.json"), encoding="utf-8"))
all_names = set(s["name"] for s in bs_states["states"])

uncovered_used = sorted(used - seen_names)
uncovered_all = sorted(all_names - seen_names)
print("覆盖度: 存档实际用到 %d, 已覆盖 %d, 未覆盖 %d"
      % (len(used), len(used & seen_names), len(uncovered_used)))
print("  block_states 全集 %d, 未覆盖 %d" % (len(all_names), len(uncovered_all)))
if uncovered_used:
    errors.append("存档用到的方块未覆盖: %s" % uncovered_used[:10])

# ---- 5. 回归断言（对照原版已知结果）
EXPECT = {
    "minecraft:stone":            ("stone.png", "stone.png", "stone.png"),
    "minecraft:grass_block":      ("grass_block_top.png", "grass_block_side.png", "dirt.png"),
    "minecraft:sandstone":        ("sandstone_top.png", "sandstone.png", "sandstone_bottom.png"),
    "minecraft:oak_log":          ("oak_log_top.png", "oak_log.png", "oak_log_top.png"),
    "minecraft:cobblestone":      ("cobblestone.png", "cobblestone.png", "cobblestone.png"),
    "minecraft:glass":            ("glass.png", "glass.png", "glass.png"),
    "minecraft:oak_planks":       ("oak_planks.png", "oak_planks.png", "oak_planks.png"),
    "minecraft:water":            ("water_still.png", "water_still.png", "water_still.png"),
}
idx = {e["blockName"]: e for e in manifest}
for name, exp in EXPECT.items():
    got = idx.get(name)
    if not got:
        errors.append("回归: %s 不在 manifest" % name)
        continue
    act = (got["topTex"], got["sideTex"], got["bottomTex"])
    if act != exp:
        errors.append("回归: %s 期望 %s 实得 %s" % (name, exp, act))

# 分类回归
EXPECT_ALPHA = {
    "minecraft:stone": "opaque",
    "minecraft:glass": "transparent",
    "minecraft:water": "transparent",
    "minecraft:oak_leaves": "masked",
    "minecraft:glass_pane": "masked",
    "minecraft:iron_bars": "masked",
    "minecraft:grass": "masked",
    "minecraft:oak_slab": None,
}
for name, exp in EXPECT_ALPHA.items():
    if exp is None:
        continue
    got = idx.get(name, {}).get("alphaMode")
    if got != exp:
        errors.append("分类回归: %s 期望 %s 实得 %s" % (name, exp, got))

# ---- 输出
print()
print("alphaMode 分布: %s" % dict(Counter(e["alphaMode"] for e in manifest)))
print("emissive: %d, placeholder: %d, animated: %d, nonCubeHint: %d"
      % (sum(1 for e in manifest if e["emissive"]),
         sum(1 for e in manifest if e.get("isPlaceholder")),
         sum(1 for e in manifest if e.get("animated")),
         sum(1 for e in manifest if e.get("shapeHint") == "non_cube")))

if warnings:
    print("\n警告 (%d):" % len(warnings))
    for w in warnings[:10]:
        print("  -", w)
if errors:
    print("\n错误 (%d):" % len(errors))
    for e in errors[:30]:
        print("  -", e)
    sys.exit(1)
print("\n全部校验通过。")
