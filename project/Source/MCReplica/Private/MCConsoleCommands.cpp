// A console command that captures the frame and reports what is in it.
//
// The Python route cannot be used for this. Under -game, -ExecutePythonScript
// is processed before the game viewport exists and the script never runs, and
// HighResShot needs a window that this session does not have. A console
// command does run in -game, and this one reads the pixels through the RHI,
// which needs no window at all.
//
//   MCCapture            capture and report
//   MCCapture <path>     capture to a specific file

#include "MCConsoleCommands.h"

#include "MCFrameCapture.h"
#include "MCReplicaCharacter.h"

#include "Components/StaticMeshComponent.h"
#include "Components/InstancedStaticMeshComponent.h"
#include "Engine/Engine.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/StaticMesh.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "UnrealEngine.h"
#include "GameFramework/Character.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/PlayerController.h"
#include "HAL/FileManager.h"
#include "HAL/IConsoleManager.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "PhysicsEngine/BodySetup.h"
#include "ProceduralMeshComponent.h"
#include "MCReplicaPropCluster.h"
#include "Components/HierarchicalInstancedStaticMeshComponent.h"
#include "TimerManager.h"

DEFINE_LOG_CATEGORY(LogMCFrame);

namespace
{
	/**
	 * Append one line of pawn/collision state to Saved/mc_runtime.txt.
	 *
	 * A Shipping build compiles UE_LOG out and writes no log file, so state has
	 * to be written directly. Kept as a free function because both the game
	 * mode's timer and the MCRuntimeDiag console command need it, and the
	 * console command works even when the level's game mode is not ours.
	 */
	void WriteMCDiagLine(UWorld* W)
	{
		if (!W)
		{
			return;
		}

		int32 MeshTotal = 0, TerrainVisual = 0, TerrainCollision = 0;
		for (TActorIterator<AStaticMeshActor> It(W); It; ++It)
		{
			++MeshTotal;
			const UStaticMeshComponent* C = It->GetStaticMeshComponent();
			const UStaticMesh* M = C ? C->GetStaticMesh() : nullptr;
			if (!M)
			{
				continue;
			}
			// Match on the mesh asset name, not the actor name: an actor's name
			// is not what the level builder labelled it, and the label does not
			// survive a cook.
			const FString MN = M->GetName();
			if (MN.Contains(TEXT("overworld")))
			{
				if (MN.StartsWith(TEXT("C_")))
				{
					++TerrainCollision;
				}
				else
				{
					++TerrainVisual;
				}
			}
		}

		APlayerController* PC = W->GetFirstPlayerController();
		APawn* Pawn = PC ? PC->GetPawn() : nullptr;

		// Frame time from the world's own delta. GAverageFPS/GAverageMS are the
		// usual way to show this but they are not exported by this engine build
		// (no declaration survives in the installed headers), and a missing
		// symbol here would cost a build cycle to discover.
		const double DeltaSec = W->GetDeltaSeconds();
		FString Line = FString::Printf(
			TEXT("[%6.1fs] frame=%.1fms fps=%.0f pawn=%s"),
			W->GetTimeSeconds(), DeltaSec * 1000.0,
			DeltaSec > 0.0 ? 1.0 / DeltaSec : 0.0,
			Pawn ? TEXT("yes") : TEXT("NO"));

		if (Pawn)
		{
			const FVector L = Pawn->GetActorLocation();
			const FVector V = Pawn->GetVelocity();
			bool bOnGround = false;
			int32 Mode = -1;
			if (const ACharacter* C = Cast<ACharacter>(Pawn))
			{
				const UCharacterMovementComponent* Move = C->GetCharacterMovement();
				bOnGround = Move->IsMovingOnGround();
				Mode = static_cast<int32>(Move->MovementMode);
			}
			Line += FString::Printf(
				TEXT(" loc=(%.0f,%.0f,%.0f) vz=%.0f speed=%.0f onGround=%d mode=%d"),
				L.X, L.Y, L.Z, V.Z, V.Size(), bOnGround ? 1 : 0, Mode);
		}

		// Where the camera is and where it looks. "The campus is invisible"
		// is ambiguous between "nothing is there" and "the camera is pointed
		// somewhere else"; the camera's own forward trace separates them.
		if (PC && PC->PlayerCameraManager)
		{
			const FVector Cam = PC->PlayerCameraManager->GetCameraLocation();
			const FRotator CamR = PC->PlayerCameraManager->GetCameraRotation();
			Line += FString::Printf(
				TEXT(" | cam=(%.0f,%.0f,%.0f) rot=(p%.0f y%.0f)"),
				Cam.X, Cam.Y, Cam.Z, CamR.Pitch, CamR.Yaw);

			FCollisionQueryParams CP(SCENE_QUERY_STAT(MCDiagCam), true);
			if (Pawn)
			{
				CP.AddIgnoredActor(Pawn);
			}
			const FVector Fwd = CamR.Vector();
			FHitResult CH;
			if (W->LineTraceSingleByChannel(CH, Cam, Cam + Fwd * 200000.0,
				ECC_Visibility, CP))
			{
				Line += FString::Printf(TEXT(" fwd=%s d=%.0f"),
					*CH.GetActor()->GetName(), CH.Distance);
			}
			else
			{
				Line += TEXT(" fwd=NONE");
			}
		}

		// One-time dump of what each terrain mesh believes about its collision.
		// A cooked level can hold the actors and still trace nothing, which is
		// exactly the falling-pawn symptom; this separates "no collision
		// surface", "wrong channel" and "collision disabled".
		static bool bDumpedDetail = false;
		if (!bDumpedDetail)
		{
			bDumpedDetail = true;
			for (TActorIterator<AStaticMeshActor> It(W); It; ++It)
			{
				const UStaticMeshComponent* C = It->GetStaticMeshComponent();
				const UStaticMesh* M = C ? C->GetStaticMesh() : nullptr;
				if (!M || !M->GetName().Contains(TEXT("overworld")))
				{
					continue;
				}
				const UBodySetup* BS = M->GetBodySetup();
				const FBoxSphereBounds B = C->Bounds;
				int32 RenderVerts = -1, RenderTris = -1;
				if (const FStaticMeshRenderData* RD = M->GetRenderData())
				{
					if (RD->LODResources.Num() > 0)
					{
						RenderVerts = (int32)RD->LODResources[0].GetNumVertices();
						RenderTris = (int32)RD->LODResources[0].GetNumTriangles();
					}
				}
				Line += FString::Printf(
					TEXT("\n  [detail] %s rverts=%d rtris=%d cpu=%d flag=%d "
					     "trimesh=%d bounds=(%.0f,%.0f)+(%.0f,%.0f)"),
					*M->GetName(), RenderVerts, RenderTris,
					M->bAllowCPUAccess ? 1 : 0,
					BS ? (int32)BS->CollisionTraceFlag : -1,
					BS ? BS->TriMeshGeometries.Num() : -1,
					B.Origin.X, B.Origin.Y, B.BoxExtent.X, B.BoxExtent.Y);
			}
		}

		// Trace the whole spawn column rather than from the pawn: once the pawn
		// is below the world a downward trace from it looks away from the
		// terrain and reports a misleading miss. Both flavours, because a
		// complex-as-simple mesh can block a simple trace while its cooked
		// trimesh is empty.
		{
			FCollisionQueryParams P(SCENE_QUERY_STAT(MCDiag), /*bTraceComplex*/ true);
			if (Pawn)
			{
				P.AddIgnoredActor(Pawn);
			}
			const FVector Start(0, 0, 40000.0);
			const FVector End(0, 0, -40000.0);
			FHitResult Hit;
			if (W->LineTraceSingleByChannel(Hit, Start, End, ECC_Visibility, P))
			{
				Line += FString::Printf(TEXT(" | colCplx=%s z=%.0f nz=%.2f"),
					*Hit.GetActor()->GetName(), Hit.ImpactPoint.Z,
					Hit.ImpactNormal.Z);
			}
			else
			{
				Line += TEXT(" | colCplx=NONE");
			}
			FHitResult HitS;
			if (W->LineTraceSingleByChannel(HitS, Start, End, ECC_Visibility,
				FCollisionQueryParams(SCENE_QUERY_STAT(MCDiag), false)))
			{
				Line += FString::Printf(TEXT(" colSmpl=%s z=%.0f"),
					*HitS.GetActor()->GetName(), HitS.ImpactPoint.Z);
			}
			else
			{
				Line += TEXT(" colSmpl=NONE");
			}
		}

		Line += FString::Printf(
			TEXT(" | meshes=%d terrain(vis=%d col=%d)"),
			MeshTotal, TerrainVisual, TerrainCollision);

		// Prop census. The props are instanced clusters, so counting actors says
		// nothing -- an empty instanced component looks exactly like a populated
		// one. The nearest instance's height against the terrain is what tells
		// whether they are buried, floating, or placed correctly.
		{
			int32 IsmComps = 0;
			int64 InstTotal = 0;
			float NearestDist = -1.0f;
			FVector NearestLoc = FVector::ZeroVector;
			const FVector Ref = Pawn ? Pawn->GetActorLocation() : FVector::ZeroVector;
			for (TActorIterator<AActor> It(W); It; ++It)
			{
				TArray<UInstancedStaticMeshComponent*> Comps;
				It->GetComponents(Comps);
				for (UInstancedStaticMeshComponent* C : Comps)
				{
					if (!C)
					{
						continue;
					}
					++IsmComps;
					const int32 N = C->GetInstanceCount();
					InstTotal += N;
					for (int32 i = 0; i < N; ++i)
					{
						FTransform T;
						if (!C->GetInstanceTransform(i, T, /*bWorldSpace*/ true))
						{
							continue;
						}
						const float D = (float)FVector::Dist(T.GetLocation(), Ref);
						if (NearestDist < 0.0f || D < NearestDist)
						{
							NearestDist = D;
							NearestLoc = T.GetLocation();
						}
					}
				}
			}
			Line += FString::Printf(
				TEXT(" | propISM=%d propInst=%lld"),
				IsmComps, (long long)InstTotal);
			if (NearestDist >= 0.0f)
			{
				Line += FString::Printf(
					TEXT(" nearest=(%.0f,%.0f,%.0f) d=%.0f dz=%.0f"),
					NearestLoc.X, NearestLoc.Y, NearestLoc.Z, NearestDist,
					NearestLoc.Z - (Pawn ? Pawn->GetActorLocation().Z : 0.0f));
			}
		}

		const FString Text = Line + TEXT("\n");
		for (const FString& Path : {
			FPaths::ProjectSavedDir() / TEXT("mc_runtime.txt"),
			FString(TEXT("Q:/MC2UE5/logs/mc_runtime.txt")) })
		{
			IFileManager::Get().MakeDirectory(*FPaths::GetPath(Path), /*Tree*/ true);
			FFileHelper::SaveStringToFile(Text, *Path,
				FFileHelper::EEncodingOptions::ForceUTF8,
				&IFileManager::Get(), FILEWRITE_Append);
		}
	}

