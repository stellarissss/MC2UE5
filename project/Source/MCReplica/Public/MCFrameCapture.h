// Reads the rendered frame back out of the GPU.
//
// Exists because the machine runs no interactive desktop. The RHI comes up
// fine -- D3D12, SM5, a real GPU -- but there is no window, so HighResShot
// writes nothing, a screen grab returns the desktop, and PrintWindow has
// nothing to target. Every previous diagnosis of the black screen was made
// without looking at a single pixel, and two of them were wrong.
//
// RHIGetViewportData is the one path that does not need a window: it asks the
// renderer to read the framebuffer back through the RHI itself. This is how
// offscreen capture works, and it is why the same call works in a session with
// no desktop at all.

#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MCFrameCapture.generated.h"

/** What a captured frame contains. */
USTRUCT(BlueprintType)
struct FMCFrameStats
{
	GENERATED_BODY()

	/** Frame was read back successfully. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	bool bValid = false;

	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 Width = 0;

	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 Height = 0;

	/** Mean red / green / blue across the sampled grid. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	FIntPoint MeanRGB = FIntPoint::ZeroValue;

	/** Percent of samples that are near-black. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	float DarkPercent = 0.0f;

	/** Percent of samples that are sky-blue. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	float SkyPercent = 0.0f;

	/** Percent of samples that are a plausibly lit surface. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	float LitPercent = 0.0f;

	/** Distinct colours after coarse quantisation: a black frame has one. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	int32 DistinctColours = 0;

	/** Why the read failed, when bValid is false. */
	UPROPERTY(BlueprintReadOnly, Category = "MCReplica")
	FString Note;
};

/**
 * Captures the current viewport and writes a PNG, then reports what is in it.
 *
 * The statistics are the point. "Is the terrain black?" has been answered by
 * inference three times and wrong twice; here it is a percentage.
 */
UCLASS()
class MCREPLICA_API UMCFrameCapture : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Grab the viewport and write it next to the log.
	 *
	 * @param WorldContextObject  any object in the world being captured
	 * @param OutPath             PNG path; empty writes to Saved/MCFrame.png
	 */
	UFUNCTION(BlueprintCallable, Category = "MCReplica",
		meta = (WorldContext = "WorldContextObject"))
	static FMCFrameStats CaptureViewport(UObject* WorldContextObject,
	                                     const FString& OutPath);
};
