#!/usr/bin/env python3
"""
路线C 第一阶段·层2（资源层）：为 MC 1.16.5 方块生成材质清单。

输入:
  parse/block_states.json          全局方块状态表（取去重 blockName）
  assets/metadata/blockstates/*.json   1.16.5 官方 blockstates 定义
  assets/metadata/models/block/*.json  1.16.5 官方 block 模型定义
  assets/textures/block/*.png          1.16.5 默认方块纹理

输出:
  assets/material_manifest.json     供层3（UE5 装配）直接消费
  assets/coverage_report.json       覆盖度核对报告

纹理解析优先级:
  1. blockstates -> model -> 沿 parent 链解析 faces.texture 的 "#var" -> textures[var] -> PNG
  2. 启发式 <name>_top/_side/_bottom.png
  3. 启发式 <name>.png
  4. 占位纯色 PNG（isPlaceholder=true）
"""
import json
import os
import re
import glob
import colorsys
from collections import OrderedDict, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
TEX_DIR = os.path.join(ASSETS, "textures", "block")
BS_DIR = os.path.join(ASSETS, "metadata", "blockstates")
MODEL_DIR = os.path.join(ASSETS, "metadata", "models", "block")
MANIFEST = os.path.join(ASSETS, "material_manifest.json")
REPORT = os.path.join(ASSETS, "coverage_report.json")

# ---------------------------------------------------------------- 纹理索引
TEX_FILES = set(os.path.basename(p) for p in glob.glob(os.path.join(TEX_DIR, "*.png")))
# 动画纹理：同名 .png.mcmeta 存在（火焰/水/岩浆帧动画）
ANIMATED = set(os.path.basename(p)[:-len(".png.mcmeta")]
               for p in glob.glob(os.path.join(TEX_DIR, "*.png.mcmeta")))
# 动画纹理是竖向帧序列（高 > 16），UE5 侧应取首帧或做 Flipbook
ANIMATED_MULTIFRAME = set()

# 占位纹理清单：保证脚本可重复运行而不会自我污染
# （上一轮生成的占位 PNG 必须在索引前移除，否则会被当成原版纹理解析）
GENERATED_LIST = os.path.join(ASSETS, "metadata", "generated_placeholders.txt")


def purge_generated():
    """删除上一轮生成的占位纹理，保持幂等"""
    removed = []
    if os.path.exists(GENERATED_LIST):
        with open(GENERATED_LIST, "r", encoding="utf-8") as f:
            for line in f:
                fn = line.strip()
                if not fn:
                    continue
                p = os.path.join(TEX_DIR, fn)
                if os.path.exists(p):
                    os.remove(p)
                    removed.append(fn)
                TEX_FILES.discard(fn)
        os.remove(GENERATED_LIST)
    return removed


GENERATED_THIS_RUN = []

# ---------------------------------------------------------------- 分类规则
# transparent：半透明混合
TRANSPARENT_EXACT = {
    "glass", "water", "lava", "ice", "packed_ice", "blue_ice", "frosted_ice",
    "slime_block", "honey_block", "barrier", "light", "structure_void",
    " stained_glass", "spawner", "end_gateway", "beacon",
}
TRANSPARENT_SUFFIX = ("stained_glass", "stained_glass_pane")
TRANSPARENT_CONTAINS = ("glass", "_ice", "ice_")

# masked：alphaTest（cutout），不透明像素保留、透明像素丢弃
MASKED_EXACT = {
    "glass_pane", "iron_bars", "ladder", "rail", "torch", "redstone_torch",
    "soul_torch", "redstone_wire", "lever", "tripwire", "tripwire_hook",
    "lily_pad", "kelp", "seagrass", "tall_grass", "large_fern", "grass",
    "vine", "scaffolding", "cobweb", "chest", "trapped_chest", "barrel",
    " furnace", "bell", "candle", "end_rod", "chain", "lantern",
    "brewing_stand", "flower_pot", "skeleton_skull", "wither_skeleton_skull",
    "zombie_head", "player_head", "creeper_head", "dragon_head", "wall_sign",
    "oak_sign", "cake", "pumpkin", "melon", "carved_pumpkin", "melon_stem",
    "attached_pumpkin_stem", "attached_melon_stem", "nether_wart",
    "cobblestone_wall", "nether_bricks", "bone_block", "hay_block",
}
MASKED_SUFFIX = (
    "_leaves", "_sapling", "_vine", "_fern", "_door", "_trapdoor", "_fence",
    "_fence_gate", "_banner", "_carpet", "_pressure_plate", "_button",
    "_rail", "_roots", "_coral", "_coral_fan", "_coral_block", "_wall_sign",
    "_wall_torch", "_wall_banner", "_sign", "_sapling", "_stem", "_hyphae",
    "_fungus", "_roots", "_candle", "_chain",
)
MASKED_CONTAINS = (
    "leaves", "sapling", "vine", "door", "trapdoor", "fence", "banner",
    "carpet", "pressure_plate", "button", "rail", "roots", "coral",
    "torch", "sign", "pane", "bars", "ladder", "plant", "bush",
    "flower", "sapling", "vine",
)

