#!/usr/bin/env bash
# 路线C 层2（资源层）：拉取 MC 1.16.5 默认方块纹理 + 元数据。
#
# 用法:  bash scripts/fetch_mc_assets.sh
# 产物:
#   assets/textures/block/*.png        默认方块纹理
#   assets/metadata/models/block/*.json   模型定义（用于精确解析面纹理）
#   assets/metadata/blockstates/*.json    方块状态定义（用于挑选变体）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSETS="$ROOT/assets"
TEX_OUT="$ASSETS/textures/block"
META_OUT="$ASSETS/metadata"
TMP="/tmp/mcassets"

REPO="https://github.com/InventivetalentDev/minecraft-assets.git"
MIRROR="${MC_MIRROR:-https://ghfast.top/}"
BRANCH="1.16.5"

mkdir -p "$TEX_OUT" "$META_OUT/models/block" "$META_OUT/blockstates"

echo "==> 清理旧占位纹理"
# 上一轮 build_material_manifest.py 生成的占位 PNG 必须删除，
# 否则会被下一轮当成原版纹理解析（barrier 就是这种情况）。
if [ -f "$META_OUT/generated_placeholders.txt" ]; then
  while read -r f; do
    [ -n "$f" ] && rm -f "$TEX_OUT/$f"
  done < "$META_OUT/generated_placeholders.txt"
  rm -f "$META_OUT/generated_placeholders.txt"
fi

echo "==> 克隆仓库（镜像: $MIRROR，分支: $BRANCH）"
if [ ! -d "$TMP/.git" ]; then
  rm -rf "$TMP"
  git clone --filter=blob:none --sparse --depth 1 "${MIRROR}${REPO}" "$TMP"
  # 该仓库按版本分支组织，默认分支无资源，需拉全部分支引用
  git -C "$TMP" remote set-branches origin '*'
  git -C "$TMP" fetch --depth 1 origin "+refs/heads/*:refs/remotes/origin/*"
fi

echo "==> 稀疏检出（分支根目录即版本目录，无 1.16.5/ 前缀）"
git -C "$TMP" sparse-checkout set --no-cone \
  '/assets/minecraft/textures/block/**' \
  '/assets/minecraft/models/block/**' \
  '/assets/minecraft/blockstates/**'
git -C "$TMP" checkout -q "origin/$BRANCH"

SRC="$TMP/assets/minecraft"
echo "==> 复制纹理"
cp -f "$SRC"/textures/block/*.png "$TEX_OUT"/
cp -f "$SRC"/textures/block/*.png.mcmeta "$TEX_OUT"/ 2>/dev/null || true
echo "==> 复制元数据"
cp -f "$SRC"/models/block/*.json "$META_OUT/models/block"/
cp -f "$SRC"/blockstates/*.json "$META_OUT/blockstates"/

echo
echo "纹理 PNG: $(ls "$TEX_OUT"/*.png | wc -l)  ($(du -sh "$TEX_OUT" | cut -f1))"
echo "模型:     $(ls "$META_OUT/models/block"/*.json | wc -l)"
echo "方块状态: $(ls "$META_OUT/blockstates"/*.json | wc -l)"
echo "commit:   $(git -C "$TMP" rev-parse origin/$BRANCH)"
echo
echo "样例验证:"
for f in stone.png grass_block_top.png grass_block_side.png oak_log_top.png; do
  if [ -f "$TEX_OUT/$f" ]; then echo "  OK      $f"; else echo "  MISSING $f"; fi
done
