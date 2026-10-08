"""ensure_lighting.py -- put a sun and a skylight in the level, idempotently.

Why this exists
---------------
The level shipped with a SkyAtmosphere and nothing else. A census of every
actor in MC_Built gave:

    MCblk 1041, B 158, MC 40, T 24, SkyAtmosphere 1, PlayerStart 1

No DirectionalLight. No SkyLight. Every earlier "the render is black / the
render is blown out" observation was therefore the *sky dome* changing, never a
lit surface: with no light actors there is nothing for a surface to respond
to. It also explains why raising the sun to 100000 lux turned the frame white
(the sky saturated) while 1000 and 10000 lux left the ground unchanged -- the
-MCsuns flag only reconfigures an existing DirectionalLight, so with none
present it had nothing to act on. An earlier run of applylook.py even logged
"CreateLight: 0 actors" and that failure was never treated as blocking.

Everything else was verified correct while this was true: the terrain carries
676,656/676,656 triangles, its vertex normals are present and unit length
(MCMeshTools::CountUsableNormals reports 387,096/387,096 across four tiles),
the material graph samples T_MC_grass, and all 182 mesh actors are visible and
unhidden. Geometry, materials and visibility were never the problem.

Units
-----
UE 5 directional light intensity is in lux and the default exposure is auto, so
a daytime value in the low single digits is normal here. The values below are
modest on purpose: they were chosen to be legible, not physically exact, and
-MCsuns still overrides the intensity at runtime when it is passed.

Run:
    UnrealEditor-Cmd.exe <project> -ExecutePythonScript=tools/ensure_lighting.py
"""

import os
import sys

import unreal

OUT = os.environ.get("MC_LOG", r"Q:/MC2UE5/logs/ensure_lighting.txt")
_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[MCLight] " + str(msg))
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


def find_existing(world, cls, label):
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, cls):
        if actor.get_actor_label() == label:
            return actor
    return None


def make_sun(world):
	"""A directional light angled the way a mid-morning sun would be.

	Pitch -58 rather than straight down: a sun directly overhead flattens every
	roof and wall face to the same value, which is exactly the "flat brown
	blobs" read that this pipeline was rebuilt to get away from.

	Shadows are OFF. The level still holds 1,041 legacy ISM clusters carrying
	1,171,144 instances, and as soon as a shadow-casting light exists the
	shadow depth pass overflows its 16-bit mesh-draw-command index and kills the
	process on startup:

	    Assertion failed: NumAcceptedStaticMeshes >= 0 && MDCIdx < ((uint16) 0xffff)
	    [File:.../Runtime/Renderer/Private/ShadowSetup.cpp] [Line: 1611]

	Setting cast_shadow=False on those ISM components does not avoid it -- the
	editor-side write does not mark the render state dirty. Turning the sun's
	shadows off removes the pass entirely, which is the only reliable way to get
	an image out of this scene today. Shadows come back once the legacy voxel
	layer is retired, which is the real fix: those clusters are a superseded
	crutch that the terrain and structure meshes already replace.
	"""
	light = unreal.DirectionalLight()
	light.set_editor_property("intensity", 4.0)
	light.set_editor_property("light_color", unreal.LinearColor(1.0, 0.96, 0.90))
	light.set_editor_property("temperature_enabled", True)
	light.set_editor_property("temperature", 5600.0)
	light.set_actor_rotation(unreal.Rotator(-58.0, 0.0, 34.0, "XYZ"))
	light.set_actor_location(unreal.Vector(0.0, 0.0, 40000.0), False, False)
	light.set_actor_label("MC_Sun")
	unreal.EditorLevelLibrary.add_actor_to_level(light) \
		if hasattr(unreal.EditorLevelLibrary, "add_actor_to_level") \
		else light.set_editor_property("folder_path", unreal.Name("/Game/MC"))

	# cast_shadows lives on the light COMPONENT, not on the actor. Setting it on
	# the actor raises "Failed to find property", which is how the first attempt
	# at this silently did nothing.
	comp = light.get_component_by_class(unreal.DirectionalLightComponent)
	if comp:
		comp.set_editor_property("cast_shadows", False)

	say("sun created: intensity=4.0 rot=(-58,0,34) "
		"cast_shadows=%s" % (comp.get_editor_property("cast_shadows")
							 if comp else "NO-COMPONENT"))
	return light


