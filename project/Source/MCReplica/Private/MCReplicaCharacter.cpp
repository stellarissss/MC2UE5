// Character for the rebuilt campus: walks the terrain, climbs steps, and is
// dressed as a Minecraft-style blocky school student.

#include "MCReplicaCharacter.h"

#include "Camera/CameraComponent.h"
#include "Components/CapsuleComponent.h"
#include "Components/InputComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Components/StaticMeshComponent.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "Camera/CameraComponent.h"

AMCReplicaCharacter::AMCReplicaCharacter()
{
	PrimaryActorTick.bCanEverTick = true;

	// ---- capsule ---------------------------------------------------------
	// Minecraft proportions: 60 cm across, 200 cm tall. UE's capsule total
	// height is 2*HalfHeight + 2*Radius, so HalfHeight is (200-60)/2 = 70.
	// 180 cm tall, 68 cm across: a real adult. The campus is at 1 block = 1 m, so
	// a real capsule is what makes a 1 m doorway and a 1 m stair tread clear the
	// way they should -- a short capsule walked under lintels a person would hit.
	GetCapsuleComponent()->InitCapsuleSize(34.0f, 88.0f);
	GetCapsuleComponent()->SetCollisionProfileName(TEXT("Pawn"));

	// A character turns to face its movement rather than snapping, which keeps
	// the camera from whipping around when the view direction changes.
	bUseControllerRotationPitch = false;
	bUseControllerRotationYaw = false;
	bUseControllerRotationRoll = false;

	// ---- movement --------------------------------------------------------
	if (UCharacterMovementComponent* Move =
			Cast<UCharacterMovementComponent>(GetCharacterMovement()))
	{
		Move->MaxWalkSpeed = WalkSpeed;
		Move->MaxWalkSpeedCrouched = WalkSpeed * 0.4f;
		Move->GroundFriction = 8.0f;
		Move->BrakingDecelerationWalking = 1400.0f;
		Move->AirControl = 0.22f;

		// v = sqrt(2 * g * h) for the requested jump height.
		Move->JumpZVelocity = FMath::Sqrt(2.0f * 980.0f * JumpHeight);

		// Step over anything up to StepHeight, and keep the character walking
		// rather than launching when it meets the lip of a one-block ledge.
		//
		// 5.8 names this MaxStepHeight; WalkableFloorZ is private and only
		// reachable through its setter, so assigning the fields directly does
		// not compile.
		Move->MaxStepHeight = StepHeight;
		Move->SetWalkableFloorAngle(44.8f);   // the default; spelled out for clarity

		// The collision proxy is a stride-4 copy of the terrain, so its surface
		// is already smoothed at 4 m. A small perch radius stops the character
		// jittering when it crosses a proxy triangle, and the extra perch height
		// lets it hang slightly over a ledge instead of sliding off it.
		Move->PerchRadiusThreshold = 20.0f;
		Move->PerchAdditionalHeight = 12.0f;

		// Turn the body to face the direction it is travelling. Movement input
		// is camera-relative (see MoveForward/MoveRight); without this the body
		// keeps its spawn yaw and strafes sideways across the campus, which
		// reads as "the character will not walk where I am looking".
		Move->bOrientRotationToMovement = true;
		Move->RotationRate = FRotator(0.0f, 540.0f, 0.0f);
	}

	// ---- figure ----------------------------------------------------------
	// A skeletal mesh component already exists on ACharacter and is left empty;
	// hiding it keeps it from contributing an empty bounds entry.
	if (USkeletalMeshComponent* SkelMesh = GetMesh())
	{
		SkelMesh->SetVisibility(false);
		SkelMesh->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	}

	BodyRoot = CreateDefaultSubobject<USceneComponent>(TEXT("BodyRoot"));
	BodyRoot->SetupAttachment(GetCapsuleComponent());

	const float T = 25.0f;   // limb thickness, 25 cm
	const float W = 50.0f;   // head / torso width, 50 cm
	const float D = 25.0f;   // torso depth, 25 cm

	auto MakeLimb = [this](const TCHAR* Name, const FVector& Pivot)
	{
		UStaticMeshComponent* C =
			CreateDefaultSubobject<UStaticMeshComponent>(Name);
		C->SetupAttachment(BodyRoot);
		C->SetRelativeLocation(Pivot);
		C->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		C->SetGenerateOverlapEvents(false);
		C->SetCastShadow(true);
		return C;
	};

	// Pivots, not centres: an arm rotates about the shoulder, a leg about the
	// hip, so the component origin sits at the joint and the mesh geometry is
	// modelled hanging below it.
	Head = MakeLimb(TEXT("Head"), FVector(0.0f, 0.0f, 175.0f));
	Torso = MakeLimb(TEXT("Torso"), FVector(0.0f, 0.0f, 112.5f));
	ArmLeft = MakeLimb(TEXT("ArmLeft"), FVector(-37.5f, 0.0f, 150.0f));
	ArmRight = MakeLimb(TEXT("ArmRight"), FVector(37.5f, 0.0f, 150.0f));
	LegLeft = MakeLimb(TEXT("LegLeft"), FVector(-12.5f, 0.0f, 75.0f));
	LegRight = MakeLimb(TEXT("LegRight"), FVector(12.5f, 0.0f, 75.0f));

	// The skin and uniform meshes are assigned from Python once the textures
	// exist; record the intended dimensions so the mesh can be authored to fit.
	(void)T; (void)W; (void)D;

	// ---- camera: first person -------------------------------------------
	//
	// The camera is attached to the capsule at eye height and takes the
	// controller rotation directly. There is no spring arm: a boom exists to hold
	// a third-person camera away from the body, and for first person it would
	// only add camera lag, a collision test that pulls the view into the player's
	// own wall, and a parallax offset that makes the world swim while walking.
	Camera = CreateDefaultSubobject<UCameraComponent>(TEXT("Camera"));
	Camera->SetupAttachment(GetCapsuleComponent());
	Camera->SetRelativeLocation(FVector(0.0f, 0.0f, EyeHeight));
	Camera->bUsePawnControlRotation = true;
	Camera->SetFieldOfView(FieldOfView);

	// First person means the body is not in the picture. Hiding the whole figure
	// rather than trimming it to arms and legs keeps the blocky Minecraft
	// silhouette out of the frame entirely -- which matters here, because the
	// point of the render rework is a realistic-looking campus, and a voxel man
	// standing in it is the one thing that would still read as Minecraft.
	//
	// The limb components stay in the hierarchy: a proper viewmodel wants them
	// once there are real arm meshes to attach.
	if (BodyRoot)
	{
		BodyRoot->SetVisibility(false, /*bPropagateToChildren*/ true);
	}
}