	FTimerHandle GDiagTimer;
}

AMCFrameCaptureGameMode::AMCFrameCaptureGameMode()
{
	// The walking student. Set here rather than from a Python-written CDO
	// because a C++ class's CDO is not serialised into the map -- the Python
	// value was gone the moment a new process started, which is how the game
	// ended up spawning the engine's DefaultPawn while the level verified with
	// the right class.
	static ConstructorHelpers::FClassFinder<APawn> PawnFinder(
		TEXT("/Script/MCReplica.MCReplicaCharacter"));
	if (PawnFinder.Succeeded())
	{
		DefaultPawnClass = PawnFinder.Class;
	}
}

namespace
{
	/**
	 * One-shot delayed capture.
	 *
	 * -ExecCmds runs while the command line is still being processed, before a
	 * game world exists, so MCCapture issued that way finds nothing. Retrying on
	 * a timer from BeginWorldPlay is what makes the capture land after the
	 * level has streamed in and the pawn has settled -- which is the only point
	 * at which a frame is worth looking at.
	 */
	struct FDelayedCapture
	{
		double Elapsed = 0.0;
		double Delay = 6.0;
		bool Done = false;
	};

	FDelayedCapture GDelayed;
}

void AMCFrameCaptureGameMode::BeginPlay()
{
	Super::BeginPlay();

	// Off by default: the voxel block layer is the terrain now. See
	// BuildProceduralTerrain() -- running both surfaces z-fights (shimmering
	// ground), gives the capsule a smooth collision surface that disagrees with
	// the stepped blocks (floating character), and doubles the collision cost
	// (stutter).
	if (bBuildLegacyTerrain)
	{
		BuildProceduralTerrain();
	}

	// Start the runtime report here rather than from -ExecCmds: that runs while
	// the command line is still being processed, before a game world exists, and
	// the console command needs a world. A flag read at BeginPlay is the point
	// at which "the game is actually running" is true.
	//
	// MCReplica.exe -MCdiag
	if (FParse::Param(FCommandLine::Get(), TEXT("MCdiag")))
	{
		UWorld* W = GetWorld();
		if (W)
		{
			WriteMCDiagLine(W);
			W->GetTimerManager().SetTimer(
				GDiagTimer,
				FTimerDelegate::CreateLambda([W]()
				{
					WriteMCDiagLine(W);
				}),
				1.0f, /*bLoop*/ true);
		}
	}

#if WITH_EDITOR
	// Everything below is editor-side diagnostic scaffolding: it rebuilds the
	// terrain physics at load so a headless -game session can probe it, and
	// logs a census of the level. A cooked Shipping build has the physics baked
	// by the cooker and no actor labels (GetActorLabel is editor-only), so the
	// shipped game mode does nothing here -- which is what shipping wants.
	// ---- build the terrain's physics data --------------------------------
	//
	// The collision proxies are marked "use complex as simple", which tells
	// the engine to trace against the render triangles. A flag alone is not
	// enough: the BodySetup still has to have its physics meshes built, and an
	// OBJ imported through AssetImportTask never gets that step -- the editor
	// builds collision lazily when a mesh is opened, which a headless build
	// never does.
	//
	// The result is a terrain that passes every static check -- four proxies,
	// the right trace flag, a blocking profile -- and stops nothing. The pawn
	// walks straight through the ground and keeps falling, which is the
	// "black figure falling" from the bug report, and the reason the campus is
	// never reached.
	//
	// CreatePhysicsMeshes() is the step that was missing. It cooks the render
	// triangles into the physics format here, at load, so it works whatever
	// the cook state of the asset.
	int32 PhysicsBuilt = 0;
	for (TActorIterator<AStaticMeshActor> It(GetWorld()); It; ++It)
	{
		const FString Label = It->GetActorLabel();
		if (!Label.StartsWith(TEXT("TerrainCollision_")))
		{
			continue;
		}
		UStaticMeshComponent* Comp = It->GetStaticMeshComponent();
		UStaticMesh* Mesh = Comp ? Comp->GetStaticMesh() : nullptr;
		UBodySetup* Body = Mesh ? Mesh->GetBodySetup() : nullptr;
		if (!Body)
		{
			continue;
		}

		// A complex-as-simple collision proxy cooks its physics trimesh from
		// the mesh's CPU-accessible render data. StaticMesh.cpp gates that copy
		// on bAllowCPUAccess (GetPhysicsTriMeshData returns false without it,
		// and the RHI buffer CPU shadow is only kept when the flag is set at
		// InitResources time). A mesh loaded with the flag false has no CPU
		// shadow, so flipping the flag alone is not enough -- the render data
		// must be rebuilt with the flag true before the trimesh is cooked.
		//
		// The asset is persisted with the flag on disk by set_cpu_access.py and
		// by the import pipeline, so a clean load already retains the data. This
		// branch is the load-time fallback for levels that were imported before
		// the flag existed, and for any DDC state that did not pick up the save.
		if (Mesh && !Mesh->bAllowCPUAccess)
		{
			Mesh->bAllowCPUAccess = true;
			Mesh->ReleaseResources();
			Mesh->InitResources();
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: %s forced bAllowCPUAccess=true and rebuilt render data"),
				*Label);
		}

		Body->CollisionTraceFlag = CTF_UseComplexAsSimple;
		Body->InvalidatePhysicsData();
		Body->CreatePhysicsMeshes();
		Comp->RecreatePhysicsState();

		// The component's collision state is set here rather than trusting the
		// Python-imported values. The Python side sets these through
		// set_editor_property with names guessed from the Blueprint surface
		// ("collision_profile_name" is not an editor property on this class --
		// the profile lives inside BodyInstance), and a failed set inside that
		// try/except is invisible: the component reads back as configured while
		// blocking nothing, which is exactly the falling-pawn symptom.
		Comp->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
		Comp->SetCollisionProfileName(TEXT("BlockAll"));
		Comp->SetGenerateOverlapEvents(false);
		Comp->SetCollisionResponseToChannel(ECC_Pawn, ECR_Block);
		Comp->RecreatePhysicsState();
		++PhysicsBuilt;

		// Ground-truth diagnostic. A "built" collision proxy that still lets
		// the pawn fall has exactly one of three causes, and this line
		// separates them:
		//   - cpuAccess=0        -> the imported mesh dropped its CPU vertex
		//     copy, so CreatePhysicsMeshes() cannot cook a queryable trimesh;
		//   - respVis=0          -> the component is not set to block the
		//     traced channel, so the geometry exists but is ignored;
		//   - neither            -> the spawn column is simply over a gap in
		//     the terrain and the miss is geometrically correct.
		const UStaticMesh* M = Comp->GetStaticMesh();
		const FBoxSphereBounds B = Comp->CalcBounds(
			Comp->GetComponentTransform());
		UE_LOG(LogMCFrame, Warning,
			TEXT("frame: %s mesh=%s cpuAccess=%d body=%d "
			     "respVis=%d respPawn=%d enabled=%d bounds=%s"),
			*Label, M ? *M->GetName() : TEXT("null"),
			M ? (M->bAllowCPUAccess ? 1 : 0) : -1,
			M && M->GetBodySetup() ? 1 : 0,
			(int32)Comp->GetCollisionResponseToChannel(ECC_Visibility),
			(int32)Comp->GetCollisionResponseToChannel(ECC_Pawn),
			(int32)Comp->GetCollisionEnabled(),
			*B.ToString());
	}
	UE_LOG(LogMCFrame, Warning,
		TEXT("frame: physics built for %d collision proxies"), PhysicsBuilt);

	// ---- the definitive collision test -----------------------------------
	// A straight-down line through the spawn point, tried on two channels and
	// as both complex and simple. With complex-as-simple the simple and
	// complex variants must agree; a blanket miss localises the fault to the
	// cooked mesh or the channel response rather than the trace geometry.
	auto Probe = [&](const TCHAR* Tag, ECollisionChannel Channel, bool bComplex)
	{
		FHitResult Hit;
		FCollisionQueryParams P(SCENE_QUERY_STAT(MCProbe), bComplex);
		const FVector Start(0.0, 0.0, 20000.0);
		const FVector End(0.0, 0.0, -20000.0);
		const bool bHit = GetWorld()->LineTraceSingleByChannel(
			Hit, Start, End, Channel, P);
		if (bHit)
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: probe[%s] HITS %s z=%.0f"),
				Tag, *Hit.GetActor()->GetActorLabel(), Hit.ImpactPoint.Z);
		}
		else
		{
			UE_LOG(LogMCFrame, Warning, TEXT("frame: probe[%s] MISSES"),
				Tag);
		}
	};
	Probe(TEXT("Vis/Cplx"), ECC_Visibility, true);
	Probe(TEXT("Vis/Smpl"), ECC_Visibility, false);
	Probe(TEXT("Wld/Cplx"), ECC_WorldStatic, true);
	Probe(TEXT("Wld/Smpl"), ECC_WorldStatic, false);

	// Report the world before anything is drawn: the two facts that explain
	// the reported symptoms -- a pawn falling forever, and terrain reading as a
	// distant island -- are both visible here.
	const APlayerController* PC = GetWorld() ? GetWorld()->GetFirstPlayerController()
		: nullptr;
	if (PC && PC->GetPawn())
	{
		const FVector L = PC->GetPawn()->GetActorLocation();
		UE_LOG(LogMCFrame, Warning,
			TEXT("frame: pawn at (%.0f, %.0f, %.0f)"), L.X, L.Y, L.Z);
	}
	// The PlayerStart actor rather than AWorldSettings::GetSpawnLocation,
	// which does not exist on this engine version.
	if (UWorld* W = GetWorld())
	{
		for (TActorIterator<AActor> It(W); It; ++It)
		{
			if (It->GetActorLabel() == TEXT("PlayerStart"))
			{
				const FVector L = It->GetActorLocation();
				UE_LOG(LogMCFrame, Warning,
					TEXT("frame: PlayerStart at (%.0f, %.0f, %.0f)"),
					L.X, L.Y, L.Z);
				break;
			}
		}

		int32 Terrain = 0, Props = 0;
		for (TActorIterator<AActor> It2(W); It2; ++It2)
		{
			const FString Label = It2->GetActorLabel();
			if (Label.StartsWith(TEXT("Terrain_")))
			{
				++Terrain;
			}
			else if (Label.StartsWith(TEXT("Prop_")) ||
			         Label.StartsWith(TEXT("Props_")))
			{
				++Props;
			}
		}
		UE_LOG(LogMCFrame, Warning,
			TEXT("frame: %d terrain actors, %d prop actors"), Terrain, Props);
	}

	// The capture fires from a world timer rather than from Tick.
	//
	// AGameModeBase does not tick unless asked to, and relying on that is how
	// the first version of this capture silently never ran: BeginPlay logged
	// its census and then nothing happened, with no error anywhere. A world
	// timer is explicit about firing.
	//
	// The 8 s delay lets World Partition stream the campus in and the pawn
	// settle onto the ground; earlier and the frame shows a half-loaded world
	// that proves nothing either way.
	GetWorldTimerManager().SetTimer(
		GCaptureHandle,
		FTimerDelegate::CreateUObject(this,
			&AMCFrameCaptureGameMode::RunCapture),
		40.0f, /*bLoop*/ false);
