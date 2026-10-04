// Editor target for the MCReplica project.
//
// Needed because the level is assembled by editor Python: cook runs through
// the editor binary, which resolves UnrealEditor-Cmd against this target's
// receipt. Without it the cook cannot find a project editor at all.

using UnrealBuildTool;

public class MCReplicaEditorTarget : TargetRules
{
	public MCReplicaEditorTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Editor;
		DefaultBuildSettings = BuildSettingsVersion.V7;

		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("MCReplica");
	}
}
