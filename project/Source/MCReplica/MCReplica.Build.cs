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
			// The terrain is built as a ProceduralMeshComponent at runtime. The
			// OBJ importer reads only the first ~640 vertices of a file, so a
			// 196,944-vertex terrain tile arrives almost empty; the heightfield
			// is shipped as raw data and triangulated here instead.
			"ProceduralMeshComponent",
			// UMCMeshTools writes vertex-instance normals into a mesh
			// description. A description built from script has no normals and
			// UStaticMesh::BuildFromMeshDescription copies them verbatim into
			// the render data without computing them, so a script-built surface
			// shades with a zero normal and renders black. Both modules are
			// engine-side and referenced only from WITH_EDITOR code.
			"MeshDescription",
			"StaticMeshDescription",
		});
	}
}