#endif // WITH_EDITOR

	// Runtime collision diagnostic. Always on: it is the only way to see inside a
	// Shipping build, and it costs one line a second. Remove or gate it once the
	// gameplay state is known good.
	GetWorldTimerManager().SetTimer(
		GDiagHandle,
		FTimerDelegate::CreateUObject(this,
			&AMCFrameCaptureGameMode::RunRuntimeDiag),
		1.0f, /*bLoop*/ true);
}

void AMCFrameCaptureGameMode::RunRuntimeDiag()
{
	WriteMCDiagLine(GetWorld());
}

void AMCFrameCaptureGameMode::MCBlockCollision(float Enable)
{
	UWorld* W = GetWorld();
	if (!W)
	{
		return;
	}

	const bool bOn = Enable != 0.0f;
	int32 Touched = 0;
	for (TActorIterator<AMCReplicaPropCluster> It(W); It; ++It)
	{
		UHierarchicalInstancedStaticMeshComponent* C = It->Instances;
		if (!C)
		{
			continue;
		}
		if (bOn)
		{
			C->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
			C->SetCollisionProfileName(TEXT("BlockAll"));
		}
		else
		{
			C->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		}
		++Touched;
	}

	UE_LOG(LogMCFrame, Warning,
		TEXT("MCBlockCollision %s on %d clusters"), bOn ? TEXT("ON") : TEXT("OFF"),
		Touched);
}

