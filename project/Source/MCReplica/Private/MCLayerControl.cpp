#include "MCLayerControl.h"

#include "Components/HierarchicalInstancedStaticMeshComponent.h"
#include "Components/InstancedStaticMeshComponent.h"
#include "Components/LightComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "HAL/PlatformTime.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "MCReplicaPropCluster.h"

#include "Containers/Ticker.h"

DEFINE_LOG_CATEGORY(LogMCLayer);

// ---- the frame ring -------------------------------------------------------

namespace
{
	/**
	 * Frame times in ms, oldest first once full.
	 *
	 * 2048 entries is ~34 s at 60 fps, which is long enough to see whether a
	 * hitch is periodic and short enough that Consume (once a second) always
	 * finds fresh samples. A fixed array rather than a TArray because this is
	 * written every frame: no allocation, no growth, no allocator contention on
	 * the game thread -- adding a per-frame allocation to a performance
	 * investigation would be self-defeating.
	 */
	constexpr int32 FRAME_RING_SIZE = 2048;

	float GFrameRing[FRAME_RING_SIZE];
	int32 GFrameWrite = 0;
	int32 GFrameCount = 0;
	double GLastFrameTime = 0.0;
	bool GFrameRingRunning = false;

	/**
	 * One entry per frame, from a core ticker.
	 *
	 * A ticker rather than a world timer on purpose. A world timer fires in
	 * game time, which is exactly the clock whose distortion is under
	 * investigation; the core ticker is driven by the engine loop itself, so the
	 * interval it measures is the interval the player actually experienced.
	 */
	bool MCTickFrame(float /*DeltaTime*/)
	{
		const double Now = FPlatformTime::Seconds();
		if (GLastFrameTime > 0.0)
		{
			GFrameRing[GFrameWrite] = (float)((Now - GLastFrameTime) * 1000.0);
			GFrameWrite = (GFrameWrite + 1) % FRAME_RING_SIZE;
			if (GFrameCount < FRAME_RING_SIZE)
			{
				++GFrameCount;
			}
		}
		GLastFrameTime = Now;
		return true;   // keep ticking
	}

	/** Clamp a parsed token's count into a sane range. */
	int32 ClampCull(float V)
	{
		return FMath::Clamp(FMath::RoundToInt(V), 1000, 200000);
	}
}

void FMCTimeRing::Start()
{
	if (GFrameRingRunning)
	{
		return;
	}
	GFrameRingRunning = true;
	GLastFrameTime = 0.0;
	GFrameWrite = 0;
	GFrameCount = 0;
	FTSTicker::GetCoreTicker().AddTicker(
		FTickerDelegate::CreateLambda(&MCTickFrame), 0.0f);
	UE_LOG(LogMCLayer, Log, TEXT("MC: frame-time ring started"));
}

bool FMCTimeRing::IsRunning()
{
	return GFrameRingRunning;
}

FMCTimeStats FMCTimeRing::Consume()
{
	FMCTimeStats S;
	S.Samples = GFrameCount;
	if (GFrameCount == 0)
	{
		return S;
	}

	// Copy out and sort: at 2048 entries this is ~20 k comparisons, once a
	// second, on a report path that already writes a file. Cheaper than
	// maintaining an ordered structure on every frame, which is the alternative
	// and would tax the thing being measured.
	TArray<float> Sorted;
	Sorted.Reserve(GFrameCount);
	for (int32 i = 0; i < GFrameCount; ++i)
	{
		Sorted.Add(GFrameRing[i]);
	}
	Sorted.Sort();

	double Sum = 0.0;
	for (const float V : Sorted)
	{
		Sum += V;
	}
	S.MinMs = Sorted[0];
	S.MaxMs = Sorted.Last();
	S.MeanMs = (float)(Sum / Sorted.Num());
	S.P50Ms = Sorted[Sorted.Num() / 2];
	// p95 by index rather than a nearest-rank search: at this size the
	// difference is a frame nobody can feel, and this runs on a report path.
	S.P95Ms = Sorted[FMath::Clamp((int32)(Sorted.Num() * 0.95f), 0,
		Sorted.Num() - 1)];

	GFrameWrite = 0;
	GFrameCount = 0;
	return S;
}

const TCHAR* FMCTimeStats::Classification() const
{
	if (Samples < 8)
	{
		return TEXT("too-few-samples");
	}
	// Thresholds: 33 ms is 30 fps, 50 ms is 20. A p50 above 33 means the game
	// is below 30 fps *typically* -- a plateau. A p50 under 20 with a p95 over
	// 50 means it is usually fine and occasionally not -- spikes.
	const bool bPlateau = P50Ms > 33.0f;
	const bool bSpiky = P95Ms > 50.0f && P50Ms <= 20.0f;
	if (bPlateau && bSpiky)
	{
		return TEXT("mixed");
	}
	if (bPlateau)
	{
		return TEXT("plateau");
	}
	if (bSpiky)
	{
		return TEXT("spikes");
	}
	return TEXT("ok");
}

