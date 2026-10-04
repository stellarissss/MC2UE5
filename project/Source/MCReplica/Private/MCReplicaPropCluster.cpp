// Instanced placement of the campus props.

#include "MCReplicaPropCluster.h"

#include "Components/HierarchicalInstancedStaticMeshComponent.h"
#include "Engine/StaticMesh.h"

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

int32 AMCReplicaPropCluster::GetInstanceCount() const
{
	return Instances ? Instances->GetInstanceCount() : 0;
}
