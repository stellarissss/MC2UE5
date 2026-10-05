// Instanced placement of the campus props.

#include "MCReplicaPropCluster.h"

#include "Components/HierarchicalInstancedStaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "Materials/MaterialInterface.h"

AMCReplicaPropCluster::AMCReplicaPropCluster()
{
	PrimaryActorTick.bCanEverTick = false;

	Instances = CreateDefaultSubobject<UHierarchicalInstancedStaticMeshComponent>(
		TEXT("Instances"));
	RootComponent = Instances;

	// The terrain is what the player walks on; props are scenery. Leaving them
	// out of the collision set keeps the character sweep cheap and stops a
	// dense hedge from becoming a wall the player cannot push through.
	Instances->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	Instances->SetGenerateOverlapEvents(false);
	Instances->SetCastShadow(true);

	Instances->InstanceStartCullDistance = StartCullDistance;
	Instances->InstanceEndCullDistance = EndCullDistance;

	// Cluster culling needs the per-cluster bounds the HISM tree builds; with
	// GPU selection off, the CPU walks the tree and rejects whole clusters.
	Instances->bUseGpuLodSelection = false;
}

void AMCReplicaPropCluster::SetMesh(UStaticMesh* InMesh)
{
	if (!InMesh || InMesh == CurrentMesh)
	{
		return;
	}
	CurrentMesh = InMesh;
	Instances->SetStaticMesh(InMesh);
}

void AMCReplicaPropCluster::SetInstancesFromData(const TArray<FVector>& Positions,
	const TArray<FRotator>& Rotations, const TArray<FVector>& Scales)
{
	Instances->ClearInstances();

	const int32 Count = FMath::Min3(Positions.Num(), Rotations.Num(),
		Scales.Num());
	if (Count == 0)
	{
		return;
	}

	TArray<FTransform> Transforms;
	Transforms.Reserve(Count);
	for (int32 i = 0; i < Count; ++i)
	{
		Transforms.Emplace(Rotations[i], Positions[i], Scales[i]);
	}

	// Reserve before filling: without this the component reallocates its
	// instance array as it grows, which is the expensive part.
	Instances->PreAllocateInstancesMemory(Count);

	// One batch, world space. The actor sits at the cell origin and the
	// transforms carry absolute positions, so the culling bounds follow the
	// actor transform without the transforms having to be rebased.
	Instances->AddInstances(Transforms, /*bShouldReturnIndices*/ false,
		/*bWorldSpace*/ true);

	// Cull distances are properties of the component, so they only take effect
	// once they are re-applied after the instances exist.
	Instances->InstanceStartCullDistance = StartCullDistance;
	Instances->InstanceEndCullDistance = EndCullDistance;
}

int32 AMCReplicaPropCluster::AddBlockInstances(const TArray<uint8>& Packed,
	float ZOffsetCm)
{
	constexpr int32 Stride = 6;                 // three little-endian uint16
	const int32 Count = Packed.Num() / Stride;
	if (Count <= 0 || !Instances)
	{
		return 0;
	}

	const uint8* Base = Packed.GetData();
	TArray<FTransform> Transforms;
	Transforms.Reserve(Count);

	for (int32 i = 0; i < Count; ++i)
	{
		uint16 Lx = 0, Lz = 0, Y = 0;
		FMemory::Memcpy(&Lx, Base + i * Stride + 0, sizeof(uint16));
		FMemory::Memcpy(&Lz, Base + i * Stride + 2, sizeof(uint16));
		FMemory::Memcpy(&Y, Base + i * Stride + 4, sizeof(uint16));

		// Unreal is (X, Y = depth, Z = up); the record is Minecraft's own
		// (x, z, height) order, so the vertical term goes to Z and the depth
		// term to Y. Swapping them is the exact mistake that once buried every
		// prop 65 m underground, and it is invisible until something renders.
		//
		// +50 cm centres the 100 cm cube on its block, matching how the
		// heightmap terrain places its vertices.
		const FVector Position(
			Lx * 100.0f + 50.0f,
			Lz * 100.0f + 50.0f,
			Y * 100.0f + 50.0f + ZOffsetCm);

		Transforms.Emplace(FRotator::ZeroRotator, Position, FVector::OneVector);
	}

	// Reserve first: without it the component reallocates its instance array as
	// it grows, which dominates the cost at this count.
	Instances->PreAllocateInstancesMemory(
		Instances->GetInstanceCount() + Count);

	// Actor-relative (bWorldSpace=false): the actor sits at the cell origin and
	// the offsets are already relative to it.
	Instances->AddInstances(Transforms, /*bShouldReturnIndices*/ false,
		/*bWorldSpace*/ false);

	ApplyCullDistances();
	return Count;
}

void AMCReplicaPropCluster::ApplyCullDistances()
{
	if (!Instances)
	{
		return;
	}
	// These are component properties, so they only take effect once the
	// instances exist.
	Instances->InstanceStartCullDistance = StartCullDistance;
	Instances->InstanceEndCullDistance = EndCullDistance;
}

void AMCReplicaPropCluster::ConfigureBlockLayer(UStaticMesh* InMesh,
	UMaterialInterface* InMaterial, bool bEnableCollision, float InStartCull,
	float InEndCull)
{
	StartCullDistance = InStartCull;
	EndCullDistance = InEndCull;

	SetMesh(InMesh);
	if (!Instances)
	{
		return;
	}

	if (InMaterial)
	{
		Instances->SetMaterial(0, InMaterial);
	}

	// The block layer is the world: the ground the player stands on and the
	// walls that stop them. The constructor's NoCollision is right for props
	// and wrong here.
	if (bEnableCollision)
	{
		Instances->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
		Instances->SetCollisionProfileName(TEXT("BlockAll"));
		Instances->SetGenerateOverlapEvents(false);
	}

	ApplyCullDistances();
}

int32 AMCReplicaPropCluster::GetInstanceCount() const
{
	return Instances ? Instances->GetInstanceCount() : 0;
}