# emissive：自发光
EMISSIVE_STRONG = {
    "lava": "#ff6a00",
    "glowstone": "#ffb463",
    "sea_lantern": "#ffdca0",
    "redstone_lamp": "#ff4d1a",
    "shroomlight": "#ffb45e",
    "end_rod": "#ffe6b0",
    "magma_block": "#8a3c10",
    "fire": "#ffb020",
    "beacon": "#8fd4ff",
    "soul_fire": "#3aa0ff",
    "crying_obsidian": "#8b3fd4",
}
EMISSIVE_SUFFIX = ("_lantern",)

# 各向异性（非立方体）方块：层3 需要额外处理，供报告参考
NON_CUBE_HINT = (
    "_slab", "_stairs", "_fence", "_fence_gate", "_wall", "_door", "_trapdoor",
    "_pane", "_pressure_plate", "_button", "_rail", "_sign", "_banner",
    "_carpet", "_candle", "_sapling", "_plant", "_flower", "_bush",
    "_leaves", "_chest", "_barrel", "_bed", "_torch", "_ladder", "_vine",
    "_grindstone", "_anvil", "_beacon", "_conduit", "_lantern", "_scaffolding",
    "_barrier", "_chain", "_coral", "_roots", "_sapling", "_grass",
)


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- 模型解析
def strip_ns(ref):
    """minecraft:block/stone -> block/stone"""
    if ref.startswith("minecraft:"):
        return ref[len("minecraft:"):]
    return ref


def model_to_basename(ref):
    """
    模型引用 -> models/block/ 下的文件名（不含 .json）
    minecraft:block/cube_all -> cube_all
    builtin/entity            -> builtin/entity（调用方需自行过滤）
    """
    r = strip_ns(ref)
    if r.startswith("block/"):
        r = r[len("block/"):]
    return r


def tex_ref_to_file(ref):
    """
    纹理引用 -> textures/block/ 下的 PNG 文件名，失败返回 None。

    只接受 block 命名空间的引用（"block/stone" / "minecraft:block/stone"）。
    item/entity 等其它命名空间必须拒绝——否则 "minecraft:item/barrier" 会因
    basename 相同而错误命中 block/barrier.png（barrier 在原版无方块贴图）。
    """
    if not ref:
        return None
    ref = strip_ns(ref)
    if ref.startswith("#"):
        return None
    # 跨模型纹理引用 xxx#var -> 只关心最终文件名，取 var 侧
    if "#" in ref:
        return None
    if ref.startswith("block/"):
        cand = ref[len("block/"):] + ".png"
        return cand if cand in TEX_FILES else None
    # 无前缀的裸引用（如 "stone"）视为 block 命名空间
    if "/" not in ref:
        cand = ref + ".png"
        return cand if cand in TEX_FILES else None
    # 其它命名空间（item/ entity/ ...）不在本目录内
    return None


_model_cache = {}


def load_model(model_name):
    """加载 models/block/<name>.json，带缓存。找不到返回 None"""
    if model_name in _model_cache:
        return _model_cache[model_name]
    base = model_to_basename(model_name)
    if base.startswith("builtin/"):
        _model_cache[model_name] = None
        return None
    path = os.path.join(MODEL_DIR, base + ".json")
    data = load_json(path)
    _model_cache[model_name] = data
    return data


