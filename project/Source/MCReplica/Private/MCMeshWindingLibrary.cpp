// Reads triangle winding back out of an imported StaticMesh.

#include "MCMeshWindingLibrary.h"

#include "Engine/StaticMesh.h"
#include "RawIndexBuffer.h"
#include "Rendering/PositionVertexBuffer.h"
#include "StaticMeshResources.h"

FMCMeshWindingStats UMCMeshWindingLibrary::ReadWinding(UStaticMesh* Mesh,
	int32 MaxSamples)
{
	FMCMeshWindingStats Stats;

	if (!Mesh)
	{
		Stats.Note = TEXT("null mesh");
		return Stats;
	}

	const FStaticMeshRenderData* RenderData = Mesh->GetRenderData();
	if (!RenderData || RenderData->LODResources.Num() == 0)
	{
		Stats.Note = FString::Printf(TEXT("no render data (%s)"),
			*Mesh->GetName());
		return Stats;
	}

	const FStaticMeshLODResources& LOD = RenderData->LODResources[0];
	const FPositionVertexBuffer& Positions = LOD.VertexBuffers.PositionVertexBuffer;
	const FRawStaticIndexBuffer& Indices = LOD.IndexBuffer;

	// GetArrayView() hands back the index array in whichever width the mesh
	// was built with; the tri count is what matters, not the element type.
	const int32 NumIndices = static_cast<int32>(Indices.GetArrayView().Num());
	const uint32 NumVerts = Positions.GetNumVertices();

	if (NumVerts == 0 || NumIndices < 3)
	{
		Stats.Note = FString::Printf(
			TEXT("empty LOD0 (%s): %u verts, %d indices"),
			*Mesh->GetName(), NumVerts, NumIndices);
		return Stats;
	}

	const int32 NumTris = NumIndices / 3;
	const int32 Step = FMath::Max(1, NumTris / FMath::Max(1, MaxSamples));

	Stats.bValid = true;
	Stats.Note = FString::Printf(TEXT("%d tris, stride %d, %u verts"),
		NumTris, Step, NumVerts);

	const FIndexArrayView& View = Indices.GetArrayView();

	for (int32 T = 0; T < NumTris; T += Step)
	{
		const uint32 I0 = View[T * 3 + 0];
		const uint32 I1 = View[T * 3 + 1];
		const uint32 I2 = View[T * 3 + 2];

		if (I0 >= NumVerts || I1 >= NumVerts || I2 >= NumVerts)
		{
			continue;
		}

		// VertexPosition(uint32) returns one element by reference; it is not a
		// pointer to the whole array.
		const FVector3f A = Positions.VertexPosition(I0);
		const FVector3f B = Positions.VertexPosition(I1);
		const FVector3f C = Positions.VertexPosition(I2);

		const FVector3f U = B - A;
		const FVector3f V = C - A;

		// Z component of U x V. The full cross product is
		//   (U.Y*V.Z - U.Z*V.Y,  U.Z*V.X - U.X*V.Z,  U.X*V.Y - U.Y*V.X)
		// and it is the third term that says whether a heightfield triangle
		// faces the sky. Reading the first term instead measures slope along X,
		// which on a terrain averages out to about half up and half down and
		// makes a perfectly good mesh look broken.
		const float Nz = (U.X * V.Y - U.Y * V.X);

		++Stats.Sampled;
		if (FMath::Abs(Nz) < KINDA_SMALL_NUMBER)
		{
			++Stats.Degenerate;
		}
		else if (Nz > 0.0f)
		{
			++Stats.FrontFacing;
		}
		else
		{
			++Stats.BackFacing;
		}
	}

	return Stats;
}