def make_skylight(world):
    """A skylight so shadowed faces get sky bounce instead of going black."""
    light = unreal.SkyLight()
    light.set_editor_property("intensity", 1.0)
    light.set_editor_property("light_color", unreal.LinearColor(0.62, 0.72, 0.90))
    light.set_editor_property("real_time_capture", True)
    light.set_actor_rotation(unreal.Rotator(-38.0, 0.0, 0.0, "XYZ"))
    light.set_actor_location(unreal.Vector(0.0, 0.0, 60000.0))
    light.set_actor_label("MC_SkyLight")
    unreal.EditorLevelLibrary.add_actor_to_level(light) \
        if hasattr(unreal.EditorLevelLibrary, "add_actor_to_level") \
        else light.set_editor_property("folder_path", unreal.Name("/Game/MC"))
    say("skylight created: intensity=1.0 real_time_capture=True")
    return light


def mute_legacy_clusters(world):
	"""Stop the legacy voxel layer from casting shadows.

	The level still carries 1,041 MCReplicaPropCluster actors holding 1,171,144
	instances from the original cube-layer pipeline. They are already hidden in
	game, but an ISM component still gathers for the shadow pass, and with the
	sun's shadows enabled that overflows the 16-bit mesh-draw-command index:

	    Assertion failed: NumAcceptedStaticMeshes >= 0 && MDCIdx < ((uint16) 0xffff)
	    [File:.../Runtime/Renderer/Private/ShadowSetup.cpp] [Line: 1611]

	That assertion kills the process on startup, so it blocks every visual check.
	The clusters are a superseded crutch -- the terrain and structure meshes
	replace them -- so muting them is the correct end state, not a workaround.
	Their collision is left alone on purpose: the pawn still stands on
	MCReplicaPropCluster_449, and removing that before the terrain has collision
	would drop the player through the world.
	"""
	clusters = unreal.GameplayStatics.get_all_actors_of_class(
		world, unreal.MCReplicaPropCluster)
	muted = 0
	for actor in clusters:
		for comp in actor.get_components_by_class(
				unreal.InstancedStaticMeshComponent):
			try:
				comp.set_editor_property("cast_shadow", False)
				muted += 1
			except Exception:
				pass
	# NOTE: as of this writing the mutation above does not stop the crash on its
	# own -- an editor-side property write does not mark the render state dirty,
	# so the shadow pass still gathers these clusters. Disabling the sun's
	# shadows is what actually unblocks the renderer. Mute the clusters anyway:
	# once the legacy layer is retired this becomes moot, and until then it is
	# one less thing that can shadow the real geometry.
	say("legacy voxel clusters: %d actors, %d components muted for shadows"
		% (len(clusters), muted))
	return muted


def main():
	subsystems = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
	world = subsystems.get_editor_world() if subsystems else None
	if world is None:
		# Fall back: the level actors list is available even when the world
		# handle is not, which is enough to decide "do the lights already exist".
		world = unreal.EditorLevelLibrary.get_editor_world()
	if world is None:
		say("FAIL: no editor world")
		return 1

	sun = find_existing(world, unreal.DirectionalLight, "MC_Sun")
	skylight = find_existing(world, unreal.SkyLight, "MC_SkyLight")

	if sun is None:
		make_sun(world)
	else:
		say("sun already present, left alone")
	if skylight is None:
		make_skylight(world)
	else:
		say("skylight already present, left alone")

	mute_legacy_clusters(world)

	# Census after, so the result is measured rather than assumed.
	suns = unreal.GameplayStatics.get_all_actors_of_class(
		world, unreal.DirectionalLight)
	skies = unreal.GameplayStatics.get_all_actors_of_class(
		world, unreal.SkyLight)
	say("after: DirectionalLight=%d SkyLight=%d" % (len(suns), len(skies)))

	if not suns:
		say("FAIL: no DirectionalLight in the level after the fix -- nothing "
			"will be lit")
		return 1

	unreal.EditorLevelLibrary.save_current_level()
	say("level saved")
	say("done")
	return 0


if __name__ == "__main__":
    sys.exit(main())