FString FMCTimeStats::Describe() const
{
	if (Samples == 0)
	{
		return TEXT("frames=0");
	}
	return FString::Printf(
		TEXT("n=%d min=%.1f p50=%.1f p95=%.1f max=%.1f mean=%.1f class=%s"),
		Samples, MinMs, P50Ms, P95Ms, MaxMs, MeanMs, Classification());
}

// ---- the layer spec -------------------------------------------------------

FMCLayerSpec FMCLayerSpec::FromCommandLine()
{
	FMCLayerSpec S;   // everything on

	FString Spec;
	// **The `false` is load-bearing.** `FParse::Value`'s FString overload
	// defaults `bShouldStopOnSeparator` to **true**, which adds `,` and `)` to
	// the terminating set (Parse.cpp:299: `",) \r\n\t"`). A comma-separated
	// spec was therefore silently truncated to its first token:
	//
	//     -MClayers=-vox,-struct   ->  Spec == "-vox"   (struct left ON)
	//
	// That made every multi-token layer A/B measure only its first token while
	// reporting success, which is the worst possible failure for an A/B switch.
	// With `false` the terminators are whitespace only, so the whole spec is
	// captured and split by the ParseIntoArray below.
	if (!FParse::Value(FCommandLine::Get(), TEXT("MClayers="), Spec,
			/*bShouldStopOnSeparator*/ false))
	{
		return S;
	}

	TArray<FString> Tokens;
	Spec.ParseIntoArray(Tokens, TEXT(","), /*bCullEmpty*/ true);

	for (FString Tok : Tokens)
	{
		Tok.TrimStartAndEndInline();
		if (Tok.IsEmpty())
		{
			continue;
		}

		// A leading '-' disables, '+' or nothing enables. Parsed by hand
		// because FParse::Value cannot express "this token was absent".
		bool bEnable = true;
		if (Tok[0] == TEXT('-'))
		{
			bEnable = false;
			Tok.RightChopInline(1);
		}
		else if (Tok[0] == TEXT('+'))
		{
			Tok.RightChopInline(1);
		}
		Tok.TrimStartAndEndInline();
		Tok.ToLowerInline();

		if (Tok == TEXT("none"))
		{
			S.bVox = S.bStruct = S.bTerrain = false;
			S.bShadow = S.bCollision = bEnable;
			continue;
		}
		if (Tok == TEXT("all"))
		{
			S.bVox = S.bStruct = S.bTerrain = bEnable;
			S.bShadow = S.bCollision = bEnable;
			continue;
		}

		if (Tok == TEXT("vox") || Tok == TEXT("voxel") || Tok == TEXT("blocks"))
		{
			S.bVox = bEnable;
		}
		else if (Tok == TEXT("struct") || Tok == TEXT("structures")
			|| Tok == TEXT("buildings"))
		{
			S.bStruct = bEnable;
		}
		else if (Tok == TEXT("terrain") || Tok == TEXT("tiles"))
		{
			S.bTerrain = bEnable;
		}
		else if (Tok == TEXT("shadow") || Tok == TEXT("shadows"))
		{
			S.bShadow = bEnable;
		}
		else if (Tok == TEXT("collision") || Tok == TEXT("collide"))
		{
			S.bCollision = bEnable;
		}
		else
		{
			// Loudly ignored rather than quietly accepted. A mistyped token
			// that left a layer on would make an A/B read as "this layer is
			// free" when in fact it was never disabled -- the one failure mode
			// that would make the whole bisection worthless.
			UE_LOG(LogMCLayer, Error,
				TEXT("MC: -MClayers: unknown token '%s'; layers are vox, "
				     "struct, terrain, shadow, collision, all, none"), *Tok);
		}
	}

	UE_LOG(LogMCLayer, Log, TEXT("MC: -MClayers=%s -> %s"), *Spec, *S.Describe());
	return S;
}

FString FMCLayerSpec::Describe() const
{
	return FString::Printf(TEXT("vox=%d struct=%d terrain=%d shadow=%d coll=%d"),
		bVox ? 1 : 0, bStruct ? 1 : 0, bTerrain ? 1 : 0,
		bShadow ? 1 : 0, bCollision ? 1 : 0);
}

FString FMCLayerCensus::Describe() const
{
	return FString::Printf(
		TEXT("vox(cl=%d n=%lld) struct=%d terrain(vis=%d col=%d) "
		     "shadowOff=%d collOff=%d"),
		VoxClusters, (long long)VoxInstances, StructMeshes,
		TerrainVisual, TerrainCollision,
		ShadowCastersDisabled, CollisionsDisabled);
}

// ---- applying the layers --------------------------------------------------

