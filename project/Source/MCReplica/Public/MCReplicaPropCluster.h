// Instanced placement of the campus props.
//
// The classified prop placements are 1297 points. Placed as individual actors
// they cost roughly one draw call each, which was the largest avoidable cost in
// the build. A Hierarchical Instanced Static Mesh component draws them all in
// one call and clusters them for culling.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "MCReplicaPropCluster.generated.h"

class UHierarchicalInstancedStaticMeshComponent;
class UStaticMesh;

/**
 * A cluster of props sharing one mesh, drawn as a single instanced batch.
 *
 * One actor per (mesh, spatial cell) rather than one per prop: the cell
 * grouping keeps each component's bounds small enough that cluster culling can
 * actually reject it, which a single actor spanning the whole campus could not
 * do.
 */
UCLASS()
class MCREPLICA_API AMCReplicaPropCluster : public AActor
{
	GENERATED_BODY()

public:
	AMCReplicaPropCluster();

	/**
	 * Replaces the cluster's contents from parallel position/rotation/scale
	 * arrays, in world space.
	 *
	 * Takes three arrays rather than an array of FTransform on purpose. The
	 * Python bindings for FTransform have no non-ambiguous constructor in 5.8
	 * -- every positional overload is also a valid Vector or Rotator, so the
	 * call fails as a struct nativize error naming the wrong member -- and
	 * FTransform.rotation is a Quat that Python cannot build from a Rotator.
	 * Plain float arrays sidestep the marshalling entirely and let the engine
	 * do the Rotator-to-Quat conversion, which is exact.
	 *
	 * Instances are added in one batch: per-instance AddInstance calls each
	 * dirty the render state, and the batch form is roughly an order of
	 * magnitude faster at this count.
	 */
	UFUNCTION(BlueprintCallable, Category = "MCReplica")
	void SetInstancesFromData(const TArray<FVector>& Positions,
	                          const TArray<FRotator>& Rotations,
	                          const TArray<FVector>& Scales);

	/** Sets the mesh drawn for every instance in this cluster. */
	UFUNCTION(BlueprintCallable, Category = "MCReplica")
	void SetMesh(UStaticMesh* InMesh);

	/** Number of instances currently held. */
	UFUNCTION(BlueprintCallable, Category = "MCReplica")
	int32 GetInstanceCount() const;

	/**
	 * Distance band over which instances fade out, in cm.
	 *
	 * A prop is invisible once it is too small to resolve, which is a distance
	 * proportional to its size -- one number cannot serve both a 1 m bush and a
	 * 30 m building -- so this is set per cluster from the semantic class.
	 */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "MCReplica")
	float StartCullDistance = 12000.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "MCReplica")
	float EndCullDistance = 16000.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "MCReplica")
	TObjectPtr<UHierarchicalInstancedStaticMeshComponent> Instances;

private:
	/** Cached so SetMesh can tell a real change from a repeated assignment. */
	UPROPERTY()
	TObjectPtr<UStaticMesh> CurrentMesh;
};