def resolve_model(model_name, _depth=0, _seen=None):
    """
    解析模型（含 parent 链），返回:
      {"textures": {var: raw_ref}, "faces": {dir: face_dict}}
    parent 的 textures 作为基底，child 覆盖；faces 按方向逐个覆盖。
    builtin/* 模型（entity/empty）返回空。
    """
    if _depth > 12:
        return {"textures": {}, "faces": {}}
    _seen = _seen or set()
    if model_name in _seen:
        return {"textures": {}, "faces": {}}
    _seen = _seen | {model_name}

    data = load_model(model_name)
    if not isinstance(data, dict):
        return {"textures": {}, "faces": {}}

    parent = data.get("parent")
    base = {"textures": {}, "faces": {}}
    if parent:
        pname = model_to_basename(parent)
        if not pname.startswith("builtin/"):
            base = resolve_model(pname, _depth + 1, _seen)

    textures = dict(base["textures"])
    for k, v in (data.get("textures") or {}).items():
        if isinstance(v, str):
            textures[k] = v

    faces = dict(base["faces"])
    # 同一方向可能有多个 element（多层几何，如 grass_block 的 side + overlay）。
    # 优先级：完整立方体 element(0..16) > 其它；同级取先出现的。
    full_box = {}
    fallback = {}
    for el in (data.get("elements") or []):
        frm = el.get("from") or [0, 0, 0]
        to = el.get("to") or [0, 0, 0]
        is_full = all(abs(float(frm[i]) - 0) < 1e-6 and abs(float(to[i]) - 16) < 1e-6
                      for i in range(3))
        for direction, face in (el.get("faces") or {}).items():
            if not isinstance(face, dict):
                continue
            if is_full:
                if direction not in full_box:
                    full_box[direction] = face
            elif direction not in fallback:
                fallback[direction] = face
    merged = dict(fallback)
    merged.update(full_box or {})
    # 继承自 parent 的面仅在自身与子模型都未覆盖时才保留
    for d, f in base["faces"].items():
        merged.setdefault(d, f)
    faces = merged

    return {"textures": textures, "faces": faces}


def deref_texture(raw, textures, _depth=0):
    """
    解析纹理引用，支持别名链：
      "#down" -> textures["down"] = "#bottom" -> textures["bottom"] = "minecraft:block/x"
    返回可用的纹理引用字符串，或 None。
    """
    if not isinstance(raw, str) or not raw:
        return None
    if _depth > 8:
        return None
    raw = raw.strip()
    if raw.startswith("#"):
        nxt = textures.get(raw[1:])
        if nxt is None:
            return None
        return deref_texture(nxt, textures, _depth + 1)
    return raw


def faces_to_named_faces(res):
    """把 6 向 faces 归约为 top/side/bottom 三个纹理文件名"""
    textures = res["textures"]
    faces = res["faces"]

    def pick(directions):
        for d in directions:
            f = faces.get(d)
            if not f:
                continue
            raw = deref_texture(f.get("texture"), textures)
            fn = tex_ref_to_file(raw)
            if fn:
                return fn, True
        return None, False

    top, _ = pick(["up"])
    bottom, _ = pick(["down"])
    side, _ = pick(["north", "south", "east", "west"])

    # 单向缺失时对侧互补（cube_column 只有 end+side）
    if top and not bottom:
        bottom = top
    if bottom and not top:
        top = bottom
    return top, side, bottom


def pick_state_model(bs, prefer_props=None):
    """
    从 blockstates 定义里挑一个代表性模型。
    prefer_props: 该 blockName 在存档中最常见的属性串（如 "snowy=false"），
                  命中则直接用该变体——保证清单反映存档里的主要形态。
    优先级：精确命中 prefer_props > 默认 key("") > 属性数最少且值含 false/0 > 首个。
    """
    variants = bs.get("variants") or {}
    if variants:
        # 1) 精确命中存档主状态
        if prefer_props and prefer_props in variants:
            return _first_model(variants[prefer_props]), "variants:%s" % prefer_props
        # 2) 默认 key
        if "" in variants:
            return _first_model(variants[""]), "variants:default"
        # 3) 含 false/0 的变体优先（grass_block 的 snowy=false 而非 snowy=true）
        def rank(k):
            low = k.lower()
            return (0 if ("=false" in low or "=0" in low) else 1, len(k), k)
        for k in sorted(variants.keys(), key=rank):
            m = _first_model(variants[k])
            if m:
                return m, "variants:%s" % k
    multipart = bs.get("multipart") or []
    if multipart:
        for part in multipart:
            apply = (part or {}).get("apply")
            if isinstance(apply, dict):
                apply = [apply]
            if isinstance(apply, list) and apply:
                m = _first_model(apply)
                if m:
                    return m, "multipart"
    return None, None


