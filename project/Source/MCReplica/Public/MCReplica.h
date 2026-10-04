// Module entry point.

#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

/**
 * Runtime module for the rebuilt campus.
 *
 * Exists to own the character and the instanced prop cluster, both of which
 * need a C++ class: a Blueprint cannot be authored from a headless editor
 * session, and neither a Character nor a placeable HISM actor is reachable
 * through UE 5.8's Python API.
 */
class FMCReplicaModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;
};
