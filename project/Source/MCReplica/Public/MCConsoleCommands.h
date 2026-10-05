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
	 * The terrain cannot come from the imported OBJ tiles: the OBJ importer
	 * reads only the first ~640 vertices of a file (measured: a 12,502-vertex
	 * collision OBJ arrives as 1,122 verts, a 196,944-vertex visual tile as
	 * 2,179), so the meshes have correct bounds and almost no triangles. The
	 * campus was therefore invisible and had no collision.
	 *
	 * Instead the raw 16-bit heightfields are shipped as ``<Tile>.u16`` beside
	 * the executable and triangulated here, at load. That also produces correct
	 * collision, built at runtime, which avoids the trimesh cooking problems a
	 * complex-as-simple StaticMesh has in an uncooked game.
	 */
	void BuildProceduralTerrain();

	void RunRuntimeDiag();

protected:
	/** Handle for the delayed capture timer. */
	FTimerHandle GCaptureHandle;

	/** Handle for the repeating runtime diagnostic timer. */
	FTimerHandle GDiagHandle;
};