def _first_model(v):
    """variants/multipart 的值可能是 dict 或 list，取第一个含 model 的项"""
    if isinstance(v, dict):
        v = [v]
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict) and "model" in first:
            return strip_ns(first["model"])
    return None


# ---------------------------------------------------------------- 启发式
def heuristic_tex(short):
    n = short + ".png"
    if n in TEX_FILES:
        return n, "heuristic:<name>.png"
    top = short + "_top.png"
    side = short + "_side.png"
    bottom = short + "_bottom.png"
    if top in TEX_FILES or side in TEX_FILES or bottom in TEX_FILES:
        t = top if top in TEX_FILES else n
        s = side if side in TEX_FILES else t
        b = bottom if bottom in TEX_FILES else t
        return (t, s, b), "heuristic:<name>_{top,side,bottom}.png"
    # 方块名 vs 物品名常见差异：fence_gate -> oak_fence_gate 等已含前缀，一般无需处理
    return None, None


# ---------------------------------------------------------------- 占位纹理
DEFAULT_PLACEHOLDER_COLOR = (0.55, 0.55, 0.58)

NAMED_COLORS = {
    "white": (0.89, 0.89, 0.89), "orange": (0.95, 0.55, 0.16),
    "magenta": (0.86, 0.35, 0.79), "light_blue": (0.42, 0.66, 0.93),
    "yellow": (0.96, 0.79, 0.25), "lime": (0.55, 0.82, 0.24),
    "pink": (0.93, 0.60, 0.73), "gray": (0.42, 0.42, 0.42),
    "light_gray": (0.63, 0.63, 0.64), "cyan": (0.28, 0.77, 0.78),
    "purple": (0.51, 0.32, 0.71), "blue": (0.24, 0.40, 0.75),
    "brown": (0.53, 0.35, 0.22), "green": (0.36, 0.62, 0.22),
    "red": (0.70, 0.24, 0.22), "black": (0.16, 0.16, 0.17),
}

