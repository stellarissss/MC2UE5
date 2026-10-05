// Reads the rendered frame back out of the GPU.

#include "MCFrameCapture.h"

#include "Engine/Engine.h"
#include "Engine/GameViewportClient.h"
#include "Engine/World.h"
#include "HAL/FileManager.h"
#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "UnrealClient.h"

namespace
{
	/**
	 * Encode as PNG through ImageWrapper, from raw BGRA.
	 *
	 * Written this way rather than via FImageUtils::CompressImageArray because
	 * that writes an uncompressed TGA from a different memory layout than the
	 * one ReadPixels returns, and getting the two confused produces a file that
	 * opens but is upside down. Here the rows are flipped once, explicitly,
	 * and the encoder is handed the result.
	 */
	bool WritePng(const FString& Path, const TArray<FColor>& Pixels,
	              int32 Width, int32 Height)
	{
		TArray64<uint8> Raw;
		Raw.Reserve(static_cast<int64>(Width) * Height * 4);
		for (int32 y = Height - 1; y >= 0; --y)
		{
			const FColor* Row = Pixels.GetData() + y * Width;
			for (int32 x = 0; x < Width; ++x)
			{
				Raw.Add(Row[x].R);
				Raw.Add(Row[x].G);
				Raw.Add(Row[x].B);
				Raw.Add(Row[x].A);
			}
		}

		IImageWrapperModule& Module =
			FModuleManager::LoadModuleChecked<IImageWrapperModule>(
				FName(TEXT("ImageWrapper")));
		const TSharedPtr<IImageWrapper> Wrapper = Module.CreateImageWrapper(
			EImageFormat::PNG);
		if (!Wrapper.IsValid())
		{
			return false;
		}
		if (!Wrapper->SetRaw(Raw.GetData(), Raw.Num(), Width, Height,
		                    ERGBFormat::BGRA, 8))
		{
			return false;
		}
		const TArray64<uint8>& Out = Wrapper->GetCompressed(100);
		return FFileHelper::SaveArrayToFile(Out, *Path);
	}
}

FMCFrameStats UMCFrameCapture::CaptureViewport(UObject* WorldContextObject,
	const FString& OutPath)
{
	FMCFrameStats Stats;

	if (!GEngine || !GEngine->GameViewport)
	{
		Stats.Note = TEXT("no game viewport -- run with -game, or render into a "
		                  "target from a tick");
		return Stats;
	}

	FViewport* Viewport = GEngine->GameViewport->Viewport;
	if (!Viewport)
	{
		Stats.Note = TEXT("viewport is null");
		return Stats;
	}

	FIntPoint Size = Viewport->GetSizeXY();
	if (Size.X <= 0 || Size.Y <= 0)
	{
		Stats.Note = FString::Printf(TEXT("viewport has no area (%dx%d)"),
			Size.X, Size.Y);
		return Stats;
	}

	Stats.Width = Size.X;
	Stats.Height = Size.Y;

	TArray<FColor> Pixels;
	if (!Viewport->ReadPixels(Pixels) || Pixels.Num() == 0)
	{
		Stats.Note = TEXT("ReadPixels returned nothing");
		return Stats;
	}

	// FViewport::ReadPixels already flips and pads; trim back to the real area.
	const int32 Count = Size.X * Size.Y;
	if (Pixels.Num() > Count)
	{
		Pixels.SetNum(Count);
	}

	Stats.bValid = true;

	// ---- census ---------------------------------------------------------
	int64 SumR = 0, SumG = 0, SumB = 0;
	int32 Dark = 0, Sky = 0, Lit = 0, Samples = 0;
	TSet<uint32> Colours;

	const int32 Step = FMath::Max(1, FMath::Min(Size.X, Size.Y) / 96);
	for (int32 y = 0; y < Size.Y; y += Step)
	{
		for (int32 x = 0; x < Size.X; x += Step)
		{
			const FColor C = Pixels[y * Size.X + x];
			const int32 R = C.R, G = C.G, B = C.B;
			SumR += R; SumG += G; SumB += B;
			++Samples;

			const float Lum = 0.299f * R + 0.587f * G + 0.114f * B;
			if (Lum < 24.0f)
			{
				++Dark;
			}
			else if (B > R + 25 && B > 90)
			{
				++Sky;
			}
			else if (Lum > 45.0f)
			{
				++Lit;
			}
			Colours.Add(((R / 24) << 16) | ((G / 24) << 8) | (B / 24));
		}
	}

	if (Samples > 0)
	{
		Stats.MeanRGB = FIntPoint((int32)(SumR / Samples),
		                          (int32)(SumG / Samples));
		Stats.DarkPercent = 100.0f * Dark / Samples;
		Stats.SkyPercent = 100.0f * Sky / Samples;
		Stats.LitPercent = 100.0f * Lit / Samples;
	}
	Stats.DistinctColours = Colours.Num();
	(void)SumB;

	// ---- write the file -------------------------------------------------
	FString Path = OutPath;
	if (Path.IsEmpty())
	{
		Path = FPaths::ProjectSavedDir() / TEXT("MCFrame") / TEXT("frame.png");
	}
	if (!FPaths::GetExtension(Path).IsEmpty())
	{
		Path = FPaths::GetBaseFilename(Path);
	}

	IFileManager::Get().MakeDirectory(*FPaths::GetPath(Path), true);

	if (WritePng(Path, Pixels, Size.X, Size.Y))
	{
		Stats.Note = FString::Printf(TEXT("wrote %s (%dx%d)"), *Path,
			Size.X, Size.Y);
	}
	else
	{
		Stats.bValid = false;
		Stats.Note = TEXT("pixels were read but the PNG could not be written");
	}
	return Stats;
}
