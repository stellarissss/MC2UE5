// Character for the rebuilt campus: walks the terrain, climbs steps, and is
// dressed as a Minecraft-style blocky school student.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Character.h"
#include "MCReplicaCharacter.generated.h"

class UCameraComponent;
class USpringArmComponent;
class UStaticMeshComponent;

/**
 * A walkable Minecraft-proportioned student.
 *
 * Built from static meshes rather than a skeletal mesh on purpose. The source
 * data is voxel, so the character is a blocky figure -- exactly what a rig of
 * boxes reproduces -- and assembling it from separate components lets each
 * limb pivot at its own joint so the walk cycle can be driven procedurally in
 * Tick. That avoids needing an AnimBlueprint, which cannot be authored from a
 * headless editor session.
 *
 * All dimensions follow the Minecraft player, where one block is 100 cm:
 *   total height 200 cm, head 50 cm, torso 75 cm, legs 75 cm, arms 75 cm,
 *   limb cross-section 25 cm, shoulder span 75 cm.
 */
UCLASS()
class MCREPLICA_API AMCReplicaCharacter : public ACharacter
{
	GENERATED_BODY()

public:
	AMCReplicaCharacter();

	/** Camera boom length in cm. Long enough to see the figure and the campus. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Camera")
	float CameraBoomLength = 420.0f;

	/** Metres per second at full walk speed (Minecraft walks at ~4.3). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
	float WalkSpeed = 450.0f;

	/** Metres per second while sprinting. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
	float SprintSpeed = 760.0f;

	/** Peak height of a jump in cm (Minecraft clears 1.25 blocks). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
	float JumpHeight = 125.0f;

	/**
	 * Largest ledge the character walks up without jumping, in cm.
	 *
	 * The terrain here is a 1 m grid, so anything under a block is free and a
	 * full block should still be climbable. The engine default of 45 is less
	 * than half a block and leaves the character snagging on the lip of every
	 * one-block step in the campus.
	 */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
	float StepHeight = 60.0f;

	/** Degrees of limb swing at full walking speed. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Animation")
	float LimbSwingDegrees = 42.0f;

	/** How quickly the walk cycle blends in and out, in 1/s. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Animation")
	float SwingInterpSpeed = 9.0f;

protected:
	virtual void BeginPlay() override;
	virtual void Tick(float DeltaSeconds) override;
	virtual void SetupPlayerInputComponent(UInputComponent* PlayerInputComponent) override;

	/** Rebuilds the walk cycle from the current planar speed. */
	void UpdateWalkCycle(float DeltaSeconds);

	/** Rotates a limb about its pivot, in local space. */
	void SwingLimb(UStaticMeshComponent* Limb, float PitchDegrees) const;

	// Input handlers. Axis functions take the raw key value and scale it
	// themselves, because the controller yaw is what movement is relative to.
	void MoveForward(float Value);
	void MoveRight(float Value);
	void Turn(float Value);
	void LookUp(float Value);
	void BeginSprint();
	void EndSprint();

private:
	/** Root for the figure itself, so the whole body can be offset as one. */
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	USceneComponent* BodyRoot;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UStaticMeshComponent* Head;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UStaticMeshComponent* Torso;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UStaticMeshComponent* ArmLeft;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UStaticMeshComponent* ArmRight;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UStaticMeshComponent* LegLeft;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UStaticMeshComponent* LegRight;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	USpringArmComponent* CameraBoom;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica",
		meta = (AllowPrivateAccess = "true"))
	UCameraComponent* Camera;

	/** Smoothed 0..1 phase of the walk cycle. */
	float WalkPhase = 0.0f;

	/** Current blend weight of the walk cycle, 0..1. */
	float SwingAlpha = 0.0f;

	bool bSprinting = false;
};
