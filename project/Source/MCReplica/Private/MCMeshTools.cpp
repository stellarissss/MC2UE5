// Build-time mesh utilities. Not shipped in gameplay code paths.

#include "MCMeshTools.h"

#if WITH_EDITOR
#include "Engine/StaticMesh.h"
#include "MeshDescription.h"
#include "StaticMeshAttributes.h"
#endif

namespace
{
	// Shared by both entry points so a script can size its arrays and get the
	// same answer the writer will see.
	int32 CountVertexInstances(const UStaticMesh* Mesh)
	{
#if WITH_EDITOR
		if (!Mesh)
		{
			return -1;
		}
		const FMeshDescription* MeshDescription = Mesh->GetMeshDescription(0);
		if (!MeshDescription)
		{
			return -2;
		}
		return MeshDescription->VertexInstances().Num();
#else
		// Normals are baked into the asset at build time; the packaged game has
		// no use for this and must not carry the editor-only dependency.
		(void)Mesh;
		return -3;
#endif
	}
}

int32 UMCMeshTools::GetVertexInstanceCount(UStaticMesh* Mesh)
{
	return CountVertexInstances(Mesh);
}

int32 UMCMeshTools::CountUsableNormals(UStaticMesh* Mesh)
{
#if WITH_EDITOR
	if (!Mesh)
	{
		return -1;
	}
	const FMeshDescription* MeshDescription = Mesh->GetMeshDescription(0);
	if (!MeshDescription)
	{
		return -2;
	}

	const FStaticMeshAttributes Attributes(
		*const_cast<FMeshDescription*>(MeshDescription));
	const TVertexInstanceAttributesConstRef<FVector3f> Normals =
		Attributes.GetVertexInstanceNormals();

	const FVertexInstanceArray& Instances = MeshDescription->VertexInstances();
	const int32 NumInstances = Instances.Num();
	int32 Usable = 0;
	for (int32 ElementIndex = 0; ElementIndex < NumInstances; ++ElementIndex)
	{
		if (!Instances.IsValid(ElementIndex))
		{
			continue;
		}
		const FVector3f Normal(Normals[FVertexInstanceID(ElementIndex)]);
		// A zero normal is the failure this whole mechanism exists to prevent,
		// so it is counted separately from a merely non-unit one.
		if (FVector(Normal).SizeSquared() > 0.5f)
		{
			++Usable;
		}
	}
	return Usable;
#else
	(void)Mesh;
	return -3;
#endif
}

int32 UMCMeshTools::WriteVertexInstanceNormals(UStaticMesh* Mesh,
	const TArray<FVector>& Normals,
	const TArray<FVector>& Tangents,
	float BinormalSign)
{
#if WITH_EDITOR
	if (!Mesh)
	{
		return -1;
	}

	FMeshDescription* MeshDescription = Mesh->GetMeshDescription(0);
	if (!MeshDescription)
	{
		return -2;
	}

	const int32 NumInstances = MeshDescription->VertexInstances().Num();
	if (NumInstances == 0)
	{
		return -4;
	}
	// Checked, not trusted: a short array would silently leave the tail of the
	// mesh with zero normals, which is the exact failure being fixed here.
	if (Normals.Num() != NumInstances)
	{
		return -5;
	}

	FStaticMeshAttributes Attributes(*MeshDescription);
	Attributes.Register();

	TVertexInstanceAttributesRef<FVector3f> NormalAttribute =
		Attributes.GetVertexInstanceNormals();
	TVertexInstanceAttributesRef<FVector3f> TangentAttribute =
		Attributes.GetVertexInstanceTangents();
	TVertexInstanceAttributesRef<float> BinormalSignAttribute =
		Attributes.GetVertexInstanceBinormalSigns();

	// Element IDs are walked by index rather than with a range-for:
	// FVertexInstanceArray exposes no begin()/end(). The container has
	// IsValid() because IDs can in general have holes, so a hole is reported
	// instead of silently skipped -- silently dropping an element here would
	// leave the caller misaligned against its own array and shade part of the
	// mesh with the wrong normal.
	const FVertexInstanceArray& Instances = MeshDescription->VertexInstances();
	int32 Index = 0;
	for (int32 ElementIndex = 0; ElementIndex < NumInstances; ++ElementIndex)
	{
		if (!Instances.IsValid(ElementIndex))
		{
			return -6;
		}
		const FVertexInstanceID VertexInstanceID(ElementIndex);

		// GetSafeNormal takes no fallback in this engine, so the degenerate case
		// is handled explicitly: a zero normal would rebuild the same black
		// surface this function exists to fix.
		FVector Normal = Normals[Index];
		if (!Normal.Normalize())
		{
			Normal = FVector::UpVector;
		}

		FVector Tangent = FVector::ForwardVector;
		if (Tangents.IsValidIndex(Index))
		{
			Tangent = Tangents[Index];
		}
		// Gram-Schmidt against the normal, so the basis cannot be degenerate.
		Tangent -= Normal * FVector::DotProduct(Tangent, Normal);
		if (!Tangent.Normalize())
		{
			Tangent = FVector::ForwardVector;
		}

		NormalAttribute[VertexInstanceID] = FVector3f(Normal);
		TangentAttribute[VertexInstanceID] = FVector3f(Tangent);
		BinormalSignAttribute[VertexInstanceID] = BinormalSign;
		++Index;
	}

	UStaticMesh::FCommitMeshDescriptionParams Params;
	// bMarkPackageDirty defaults to true, which is wanted: the asset must be
	// saved for the change to reach disk and the cook.
	Mesh->CommitMeshDescription(0, Params);
	Mesh->PostEditChange();
	Mesh->MarkPackageDirty();

	return Index;
#else
	(void)Mesh;
	(void)Normals;
	(void)Tangents;
	(void)BinormalSign;
	return -3;
#endif
}
