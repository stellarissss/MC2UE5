import unreal
for p in ("/Game/Maps/MCReplica",):
    if unreal.EditorAssetLibrary.does_asset_exist(p):
        ok = unreal.EditorAssetLibrary.delete_asset(p)
        unreal.log("[DEL] %s -> %s" % (p, ok))