SPECIAL_COLORS = {
    "stone": (0.51, 0.51, 0.53), "granite": (0.64, 0.40, 0.33),
    "diorite": (0.86, 0.86, 0.87), "andesite": (0.55, 0.55, 0.56),
    "grass_block": (0.42, 0.66, 0.28), "dirt": (0.53, 0.38, 0.25),
    "coarse_dirt": (0.51, 0.37, 0.25), "podzol": (0.42, 0.31, 0.18),
    "cobblestone": (0.48, 0.48, 0.49), "bedrock": (0.42, 0.42, 0.44),
    "sand": (0.91, 0.86, 0.65), "red_sand": (0.85, 0.44, 0.24),
    "gravel": (0.55, 0.53, 0.53), "clay": (0.62, 0.66, 0.70),
    "netherrack": (0.55, 0.22, 0.22), "nether_bricks": (0.24, 0.13, 0.14),
    "end_stone": (0.85, 0.84, 0.60), "end_stone_bricks": (0.83, 0.82, 0.58),
    "obsidian": (0.11, 0.09, 0.20), "crying_obsidian": (0.28, 0.13, 0.42),
    "water": (0.25, 0.42, 0.75), "lava": (0.85, 0.35, 0.05),
    "ice": (0.55, 0.72, 0.85), "packed_ice": (0.53, 0.70, 0.83),
    "blue_ice": (0.40, 0.60, 0.80), "frosted_ice": (0.65, 0.80, 0.90),
    "snow_block": (0.92, 0.95, 0.98), "soul_sand": (0.44, 0.35, 0.31),
    "soul_soil": (0.36, 0.28, 0.24), "magma_block": (0.42, 0.20, 0.11),
    "glowstone": (0.98, 0.83, 0.55), "sea_lantern": (0.85, 0.92, 0.88),
    "shroomlight": (0.96, 0.65, 0.30), "redstone_lamp": (0.75, 0.24, 0.12),
    "netherite_block": (0.32, 0.28, 0.27), "quartz_block": (0.92, 0.90, 0.87),
    "smooth_sandstone": (0.90, 0.85, 0.65), "sandstone": (0.87, 0.80, 0.58),
    "prismarine": (0.60, 0.79, 0.75), "terracotta": (0.66, 0.40, 0.31),
    "brick": (0.60, 0.34, 0.28), "bookshelf": (0.60, 0.44, 0.28),
    "iron_block": (0.78, 0.78, 0.80), "gold_block": (0.98, 0.82, 0.30),
    "diamond_block": (0.09, 0.80, 0.79), "emerald_block": (0.24, 0.79, 0.44),
    "lapis_block": (0.16, 0.31, 0.62), "coal_block": (0.10, 0.10, 0.10),
    "obsidian_peak": (0.11, 0.09, 0.20), "basalt": (0.35, 0.33, 0.34),
    "blackstone": (0.16, 0.14, 0.16), "polished_blackstone": (0.20, 0.18, 0.20),
    "deepslate": (0.24, 0.24, 0.26), "tuff": (0.44, 0.42, 0.39),
    "calcite": (0.90, 0.89, 0.85), "dripstone_block": (0.72, 0.66, 0.58),
    "moss_block": (0.30, 0.42, 0.22), "sculk": (0.12, 0.20, 0.22),
    "mud": (0.35, 0.27, 0.22), "packed_mud": (0.48, 0.38, 0.30),
    "honey_block": (0.90, 0.66, 0.20), "slime_block": (0.42, 0.72, 0.42),
    "glass": (0.80, 0.88, 0.88), "white_stained_glass": (0.90, 0.93, 0.93),
    "oak_log": (0.55, 0.43, 0.24), "birch_log": (0.83, 0.79, 0.68),
    "spruce_log": (0.35, 0.25, 0.14), "jungle_log": (0.53, 0.40, 0.24),
    "acacia_log": (0.55, 0.35, 0.24), "dark_oak_log": (0.35, 0.25, 0.14),
    "oak_planks": (0.68, 0.56, 0.36), "oak_leaves": (0.32, 0.58, 0.24),
    "oak_sapling": (0.36, 0.62, 0.26), "coal_ore": (0.42, 0.42, 0.42),
    "iron_ore": (0.72, 0.65, 0.58), "gold_ore": (0.78, 0.68, 0.42),
    "diamond_ore": (0.53, 0.60, 0.61), "emerald_ore": (0.44, 0.57, 0.48),
    "lapis_ore": (0.30, 0.40, 0.58), "redstone_ore": (0.55, 0.22, 0.20),
    "gravel_placeholder": (0.55, 0.53, 0.53),
}


def guess_color(short):
    """为占位纹理猜一个合理颜色"""
    if short in NAMED_COLORS:
        return NAMED_COLORS[short]
    if short in SPECIAL_COLORS:
        return SPECIAL_COLORS[short]
    # 复合名：取最后一个颜色词（white_concrete -> white）
    parts = short.split("_")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in NAMED_COLORS:
            base = NAMED_COLORS[parts[i]]
            # 若是 concrete/terracotta/wool/stained_glass，略微压暗增加区分
            if any(k in short for k in ("terracotta", "concrete")):
                return tuple(c * 0.94 for c in base)
            return base
    # 前缀匹配特殊表
    for key, col in SPECIAL_COLORS.items():
        if short.startswith(key):
            return col
    return DEFAULT_PLACEHOLDER_COLOR


def write_placeholder_png(short, color):
    """生成 16x16 占位纯色 PNG（带轻微噪声便于识别）"""
    from PIL import Image
    fn = short + ".png"
    path = os.path.join(TEX_DIR, fn)
    if os.path.exists(path):
        return fn
    img = Image.new("RGBA", (16, 16))
    px = img.load()
    for y in range(16):
        for x in range(16):
            n = ((x * 7 + y * 13) % 5 - 2) * 0.012
            px[x, y] = tuple(max(0, min(255, int((c + n) * 255))) for c in color) + (255,)
    img.save(path)
    TEX_FILES.add(fn)
    GENERATED_THIS_RUN.append(fn)
    return fn


