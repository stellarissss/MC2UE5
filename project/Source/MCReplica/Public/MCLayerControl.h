// Runtime A/B control over the world's geometry layers, plus an honest frame
// timer.
//
// Written for the P0 investigation (the game froze, the camera would not
// rotate, and CPU/GPU were not maxed). Two things here, both of which a rebuild
// cannot provide:
//
//   * **Layer switches.** Five candidates were on the table -- two geometry
//     layers at once, per-frame world iteration, shadow cost, doubled collision,
//     repeated SetActorLocation. Each costs a build-and-cook cycle to test by
//     hand, which is minutes per hypothesis. `-MClayers=` turns any subset off
//     in one launch, so a single run bisects the space.
//
//   * **A frame timer that can tell a plateau from a spike.** The existing
//     diagnostic sampled `W->GetDeltaSeconds()` once a second, which cannot
//     answer that question: one sample per second aliases anything faster, and
//     `MaxDeltaTime` clamps the value at 400 ms so the worst frames all report
//     the same number. Those two causes have opposite fixes -- a plateau is a
//     sustained per-frame cost, a spike is a periodic stall -- so the
//     distinction is worth a real ring buffer.
//
// Neither is wired into gameplay. Both are inert unless asked for on the
// command line.

#pragma once

#include "CoreMinimal.h"
#include "Logging/LogMacros.h"

// Declared here rather than kept private to the .cpp: DEFINE_LOG_CATEGORY in a
// .cpp only makes the name visible to that one translation unit, and this file
// is excluded from the module's unity build, so it compiles on its own. The
// declaration also means other translation units can log to this category.
DECLARE_LOG_CATEGORY_EXTERN(LogMCLayer, Log, All);

class UWorld;

/**
 * Which layers are active. Defaults are the shipping look: everything on.
 */
struct FMCLayerSpec
{
	/** The voxel block layer: 1,171,144 HISM instances across 1,041 clusters. */
	bool bVox = true;

	/** The new building/detail meshes (assets named ``bld_*``). */
	bool bStruct = true;

	/** The smooth terrain tiles (``T_overworld_*`` visual, ``C_*`` collision). */
	bool bTerrain = true;

	/** Shadow casting. Off clears ``CastShadow`` on every primitive. */
	bool bShadow = true;

	/** Collision on the voxel and structure layers. */
	bool bCollision = true;

	/**
	 * Parse ``-MClayers=<spec>``.
	 *
	 * Comma-separated tokens, applied left to right over the default
	 * (everything on). A bare token enables, a ``-`` prefix disables:
	 *
	 *     -MClayers=vox                  only the voxel layer
	 *     -MClayers=vox,struct           voxel + structures, terrain off
	 *     -MClayers=-shadow              everything except shadow casting
	 *     -MClayers=none                 nothing at all
	 *     -MClayers=none,vox             only the voxel layer
	 *     -MClayers=all                  the shipping look
	 *
	 * ``+name`` is accepted as a synonym for ``name``. Unrecognised tokens are
	 * reported rather than ignored: a typo that silently enabled the layer it
	 * meant to disable would make the whole measurement a lie.
	 */
	static FMCLayerSpec FromCommandLine();

	/** "vox,struct,terrain,shadow,collision" with the off ones marked. */
	FString Describe() const;
};

/** What a layer actually touched, so a no-op switch cannot look like a fix. */
struct FMCLayerCensus
{
	int32 VoxClusters = 0;
	int64 VoxInstances = 0;
	int32 StructMeshes = 0;
	int32 TerrainVisual = 0;
	int32 TerrainCollision = 0;
	int32 ShadowCastersDisabled = 0;
	int32 CollisionsDisabled = 0;

	FString Describe() const;
};

/**
 * Apply a layer spec to the world, live.
 *
 * Layers are found by **asset name**, not actor name: an actor's name is not
 * what the level builder labelled it and the label does not survive a cook,
 * whereas the mesh asset name does. That is the same reasoning the existing
 * terrain census uses.
 *
 * Returns what it touched. A layer that found nothing is reported as zero
 * rather than silently doing nothing, because "the switch did not fix it" and
 * "the switch had nothing to turn off" are different findings.
 */
FMCLayerCensus ApplyMCLayers(UWorld* World, const FMCLayerSpec& Spec);

/**
 * Percentiles over a window of game-thread frame times.
 */
struct FMCTimeStats
{
	int32 Samples = 0;
	float MinMs = 0.0f;
	float P50Ms = 0.0f;
	float P95Ms = 0.0f;
	float MaxMs = 0.0f;
	float MeanMs = 0.0f;

	/**
	 * "plateau", "spikes", "mixed" or "ok".
	 *
	 * The distinction the P0 report needs. A **plateau** (p50 itself high) is a
	 * sustained per-frame cost -- too much geometry, too much collision, a
	 * per-frame whole-world loop. **Spikes** (p50 fine, p95/max high) are a
	 * periodic stall -- a timer, a streaming hitch, a GC pass. These have
	 * opposite fixes, and a once-a-second sample cannot tell them apart.
	 */
	const TCHAR* Classification() const;

	FString Describe() const;
};

/**
 * A ring buffer of game-thread frame times.
 *
 * Fed from a core ticker rather than from the diagnostic's timer, so the
 * sampling rate is the frame rate and short hitches are not missed.
 */
class FMCTimeRing
{
public:
	/** Idempotent. Safe to call from BeginPlay with no argument parsing. */
	static void Start();

	/**
	 * Stats for the window since the last Consume, then reset.
	 *
	 * Consuming rather than peeking is deliberate: the diagnostic wants each
	 * report to describe the second that just passed, not all history, so a
	 * plateau and a spike stay distinguishable over a long session.
	 */
	static FMCTimeStats Consume();

	/** True between Start and process exit. */
	static bool IsRunning();
};