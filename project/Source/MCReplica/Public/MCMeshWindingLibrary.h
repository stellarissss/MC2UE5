// Reads triangle winding back out of an imported StaticMesh.
//
// Exists because the black-screen bug it guards against passed every
// Python-side check: the level reported four meshes placed, collision was
// present, the cook succeeded, and the packaged game launched and ran. What was
// wrong was that every terrain triangle was wound backwards, so the engine
// culled the entire ground and the player saw sky through it.
//
// Verifying in C++ is the reliable way to read mesh geometry out of UE: the
// Python API exposes no accessor for LOD vertex positions on this engine
// version, and guessing at attribute names from Python costs an editor launch
// per attempt.

#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"

#include "MCMeshWindingLibrary.generated.h"

/** Triangle counts read back out of one mesh's LOD0. */
USTRUCT(BlueprintType)
struct FMCMeshWindingStats
{
	GENERATED_BODY()

	/** Triangles whose geometric normal points along +Z. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 FrontFacing = 0;

	/** Triangles whose geometric normal points along -Z: culled from above. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 BackFacing = 0;

	/** Triangles with a zero-area normal, i.e. no usable facing at all. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 Degenerate = 0;

	/** How many triangles were examined. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 Sampled = 0;

	/** True when the mesh was readable and something was measured. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	bool bValid = false;

	/** Why the read failed, when bValid is false. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	FString Note;
};

/**
 * Reads a mesh's LOD0 triangle winding.
 *
 * Used as a build gate: terrain must report zero back-facing triangles, and a
 * closed primitive (the prop boxes) must report some, because half of a box's
 * faces legitimately point away from +Z.
 */
UCLASS()
class MCREPLICA_API UMCMeshWindingLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Classify the triangles of `Mesh`'s LOD0 by the sign of their geometric
	 * normal's Z component.
	 *
	 * `MaxSamples` bounds the work on the terrain, which has 392k triangles per
	 * tile -- enough to be certain from a few thousand, and a full pass over
	 * four tiles on every build is minutes of stall for no extra confidence.
	 */
	UFUNCTION(BlueprintCallable, Category = "MCReplica",
		meta = (WorldContext = "WorldContextObject"))
	static FMCMeshWindingStats ReadWinding(UStaticMesh* Mesh,
	                                       int32 MaxSamples = 4000);
};
