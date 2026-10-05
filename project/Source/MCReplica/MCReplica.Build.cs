// Build rules for the MCReplica project module.

using UnrealBuildTool;

public class MCReplica : ModuleRules
{
	public MCReplica(ReadOnlyTargetRules Target) : base(Target)
	{
		// ACharacter and the character movement component live in the Engine
		// module; the spring arm and camera come from Engine as well. Nothing
		// here needs editor-only code, so the module is runtime-only and the
		// packaged build does not carry an editor dependency.
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"InputCore",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Slate",
			"SlateCore",
			// UMCFrameCapture encodes the captured frame through ImageWrapper.
			// It is an engine module rather than an image plugin, so it has no
			// plugin dependency and is always present.
			"ImageWrapper",
			"RHI",
			"RenderCore",
		});
	}
}