void AMCFrameCaptureGameMode::BuildProceduralTerrain()
{
	UWorld* W = GetWorld();
	if (!W)
	{
		return;
	}

	// One entry per phase-2 tile. Origin and the height decode come from
	// out/phase2/overworld/landscape/landscape.json:
	//     z_cm = actor_offset_z_cm + (v - 32768) / 128 * z_scale_cm
	// and 1 vertex == 1 block == 100 cm, so a vertex's world XY is the tile
	// origin plus its grid index in blocks.
	struct FTileSpec
	{
		const TCHAR* Name;
		double OriginX;
		double OriginY;
	};
	static const FTileSpec Tiles[] = {
		{ TEXT("overworld_00_00"), -27200.0, -67200.0 },
		{ TEXT("overworld_00_01"),   8800.0, -67200.0 },
		{ TEXT("overworld_01_00"), -27200.0, -15200.0 },
		{ TEXT("overworld_01_01"),   8800.0, -15200.0 },
	};

	const int32 GridW = 373;
	const int32 GridH = 528;
	const double BlockCm = 100.0;
	const double ZOffsetCm = 3350.0;
	const double ZScaleCm = 11.523438;

	// Replace any imported terrain. Those actors have correct bounds but almost
	// no triangles (see this function's declaration), and their collision
	// proxies would fight the procedural mesh's own collision.
	int32 Removed = 0;
	for (TActorIterator<AStaticMeshActor> It(W); It; ++It)
	{
		const UStaticMeshComponent* C = It->GetStaticMeshComponent();
		const UStaticMesh* M = C ? C->GetStaticMesh() : nullptr;
		if (M && M->GetName().Contains(TEXT("overworld")))
		{
			It->Destroy();
			++Removed;
		}
	}

	UMaterialInterface* Mat = LoadObject<UMaterialInterface>(
		nullptr, TEXT("/Game/MC/Materials/MC_Terrain.MC_Terrain"));

	int32 Built = 0;
	for (const FTileSpec& T : Tiles)
	{
		const FString Path = FPaths::ProjectDir() / TEXT("Terrain") /
			(FString(T.Name) + TEXT(".u16"));
		TArray<uint8> Raw;
		if (!FFileHelper::LoadFileToArray(Raw, *Path))
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: terrain data missing: %s"), *Path);
			continue;
		}
		if (Raw.Num() < GridW * GridH * 2)
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: terrain data short: %s (%d bytes)"),
				*Path, Raw.Num());
			continue;
		}
		const uint16* Hm = reinterpret_cast<const uint16*>(Raw.GetData());

		TArray<FVector> Verts;
		TArray<FVector2D> UVs;
		TArray<FVector> Normals;
		TArray<int32> Indices;
		Verts.Reserve(GridW * GridH);
		UVs.Reserve(GridW * GridH);
		Normals.Reserve(GridW * GridH);
		Indices.Reserve((GridW - 1) * (GridH - 1) * 6);

		auto SampleZ = [&](int32 c, int32 r) -> double
		{
			c = FMath::Clamp(c, 0, GridW - 1);
			r = FMath::Clamp(r, 0, GridH - 1);
			return ZOffsetCm + ((double)Hm[r * GridW + c] - 32768.0) / 128.0
				* ZScaleCm;
		};

		for (int32 r = 0; r < GridH; ++r)
		{
			for (int32 c = 0; c < GridW; ++c)
			{
				Verts.Add(FVector(T.OriginX + c * BlockCm,
					T.OriginY + r * BlockCm, SampleZ(c, r)));
				// UVs in block units, matching what the material expects
				// (WorldPosition-based) and the OBJ the pipeline produced.
				UVs.Add(FVector2D((float)c, (float)r));

				// Central-difference normal, oriented upward.
				const FVector Dx(
					c + 1 < GridW ? 2.0 * BlockCm : BlockCm, 0.0,
					SampleZ(c + 1, r) - SampleZ(c - 1, r));
				const FVector Dy(0.0,
					r + 1 < GridH ? 2.0 * BlockCm : BlockCm,
					SampleZ(c, r + 1) - SampleZ(c, r - 1));
				FVector N = FVector::CrossProduct(Dx, Dy).GetSafeNormal();
				if (N.Z < 0.0)
				{
					N = -N;
				}
				Normals.Add(N);
			}
		}

		for (int32 r = 0; r < GridH - 1; ++r)
		{
			for (int32 c = 0; c < GridW - 1; ++c)
			{
				const int32 A = r * GridW + c;
				const int32 B = A + 1;
				const int32 D = A + GridW;
				const int32 E = D + 1;
				// Same winding as tools/build_terrain_mesh.py, which was
				// verified to face upward.
				Indices.Add(A); Indices.Add(E); Indices.Add(B);
				Indices.Add(A); Indices.Add(D); Indices.Add(E);
			}
		}

		AActor* Holder = W->SpawnActor<AActor>();
		if (!Holder)
		{
			continue;
		}
		UProceduralMeshComponent* PMC = NewObject<UProceduralMeshComponent>(
			Holder, *FString::Printf(TEXT("Terrain_%s"), T.Name));
		if (!PMC)
		{
			Holder->Destroy();
			continue;
		}
		Holder->SetRootComponent(PMC);
		PMC->RegisterComponent();
		PMC->SetMobility(EComponentMobility::Movable);

		// bCreateCollision makes the component build a triangle-mesh body at
		// creation time -- no cooker involved, so it works in a packaged build.
		PMC->CreateMeshSection_LinearColor(0, Verts, Indices, Normals, UVs,
			TArray<FLinearColor>(), TArray<FProcMeshTangent>(),
			/*bCreateCollision*/ true);

		if (Mat)
		{
			PMC->SetMaterial(0, Mat);
		}
		PMC->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
		PMC->SetCollisionProfileName(TEXT("BlockAll"));
		PMC->bUseComplexAsSimpleCollision = false;
		PMC->SetCastShadow(true);

		++Built;
		UE_LOG(LogMCFrame, Warning,
			TEXT("frame: built terrain %s (%d verts, %d tris)"),
			T.Name, Verts.Num(), Indices.Num() / 3);
	}

	UE_LOG(LogMCFrame, Warning,
		TEXT("frame: procedural terrain built %d tiles, removed %d imported"),
		Built, Removed);
}