# ---------------------------------------------------------------- 分类
def classify_alpha(short):
    """返回 (alphaMode, reason)"""
    s = short

    def is_transparent():
        if s in TRANSPARENT_EXACT:
            return True
        if any(s.endswith(sfx) for sfx in TRANSPARENT_SUFFIX):
            return True
        return False

    def is_masked():
        if s in MASKED_EXACT:
            return True
        if any(s.endswith(sfx) for sfx in MASKED_SUFFIX):
            return True
        for c in MASKED_CONTAINS:
            if c in s:
                return True
        return False

    # glass_pane 是 alphaTest（cutout），不是半透明——先判 masked
    if "glass_pane" in s or s.endswith("_pane"):
        return "masked", "pane=cutout"
    if s in ("fire", "soul_fire", "nether_portal", "end_portal", "end_gateway"):
        return "masked", "cutout-keyword"
    if is_masked():
        return "masked", "cutout-keyword"
    if is_transparent():
        return "transparent", "translucent-keyword"
    if any(c in s for c in TRANSPARENT_CONTAINS):
        return "transparent", "translucent-keyword"
    return "opaque", "default"


def classify_emissive(short):
    if short in EMISSIVE_STRONG:
        return True, EMISSIVE_STRONG[short]
    for sfx in EMISSIVE_SUFFIX:
        if short.endswith(sfx):
            # soul_lantern 偏蓝，其余偏暖
            return True, ("#3aa8ff" if "soul" in short else "#ffb45e")
    return False, None


