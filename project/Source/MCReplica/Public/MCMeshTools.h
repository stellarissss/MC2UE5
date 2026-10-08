// Build-time mesh utilities. Not shipped in gameplay code paths.

#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MCMeshTools.generated.h"

class UStaticMesh;

/**
 * Utilities for meshes that were authored by script rather than by an importer.
 */
UCLASS()
class MCREPLICA_API UMCMeshTools : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Write per-vertex-instance normals (and tangents) into a static mesh's LOD 0
	 * mesh description, then commit and rebuild it.
	 *
	 * Why this exists. A StaticMesh built from a UStaticMeshDescription gets
	 * whatever normals the description carries -- and UStaticMesh::
	 * BuildFromMeshDescription copies them straight into the render data
	 * (StaticMesh.cpp: `StaticMeshVertex.TangentZ = VertexInstanceNormals[...]`).
	 * It does not compute them. A description whose normals were never set
	 * therefore shades every surface with a zero normal, which is why the
	 * terrain came out flat black no matter how strong the sun was: dot(N,L) = 0
	 * regardless of intensity.
	 *
	 * The OBJ importer does compute them, which is why imported meshes light up
	 * and script-built ones do not. UE 5.8's Python API cannot set them --
	 * UStaticMeshDescription exposes only seven set_* members and none of them
	 * touch normals -- hence this function, which Python can call.
	 *
	 * The caller supplies the arrays, in the same order as the mesh description's
	 * vertex instances (the order they were created in). The count is checked
	 * rather than trusted.
	 *
	 * @param Mesh        mesh to modify in place
	 * @param Normals     per-vertex-instance normals, not required to be unit
	 * @param Tangents    per-vertex-instance tangents; orthogonalised against the
	 *                    normal, falling back to +X when degenerate
	 * @param BinormalSign handedness for the tangent basis
	 * @return how many vertex instances were written, or a negative error code
	 *         (-1 no mesh, -2 no mesh description, -3 editor-only, -4 empty
	 *         description, -5 array count mismatch)
	 */
	UFUNCTION(BlueprintCallable, Category = "MC2UE5|Build")
	static int32 WriteVertexInstanceNormals(UStaticMesh* Mesh,
		const TArray<FVector>& Normals,
		const TArray<FVector>& Tangents,
		float BinormalSign);

	/**
	 * Number of vertex instances in LOD 0's mesh description, or -1 if there is
	 * no description. Lets a script size its arrays before writing them, so a
	 * mismatch is caught before anything is modified.
	 */
	UFUNCTION(BlueprintCallable, Category = "MC2UE5|Build")
	static int32 GetVertexInstanceCount(UStaticMesh* Mesh);

	/**
	 * Read the LOD 0 mesh description back and report how many vertex instances
	 * carry a usable normal.
	 *
	 * This exists because "the writer returned the right count" is not evidence
	 * that the normals survived CommitMeshDescription and the subsequent
	 * rebuild -- and a mesh with zero normals renders black, which looks
	 * identical to a lighting problem. Counting unit-length normals
	 * distinguishes the two without needing a render.
	 *
	 * @return how many vertex instances have a normal of length ~1, or a
	 *         negative error code on the same terms as the other functions
	 */
	UFUNCTION(BlueprintCallable, Category = "MC2UE5|Build")
	static int32 CountUsableNormals(UStaticMesh* Mesh);
};
