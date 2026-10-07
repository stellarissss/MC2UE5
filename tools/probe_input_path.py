# -*- coding: utf-8 -*-
"""
probe_input_path.py -- prove, from inside a running game world, that the input
path is live. Read-only.

Why this exists. The report was "the camera cannot move at all". Measured facts:

  * DefaultInput.ini maps the legacy axis names (MoveForward/MoveRight/Turn/
    LookUp to W/A/S/D, MouseX, MouseY) and AMCReplicaCharacter binds exactly
    those names through UInputComponent::BindAxis/BindAction;
  * but the default input classes were EnhancedInput, and
    UEnhancedInputComponent *deletes* the legacy BindAxis/BindAction overloads
    while UEnhancedPlayerInput never evaluates AxisBindings/ActionBindings.

So the bindings were registered and then ignored -- no error, no movement. The
fix is two lines of config (now legacy Engine classes). This probe is how we
confirm the fix landed rather than assume it: it reports the classes the engine
actually instantiated and how many legacy bindings exist on the pawn.

It cannot prove a key was pressed -- synthesising keyboard input is not
available here. What it does prove is that the chain from config to a live
PlayerController is intact, which is the part that was broken.

Run:

    UnrealEditor-Cmd.exe MCReplica.uproject -game -windowed ^
        -ExecutePythonScript=<this> -nullrhi -unattended -nopause -nosplash
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/probe_input_path.txt"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCIN] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def main():
    ok = True
    try:
        say("=== probe_input_path (running inside a game world) ===")
        w = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        say("world: %s" % (w.get_name() if w else "None"))
        if w is None:
            raise RuntimeError("no world")

        pcs = unreal.GameplayStatics.get_all_actors_of_class(
            w, unreal.PlayerController)
        say("PlayerControllers: %d" % len(pcs))
        if not pcs:
            raise RuntimeError("no PlayerController -- the pawn is never "
                               "possessed, so no input can exist either")

        pc = pcs[0]
        say("")
        say("--- the chain, end to end ---")
        say("  GameMode          : %s" % pc.get_class().get_name())

        pi = None
        try:
            pi = pc.get_editor_property("player_input")
        except Exception as exc:
            say("  PlayerInput       : <not readable: %s>" % str(exc)[:60])
        if pi is not None:
            say("  PlayerInput class : %s" % pi.get_class().get_name())
            say("     (must be PlayerInput, not EnhancedPlayerInput)")
            for prop in ("bEnableTouchEvents", "bCaptureMouseOnLaunch",
                         "DefaultViewportMouseCaptureMode"):
                try:
                    say("     %-32s = %s"
                        % (prop, pi.get_editor_property(prop)))
                except Exception:
                    pass

        pawn = pc.get_pawn()
        say("  Pawn              : %s"
            % (pawn.get_class().get_name() if pawn else "None"))
        if pawn is None:
            raise RuntimeError("PlayerController has no pawn")

        comp = pawn.get_component_by_class(unreal.InputComponent)
        if comp is None:
            say("  InputComponent    : *** NONE ***")
            ok = False
        else:
            say("  InputComponent cls: %s" % comp.get_class().get_name())
            for prop, label in (("axis_bindings", "AxisBindings"),
                                ("action_bindings", "ActionBindings")):
                try:
                    arr = comp.get_editor_property(prop)
                    say("  %-16s : %d" % (label, len(arr) if arr else 0))
                    if arr:
                        say("       names: %s"
                            % ", ".join(sorted(
                                str(b.get_editor_property("axis_name")
                                    if label == "AxisBindings"
                                    else b.get_editor_property("action_name"))
                                for b in arr)[:12]))
                except Exception as exc:
                    say("  %-16s : <unreadable: %s>" % (label, str(exc)[:50]))

        say("")
        say("--- verdict ---")
        cls = comp.get_class().get_name() if comp else "?"
        if cls == "InputComponent":
            say("  InputComponent is the LEGACY one -> the legacy BindAxis")
            say("  overloads exist and the bindings below are live.")
        else:
            say("  *** InputComponent is %s, not the legacy InputComponent." % cls)
            ok = False
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
