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

	/**
	 * Bulk-fills the cluster with unit-block instances from packed positions.
	 *
	 * ``Packed`` is a flat run of little-endian uint16 triples ``(lx, lz, y)``
	 * giving each block's offset in blocks from the actor origin, with ``y`` the
	 * vertical axis -- Minecraft's own component order. The count is
	 * ``Packed.Num() / 6``.
	 *
	 * Two reasons this lives in C++ rather than in the assembler script:
	 *
	 *  * **The Python API cannot build one of these at all.** In 5.8's bindings
	 *    ``AActor`` exposes neither ``AddInstanceComponent`` nor
	 *    ``SetRootComponent``, and ``UActorComponent`` exposes no
	 *    ``RegisterComponent`` -- a component made with ``new_object`` cannot be
	 *    attached to an actor or registered, so it draws nothing and is not
	 *    saved. A Python-side HISM is therefore impossible, not merely awkward.
	 *  * **1.17 M transforms.** Building them here instead of as Python objects
	 *    avoids the marshalling and the per-object overhead entirely.
	 *
	 * Positions are added in **actor-relative** space, so the component's
	 * culling bounds follow the actor and the instances never need rebasing.
	 *
	 * ``ZOffsetCm`` shifts every instance vertically, which is how a dimension
	 * is placed without moving the actor.
	 *
	 * Returns the number of instances added.
	 */
	UFUNCTION(BlueprintCallable, Category = "MCReplica")
	int32 AddBlockInstances(const TArray<uint8>& Packed, float ZOffsetCm);

	/** Re-applies the cull band; call after the instances exist. */
	UFUNCTION(BlueprintCallable, Category = "MCReplica")
	void ApplyCullDistances();

	/**
	 * One-shot configuration for a terrain block layer.
	 *
	 * Exists as a single call rather than a handful of Python property writes
	 * because collision, material and cull distances all have to be applied to
	 * the *component*, and the component is created by this actor's constructor
	 * -- Python can read ``Instances`` but going through it for each of the
	 * ~1000 block clusters is both slower and more fragile than one typed call.
	 *
	 * ``bEnableCollision`` matters for blocks specifically: the constructor
	 * leaves props non-colliding (scenery the player walks through), but the
	 * block layer *is* the ground and the buildings, so it must collide.
	 */
	UFUNCTION(BlueprintCallable, Category = "MCReplica")
	void ConfigureBlockLayer(UStaticMesh* InMesh, UMaterialInterface* InMaterial,
	                         bool bEnableCollision, float InStartCull,
	                         float InEndCull);

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