void AMCFrameCaptureGameMode::RunCapture()
{
	if (GDelayed.Done)
	{
		return;
	}
	GDelayed.Done = true;

	UE_LOG(LogMCFrame, Warning, TEXT("frame: capturing now"));

	// The pawn's position at capture time is the collision test. A pawn that
	// has fallen through the terrain reads as a large negative Z; the user's
	// report of "a black figure falling" is exactly this, and it is a physics
	// failure rather than a rendering one.
	for (TActorIterator<APawn> It(GetWorld()); It; ++It)
	{
		const FVector L = It->GetActorLocation();
		const FVector V = It->GetVelocity();
		UE_LOG(LogMCFrame, Warning,
			TEXT("frame: pawn %s at (%.0f, %.0f, %.0f) speed %.0f"),
			*It->GetClass()->GetName(), L.X, L.Y, L.Z, V.Size());
		if (L.Z < -1000.0f)
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: VERDICT the pawn has fallen below the terrain "
				     "-- collision is not stopping it"));
		}
	}

	const FString Out = FPaths::ProjectSavedDir() / TEXT("MCFrame") /
		TEXT("live.png");
	const FMCFrameStats S = UMCFrameCapture::CaptureViewport(this, Out);

	UE_LOG(LogMCFrame, Warning,
		TEXT("frame: valid=%d %dx%d colours=%d"),
		S.bValid ? 1 : 0, S.Width, S.Height, S.DistinctColours);
	UE_LOG(LogMCFrame, Warning,
		TEXT("frame: mean=(%d,%d) black=%.1f%% sky=%.1f%% lit=%.1f%%"),
		S.MeanRGB.X, S.MeanRGB.Y, S.DarkPercent, S.SkyPercent, S.LitPercent);
	UE_LOG(LogMCFrame, Warning, TEXT("frame: %s"), *S.Note);

	if (S.bValid)
	{
		if (S.LitPercent < 2.0f)
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: VERDICT nothing lit -- surfaces are black"));
		}
		else if (S.DarkPercent > 55.0f)
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: VERDICT geometry present but mostly black"));
		}
		else if (S.SkyPercent > 55.0f)
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: VERDICT mostly sky -- terrain is not filling "
				     "the view"));
		}
		else
		{
			UE_LOG(LogMCFrame, Warning,
				TEXT("frame: VERDICT sky and lit ground both present"));
		}
	}
}

