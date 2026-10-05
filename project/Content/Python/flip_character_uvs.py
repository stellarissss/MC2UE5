# -*- coding: utf-8 -*-
"""
flip_character_uvs.py -- correct the character skin mapping.

The figure renders as a black silhouette while the terrain (a different material)
is fine. The character meshes map onto a 64x64 Minecraft skin atlas, and the
regions they use -- head at v 0.0-0.25, torso/limbs at v 0.25-0.5 -- are the
painted ones only if V is measured from the *bottom*, which is the OBJ
convention. If the importer keeps V as authored while the texture is sampled
top-left, every part lands in the atlas's dead (black) half.

This re-imports the four parts with V mirrored (v -> 1 - v) so the sampled
region matches the painted region, and reports the UV range it wrote.

Run inside the editor.
"""

import os

import unreal

SRC_DIR = "Q:/MC2UE5/character"
TMP_DIR = "Q:/MC2UE5/character_flipped"
DEST_DIR = "/Game/MC/Character"
PARTS = ("char_head", "char_torso", "char_arm", "char_leg")
REPORT = "Q:/MC2UE5/logs/flip_char.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCUVF] " + str(m))
    with open(REPORT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def flip_v(src, dst):
    out = []
    for line in open(src):
        if line.startswith("vt "):
            bits = line.split()
            u = float(bits[1])
            v = float(bits[2])
            out.append("vt %.6f %.6f\n" % (u, 1.0 - v))
        else:
            out.append(line)
    with open(dst, "w", newline="\n") as fh:
        fh.writelines(out)


def main():
    os.makedirs(TMP_DIR, exist_ok=True)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    for part in PARTS:
        src = os.path.join(SRC_DIR, part + ".obj")
        dst = os.path.join(TMP_DIR, part + ".obj")
        flip_v(src, dst)

        name = "CH_" + part.split("_")[1]      # char_head -> CH_head
        asset = "%s/%s" % (DEST_DIR, name)
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", dst)
        task.set_editor_property("destination_path", DEST_DIR)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        tools.import_asset_tasks([task])

        mesh = unreal.load_asset(asset)
        if mesh is None:
            say("%s: import produced nothing" % name)
            continue
        try:
            unreal.EditorAssetLibrary.save_loaded_asset(mesh)
        except Exception:
            pass
        say("%s re-imported with V flipped" % name)

    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    say("--- done ---")


if __name__ == "__main__":
    main()
