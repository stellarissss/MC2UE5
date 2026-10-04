// Game target for the MCReplica project.

using UnrealBuildTool;

public class MCReplicaTarget : TargetRules
{
	public MCReplicaTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Game;
		DefaultBuildSettings = BuildSettingsVersion.V7;

		// The whole feature set is in the project module; there is no editor
		// code in it, so the game target links only what the game needs.
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("MCReplica");
	}
}