void AMCReplicaCharacter::BeginPlay()
{
	Super::BeginPlay();

	if (UCharacterMovementComponent* Move =
			Cast<UCharacterMovementComponent>(GetCharacterMovement()))
	{
		// Re-apply the tunables here as well as in the constructor: the
		// defaults above are written before the properties are deserialised,
		// and an edited Blueprint would otherwise silently lose them.
		Move->MaxWalkSpeed = WalkSpeed;
		Move->MaxStepHeight = StepHeight;
		Move->JumpZVelocity = FMath::Sqrt(2.0f * 980.0f * JumpHeight);
	}

	// Drop onto whatever the level actually collides with.
	//
	// The PlayerStart height is derived from the source heightmap, but the
	// terrain collision mesh in the level is what the character can stand on,
	// and the two need not agree: the OBJ -> StaticMesh import does not
	// reproduce the heightfield faithfully, so the collidable surface can sit
	// well above or below the recorded spawn height. Spawning without this
	// check means spawning under the ground and falling forever.
	if (UWorld* W = GetWorld())
	{
		FHitResult Hit;
		FCollisionQueryParams P(SCENE_QUERY_STAT(MCGroundSnap), false);
		P.AddIgnoredActor(this);
		const FVector L = GetActorLocation();
		const FVector Start(L.X, L.Y, L.Z + 100000.0);
		const FVector End(L.X, L.Y, L.Z - 100000.0);
		if (W->LineTraceSingleByChannel(Hit, Start, End, ECC_Visibility, P))
		{
			const float Half = GetCapsuleComponent()->GetScaledCapsuleHalfHeight();
			SetActorLocation(FVector(L.X, L.Y, Hit.ImpactPoint.Z + Half + 2.0f),
				false, nullptr, ETeleportType::TeleportPhysics);
		}
	}
}

void AMCReplicaCharacter::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);

	UpdateWalkCycle(DeltaSeconds);

	if (UCharacterMovementComponent* Move =
			Cast<UCharacterMovementComponent>(GetCharacterMovement()))
	{
		Move->MaxWalkSpeed = bSprinting ? SprintSpeed : WalkSpeed;
	}
}