FMCLayerCensus ApplyMCLayers(UWorld* World, const FMCLayerSpec& Spec)
{
	FMCLayerCensus Census;
	if (!World)
	{
		return Census;
	}

	// ---- the voxel block layer -------------------------------------------
	//
	// The clusters are the 1,041 AMCReplicaPropCluster actors. They are turned
	// off by clearing visibility and collision rather than by destroying them:
	// this is a measurement switch, and destroying the layer would be
	// irreversible from inside the running game. S6 removes them properly, with
	// sign-off.
	//
	// Visibility is the important half. A hidden HISM still has its instances
	// in memory and still costs physics, but it stops costing draw calls,
	// shadow passes and -- because bUseGpuLodSelection is off -- the CPU
	// cluster-tree walk, which is where a million instances hurt most.
	if (!Spec.bVox)
	{
		for (TActorIterator<AMCReplicaPropCluster> It(World); It; ++It)
		{
			UHierarchicalInstancedStaticMeshComponent* C = It->Instances;
			if (!C)
			{
				continue;
			}
			C->SetVisibility(false, /*bPropagateToChildren*/ true);
			C->SetCollisionEnabled(ECollisionEnabled::NoCollision);
			C->SetCastShadow(false);
			++Census.VoxClusters;
			Census.VoxInstances += C->GetInstanceCount();
		}
	}
	else
	{
		// Counted even when left alone, so the census describes the scene
		// rather than describing the switches.
		for (TActorIterator<AMCReplicaPropCluster> It(World); It; ++It)
		{
			if (!It->Instances)
			{
				continue;
			}
			++Census.VoxClusters;
			Census.VoxInstances += It->Instances->GetInstanceCount();

			// The collision switch has to reach the block layer too, not just
			// the meshes. It exists to answer "is the capsule's ground check the
			// cost?", and the capsule stands on the blocks -- a collision switch
			// that skipped them measured nothing and reported zero, which is
			// precisely the kind of no-op that gets read as "collision is free".
			if (!Spec.bCollision)
			{
				It->Instances->SetCollisionEnabled(ECollisionEnabled::NoCollision);
				++Census.CollisionsDisabled;
			}
			if (!Spec.bShadow)
			{
				It->Instances->SetCastShadow(false);
				++Census.ShadowCastersDisabled;
			}
		}
	}

	// ---- structures and terrain ------------------------------------------
	//
	// Both are StaticMeshActors, separated by asset name: the structure assets
	// are ``bld_*`` and the terrain tiles are ``terrain_*`` (the older pipeline's
	// ``T_overworld_*`` visual / ``C_overworld_*`` collision names are still
	// accepted so this does not silently stop seeing a level built the old way).
	// Matching on the mesh rather than the actor is what makes this work in a
	// cooked build, where the editor's actor labels are gone.
	for (TActorIterator<AStaticMeshActor> It(World); It; ++It)
	{
		UStaticMeshComponent* Comp = It->GetStaticMeshComponent();
		UStaticMesh* Mesh = Comp ? Comp->GetStaticMesh() : nullptr;
		if (!Mesh)
		{
			continue;
		}
		const FString Name = Mesh->GetName();
		const bool bIsStruct = Name.StartsWith(TEXT("bld_"));
		const bool bIsTerrain = Name.Contains(TEXT("terrain_"))
			|| Name.Contains(TEXT("overworld"));
		if (!bIsStruct && !bIsTerrain)
		{
			continue;
		}
		if (bIsTerrain)
		{
			if (Name.StartsWith(TEXT("C_")))
			{
				++Census.TerrainCollision;
			}
			else
			{
				++Census.TerrainVisual;
			}
		}
		else
		{
			++Census.StructMeshes;
		}

		const bool bLayerOn = bIsStruct ? Spec.bStruct : Spec.bTerrain;
		if (!bLayerOn)
		{
			Comp->SetVisibility(false);
			Comp->SetCollisionEnabled(ECollisionEnabled::NoCollision);
			Comp->SetCastShadow(false);
		}

		// The collision switch is independent of the layer switch: it answers
		// "is the capsule's ground check the cost?" rather than "is this
		// geometry the cost?", so it must be able to fire with the layer still
		// visible.
		if (!Spec.bCollision)
		{
			Comp->SetCollisionEnabled(ECollisionEnabled::NoCollision);
			++Census.CollisionsDisabled;
		}
		if (!Spec.bShadow)
		{
			Comp->SetCastShadow(false);
			++Census.ShadowCastersDisabled;
		}
	}

	// ---- shadows, globally -----------------------------------------------
	//
	// A directional light's own CastShadow is the master switch for the whole
	// cascade chain. 1.17 M shadow-casting instances is the expensive case, and
	// this is the cheapest way to find out whether it is *this* expensive --
	// one flag rather than 1.17 M.
	if (!Spec.bShadow)
	{
		for (TActorIterator<AActor> It(World); It; ++It)
		{
			if (ULightComponent* L = It->FindComponentByClass<ULightComponent>())
			{
				if (L->CastShadows)
				{
					L->SetCastShadows(false);
					++Census.ShadowCastersDisabled;
				}
			}
		}
	}

	UE_LOG(LogMCLayer, Log, TEXT("MC: layers applied [%s] %s"),
		*Spec.Describe(), *Census.Describe());
	return Census;
}