static FAutoConsoleCommandWithWorld GMCCaptureNowCmd(
	TEXT("MCCaptureNow"),
	TEXT("Capture immediately (works only once a game world exists)."),
	FConsoleCommandWithWorldDelegate::CreateLambda([](UWorld* World)
	{
		const FString Out = FPaths::ProjectSavedDir() / TEXT("MCFrame") /
			TEXT("live.png");
		const FMCFrameStats S = UMCFrameCapture::CaptureViewport(World, Out);
		UE_LOG(LogMCFrame, Warning, TEXT("MCCaptureNow: %s"), *S.Note);
		UE_LOG(LogMCFrame, Warning,
			TEXT("MCCaptureNow: %dx%d black=%.1f%% sky=%.1f%% lit=%.1f%%"),
			S.Width, S.Height, S.DarkPercent, S.SkyPercent, S.LitPercent);
	}));

// Writes a per-second pawn/collision report to Saved/mc_runtime.txt. Exists
// because a Shipping build has no log file at all, and because the level's game
// mode cannot be assumed to be ours. Run it from the command line:
//
//     MCReplica.exe -ExecCmds="MCRuntimeDiag"
static FAutoConsoleCommandWithWorld GMCRuntimeDiagCmd(
	TEXT("MCRuntimeDiag"),
	TEXT("Start writing a per-second pawn/collision report to "
	     "Saved/mc_runtime.txt."),
	FConsoleCommandWithWorldDelegate::CreateLambda([](UWorld* World)
	{
		if (!World)
		{
			return;
		}
		// One line now, then one a second, so a single startup command leaves
		// behind a time series rather than a single sample.
		WriteMCDiagLine(World);
		World->GetTimerManager().SetTimer(
			GDiagTimer,
			FTimerDelegate::CreateLambda([World]()
			{
				WriteMCDiagLine(World);
			}),
			1.0f, /*bLoop*/ true);
	}));
