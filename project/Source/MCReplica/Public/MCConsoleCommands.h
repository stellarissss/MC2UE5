// Deferred frame capture, driven from the game mode rather than the console.
//
// Written after three rounds of diagnosing a black screen without seeing a
// picture. The obstacle is that nothing that reaches the renderer works in
// this session: -ExecutePythonScript is processed before a game world exists,
// HighResShot needs a window, and a screen grab or PrintWindow has no window to
// target. A game mode's BeginPlay/Tick does run, once the level has streamed
// in, which is the only moment a frame is worth looking at.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"
#include "MCConsoleCommands.generated.h"

DECLARE_LOG_CATEGORY_EXTERN(LogMCFrame, Log, All);

/**
 * The project's game mode: GameModeBase plus a deferred screenshot.
 *
 * DefaultPawnClass is set in the constructor rather than from Python: a C++
 * class's CDO is not serialised into the map, so a value written from an
 * editor session is gone the moment a new process starts. That is how the
 * level verified with the right pawn class while the game spawned the engine's
 * DefaultPawn instead.
 */
UCLASS()
class MCREPLICA_API AMCFrameCaptureGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	AMCFrameCaptureGameMode();

	virtual void BeginPlay() override;

	/** The deferred screenshot, fired by a world timer 8 s into play. */
	void RunCapture();

	/**
	 * Write one line of pawn/collision state to Saved/mc_runtime.txt.
	 *
	 * A Shipping build compiles UE_LOG out and writes no log file at all, so the
	 * only way to see what the shipped game is doing is to write the state to
	 * disk directly. Gated on the ``mc.Diag`` cvar so it costs nothing unless
	 * asked for:
	 *
	 *     MCReplica.exe -ExecCmds="mc.Diag 1"
	 */
	/**
	 * Build the campus terrain as procedural meshes.
	 *
	 * **Superseded -- off by default, see ``bBuildLegacyTerrain``.**
	 *
	 * This was written when the terrain could not come from the imported OBJ
	 * tiles: the OBJ importer reads only the first ~640 vertices of a file
	 * (measured: a 12,502-vertex collision OBJ arrives as 1,122 verts, a
	 * 196,944-vertex visual tile as 2,179), so the meshes had correct bounds and
	 * almost no triangles, and the campus was invisible and had no collision.
	 *
	 * It triangulates the raw 16-bit heightfields shipped as ``<Tile>.u16``
	 * beside the executable, which fixed that -- but it is now the *wrong*
	 * layer. The level carries 1,171,144 individual voxel blocks whose tops are
	 * the real surface, and this mesh is a 4-block-averaged approximation of
	 * the same surface. Running both put two surfaces a few centimetres apart
	 * everywhere, which is what produced the reported symptoms:
	 *
	 *   * **speckle / shimmering ground** -- z-fighting between the two
	 *     coplanar surfaces;
	 *   * **a floating character** -- the ground-snap trace hits whichever
	 *     surface is higher, the smooth heightfield, while the visible blocks
	 *     are stepped, so the capsule rests above them;
	 *   * **severe stutter** -- this mesh builds ~1.57 M triangles of *complex*
	 *     collision (four triangle-mesh bodies) on top of the block layer's own
	 *     collision, and the camera boom sweeps against the combined broadphase
	 *     every frame.
	 *
	 * Kept rather than deleted because it is the only collision source if the
	 * voxel layer is ever dropped, and because the heightfield route is worth
	 * revisiting for distant terrain.
	 */
	void BuildProceduralTerrain();

	void RunRuntimeDiag();

	/**
	 * Turn collision on the voxel block layer on or off, live.
	 *
	 * Exists to settle one question with a measurement instead of an argument:
	 * the block layer is 1.17 M instances, and an instanced mesh with collision
	 * enabled creates a physics body **per instance**. Whether that is what
	 * makes the frame time collapse is worth ten seconds of A/B rather than an
	 * opinion, and a rebuild costs minutes.
	 *
	 *     MCReplica.exe -ExecCmds="MCBlockCollision 0"
	 */
	UFUNCTION(Exec)
	void MCBlockCollision(float Enable);

protected:
	/**
	 * Build the legacy heightfield terrain at BeginPlay.
	 *
	 * Default **false**: the voxel block layer is the terrain. Turning this on
	 * alongside it reproduces the z-fighting / floating / stutter described on
	 * ``BuildProceduralTerrain``.
	 */
	UPROPERTY(EditDefaultsOnly, Category = "MCReplica|Terrain")
	bool bBuildLegacyTerrain = false;

	/** Handle for the delayed capture timer. */
	FTimerHandle GCaptureHandle;

	/** Handle for the repeating runtime diagnostic timer. */
	FTimerHandle GDiagHandle;
};