void AMCReplicaCharacter::UpdateWalkCycle(float DeltaSeconds)
{
	if (DeltaSeconds <= 0.0f)
	{
		return;
	}

	// Only the horizontal component drives the cycle: a pawn falling straight
	// down has full speed and would otherwise flail its limbs on every jump.
	const FVector V = GetVelocity();
	const float PlanarSpeed = FVector(V.X, V.Y, 0.0f).Size();

	const float TargetAlpha = FMath::Clamp(PlanarSpeed / FMath::Max(1.0f, WalkSpeed),
		0.0f, 1.0f);
	SwingAlpha = FMath::FInterpTo(SwingAlpha, TargetAlpha, DeltaSeconds,
		SwingInterpSpeed);

	if (SwingAlpha <= KINDA_SMALL_NUMBER)
	{
		// Settle back to the neutral pose rather than freezing mid-stride.
		SwingLimb(ArmLeft, 0.0f);
		SwingLimb(ArmRight, 0.0f);
		SwingLimb(LegLeft, 0.0f);
		SwingLimb(LegRight, 0.0f);
		WalkPhase = 0.0f;
		return;
	}

	// Stride frequency scales with speed so the feet do not skate: one full
	// cycle per stride length rather than per second.
	const float Frequency = 0.55f + 1.15f * (PlanarSpeed / FMath::Max(1.0f, SprintSpeed));
	WalkPhase += DeltaSeconds * Frequency * SwingAlpha;

	const float Angle = LimbSwingDegrees * SwingAlpha * FMath::Sin(WalkPhase * 2.0f * PI);
	const float LegAngle = Angle * 1.15f;

	// Arms swing opposite the legs, as they do when walking.
	SwingLimb(ArmLeft, -Angle);
	SwingLimb(ArmRight, Angle);
	SwingLimb(LegLeft, LegAngle);
	SwingLimb(LegRight, -LegAngle);
}

void AMCReplicaCharacter::SwingLimb(UStaticMeshComponent* Limb,
	float PitchDegrees) const
{
	if (!Limb)
	{
		return;
	}
	// Pitch about Y: rotates the limb forward and back about its pivot.
	Limb->SetRelativeRotation(FRotator(PitchDegrees, 0.0f, 0.0f));
}

void AMCReplicaCharacter::SetupPlayerInputComponent(
	UInputComponent* PlayerInputComponent)
{
	Super::SetupPlayerInputComponent(PlayerInputComponent);

	check(PlayerInputComponent);

	// Classic axis mappings rather than Enhanced Input: the project has no
	// InputAction assets and authoring them headlessly is not possible, while
	// the legacy path is still fully supported and does the same job.
	PlayerInputComponent->BindAxis(TEXT("MoveForward"), this,
		&AMCReplicaCharacter::MoveForward);
	PlayerInputComponent->BindAxis(TEXT("MoveRight"), this,
		&AMCReplicaCharacter::MoveRight);
	PlayerInputComponent->BindAxis(TEXT("Turn"), this,
		&AMCReplicaCharacter::Turn);
	PlayerInputComponent->BindAxis(TEXT("LookUp"), this,
		&AMCReplicaCharacter::LookUp);

	PlayerInputComponent->BindAction(TEXT("Jump"), IE_Pressed, this,
		&ACharacter::Jump);
	PlayerInputComponent->BindAction(TEXT("Jump"), IE_Released, this,
		&ACharacter::StopJumping);
	PlayerInputComponent->BindAction(TEXT("Sprint"), IE_Pressed, this,
		&AMCReplicaCharacter::BeginSprint);
	PlayerInputComponent->BindAction(TEXT("Sprint"), IE_Released, this,
		&AMCReplicaCharacter::EndSprint);
}

void AMCReplicaCharacter::MoveForward(float Value)
{
	if (!FMath::IsNearlyZero(Value))
	{
		// Movement is relative to where the camera is looking, so walking
		// always goes the way the view faces. The actor's own forward vector
		// is wrong here: the body turns to follow travel (see
		// bOrientRotationToMovement), so on the frame a turn starts the actor
		// still faces the old way and half the input would push backwards.
		AddMovementInput(FRotationMatrix(
			FRotator(0.0f, GetControlRotation().Yaw, 0.0f)).GetUnitAxis(EAxis::X),
			Value);
	}
}

void AMCReplicaCharacter::MoveRight(float Value)
{
	if (!FMath::IsNearlyZero(Value))
	{
		AddMovementInput(FRotationMatrix(
			FRotator(0.0f, GetControlRotation().Yaw, 0.0f)).GetUnitAxis(EAxis::Y),
			Value);
	}
}

void AMCReplicaCharacter::Turn(float Value)
{
	// Rotate the controller, and with it the body, so the figure always faces
	// the direction the camera orbits to.
	AddControllerYawInput(Value);
}

void AMCReplicaCharacter::LookUp(float Value)
{
	// Pitch is clamped by the engine so the camera cannot roll over the top.
	AddControllerPitchInput(Value);
}

void AMCReplicaCharacter::BeginSprint()
{
	bSprinting = true;
}

void AMCReplicaCharacter::EndSprint()
{
	bSprinting = false;
}
