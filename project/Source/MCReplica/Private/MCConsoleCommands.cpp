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
#include "Engine/Engine.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/StaticMesh.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "GameFramework/Character.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/PlayerController.h"
#include "HAL/FileManager.h"
#include "HAL/IConsoleManager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "PhysicsEngine/BodySetup.h"
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

		FString Line = FString::Printf(TEXT("[%6.1fs] pawn=%s"),
			W->GetTimeSeconds(), Pawn ? TEXT("yes") : TEXT("NO"));

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
				Line += FString::Printf(
					TEXT("\n  [detail] %s enabled=%d respVis=%d cpu=%d flag=%d "
					     "trimesh=%d convex=%d"),
					*M->GetName(), (int32)C->GetCollisionEnabled(),
					(int32)C->GetCollisionResponseToChannel(ECC_Visibility),
					M->bAllowCPUAccess ? 1 : 0,
					BS ? (int32)BS->CollisionTraceFlag : -1,
					BS ? BS->TriMeshGeometries.Num() : -1,
					BS ? BS->AggGeom.ConvexElems.Num() : -1);
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