# ---------------------------------------------------------------- 主流程
def main():
    purged = purge_generated()
    if purged:
        print("清理上一轮占位纹理 %d 个: %s" % (len(purged), purged[:5]))
    bs_states = load_json(os.path.join(ROOT, "parse", "block_states.json")) or {}
    states = bs_states.get("states") or []
    all_names = sorted(set(s["name"] for s in states if s.get("name")))
    # 每种 blockName 在存档中最常见的属性串，用于挑选代表性变体
    dom_props = {}
    for s in states:
        n = s.get("name")
        if not n:
            continue
        c = s.get("count") or 0
        p = s.get("properties") or ""
        if n not in dom_props or c > dom_props[n][0]:
            dom_props[n] = (c, p)
    stats = load_json(os.path.join(ROOT, "parse", "stats.json")) or {}
    used_names = set()
    for dim in (stats.get("dimensions") or {}).values():
        for key in ("top30_block_types",):
            for item in (dim.get(key) or []):
                if item.get("name"):
                    used_names.add(item["name"])
        for item in (dim.get("all_block_types") or []):
            if isinstance(item, dict) and item.get("name"):
                used_names.add(item["name"])

    manifest = []
    src_counter = defaultdict(int)
    missing = []
    placeholder_count = 0

    for full in all_names:
        short = full.split(":", 1)[1] if ":" in full else full
        entry = OrderedDict()
        entry["blockName"] = full

        source = None
        top = side = bottom = None

        # --- 1) 精确解析 blockstates + models
        bs = load_json(os.path.join(BS_DIR, short + ".json"))
        if bs:
            model, variant_key = pick_state_model(bs, dom_props.get(full, (0, ""))[1])
            if model:
                res = resolve_model("minecraft:" + model)
                t, s, bt = faces_to_named_faces(res)
                if t or s or bt:
                    # 火焰等只有侧面：缺失方向用已解析到的纹理补齐
                    any_tex = t or s or bt
                    top, side, bottom = t or any_tex, s or any_tex, bt or any_tex
                    source = "model:%s(%s)" % (model, variant_key)
                elif res.get("textures"):
                    # 无 faces 但有 textures（water/lava 等仅有 particle）
                    for var in ("all", "top", "side", "bottom", "particle", "cross", "end"):
                        fn = tex_ref_to_file(deref_texture(res["textures"].get(var), res["textures"]))
                        if fn:
                            top = side = bottom = fn
                            source = "model-textures:%s(%s)" % (model, variant_key)
                            break

        # --- 2) 启发式 <name>_top/_side/_bottom
        if not top:
            r = heuristic_tex(short)
            if r and isinstance(r[0], tuple):
                top, side, bottom = r[0]
                source = r[1]
            elif r and isinstance(r[0], str):
                top = side = bottom = r[0]
                source = r[1]

        # --- 3) 占位纯色
        # barrier 等在原版中就没有方块贴图（用 item/barrier 渲染），落到这里生成占位
        if not top:
            fn = write_placeholder_png(short, guess_color(short))
            top = side = bottom = fn
            source = "placeholder:generated"
            placeholder_count += 1
            entry["isPlaceholder"] = True

        src_counter[source.split(":")[0]] += 1
        entry["texSource"] = source
        entry["topTex"] = top
        entry["sideTex"] = side or top
        entry["bottomTex"] = bottom or top

        # 缺任一面 -> 记录需要 fallback
        if not (top and side and bottom):
            missing.append({"blockName": full, "source": source,
                            "top": top, "side": side, "bottom": bottom})

        am, am_reason = classify_alpha(short)
        entry["alphaMode"] = am
        em, em_color = classify_emissive(short)
        entry["emissive"] = em
        if em and em_color:
            entry["emissiveColor"] = em_color

        # 附加快照信息，便于层3 判断是否需要非立方体处理
        if any(k in short for k in NON_CUBE_HINT):
            entry["shapeHint"] = "non_cube"
        if any((f or "").rsplit(".", 1)[0] in ANIMATED
               for f in (top, side, bottom)):
            entry["animated"] = True

        manifest.append(entry)

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    # 记录本轮生成的占位纹理，供下次运行清理（保证幂等）
    if GENERATED_THIS_RUN:
        with open(GENERATED_LIST, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(GENERATED_THIS_RUN)) + "\n")

    # ---------------------------------------------------------------- 报告
    alpha_counts = defaultdict(int)
    for e in manifest:
        alpha_counts[e["alphaMode"]] += 1
    emissive_n = sum(1 for e in manifest if e["emissive"])
    shape_n = sum(1 for e in manifest if e.get("shapeHint") == "non_cube")

    # 覆盖度：用实际出现集合核对
    covered = [n for n in sorted(used_names) if any(e["blockName"] == n for e in manifest)]
    uncovered = sorted(set(used_names) - set(covered))

    # 纹理文件是否真的都在磁盘上
    bad_files = []
    for e in manifest:
        for k in ("topTex", "sideTex", "bottomTex"):
            if e[k] not in TEX_FILES:
                bad_files.append((e["blockName"], k, e[k]))

    report = OrderedDict()
    report["textureSource"] = {
        "repo": "https://github.com/InventivetalentDev/minecraft-assets",
        "branch": "1.16.5",
        "commit": "1b7a1d48b0343c0b8da80604598707df428dc32a",
        "mirror": "https://ghfast.top/",
        "path": "assets/minecraft/textures/block",
        "pngCount": len(TEX_FILES),
    }
    report["manifest"] = {
        "entries": len(manifest),
        "alphaMode": dict(alpha_counts),
        "emissive": emissive_n,
        "placeholder": placeholder_count,
        "nonCubeHint": shape_n,
        "textureSourceBreakdown": dict(src_counter),
    }
    report["coverage"] = {
        "usedBlockNames": len(used_names),
        "covered": len(covered),
        "uncovered": uncovered,
        "partialFaceEntries": len(missing),
        "partialFaceSample": missing[:15],
        "missingTextureFiles": bad_files[:20],
        "missingTextureFileCount": len(bad_files),
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # ---------------------------------------------------------------- 打印
    print("=== 纹理 ===")
    print("PNG 总数(磁盘): %d" % len(TEX_FILES))
    print("=== manifest ===")
    print("条目数: %d" % len(manifest))
    for k in ("opaque", "masked", "transparent"):
        print("  %-12s %d" % (k, alpha_counts.get(k, 0)))
    print("  emissive     %d" % emissive_n)
    print("  placeholder  %d" % placeholder_count)
    print("  nonCubeHint  %d" % shape_n)
    print("  纹理来源分布: %s" % dict(src_counter))
    print("=== 覆盖度 ===")
    print("stats 中出现的 blockName: %d, 已覆盖: %d, 未覆盖: %d"
          % (len(used_names), len(covered), len(uncovered)))
    if uncovered:
        print("  未覆盖: %s" % uncovered[:10])
    print("面纹理不完整条目: %d" % len(missing))
    for m in missing[:10]:
        print("   ", m)
    print("引用了不存在纹理的条目数: %d" % len(bad_files))
    for b in bad_files[:10]:
        print("   ", b)


if __name__ == "__main__":
    main()
