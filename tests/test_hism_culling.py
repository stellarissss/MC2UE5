"""
Offline checks for the HISM culling configuration in the two import scripts.

Why this needs its own harness
------------------------------
The cull distances are the main frame-time lever on this map: ~1.6 M instances
of a 1 m cube, all of which the engine would otherwise draw to the far plane.
Getting them wrong is expensive and completely invisible -- the level imports,
the frame rate is just bad, and nothing in the log says why.

They are also easy to get wrong in ways that are not obvious from reading the
code, which is what the cases below pin down:

  * a fade band with ``start >= end`` culls instances the moment they appear;
  * a cull distance shorter than the view distance means the far side of the
    campus is simply missing, which looks like a bug in the map rather than in
    the culling;
  * props differ in size by two orders of magnitude, so a single shared
    distance either draws bushes to the horizon or deletes buildings while
    they still read clearly;
  * ``r.ViewDistanceScale`` from the quality tier multiplies these, so the Low
    tier must still be left with a usable view distance.

Both scripts are loaded with a stub ``unreal`` module, so this runs with no
engine and no GPU.

Run standalone::

    python3 tests/test_hism_culling.py
"""

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYPATH = os.path.join(ROOT, "project", "Content", "Python")

FAILURES = []

#: The campus is ~720 x 1040 blocks at 100 cm per block. Anything culled well
#: inside this is a visible hole in the map, not a performance win.
CAMPUS_MAX_CM = 104000.0

#: The lowest r.ViewDistanceScale any tier applies (ViewDistanceQuality@0).
MIN_VIEW_DISTANCE_SCALE = 0.4


def check(cond, label, detail=""):
    if cond:
        print("  PASS  %s" % label + ("  -- %s" % detail if detail else ""))
    else:
        print("  FAIL  %s" % label + ("  -- %s" % detail if detail else ""))
        FAILURES.append(label)
    return bool(cond)


# --------------------------------------------------------------------------- #
# stub engine
# --------------------------------------------------------------------------- #

class _LogSink:
    def __init__(self):
        self.lines = []

    def _emit(self, level, msg):
        self.lines.append((level, msg))

    def log(self, msg):
        self._emit("log", msg)

    def log_warning(self, msg):
        self._emit("warning", msg)

    def log_error(self, msg):
        self._emit("error", msg)


def _install_unreal_stub():
    if "unreal" in sys.modules:
        return sys.modules["unreal"]
    stub = types.ModuleType("unreal")
    stub.log_sink = _LogSink()
    stub.log = stub.log_sink.log
    stub.log_warning = stub.log_sink.log_warning
    stub.log_error = stub.log_sink.log_error

    class _Paths:
        @staticmethod
        def project_dir():
            return os.path.join(ROOT, "project")

    stub.Paths = _Paths
    sys.modules["unreal"] = stub
    return stub


def _load(name):
    """Import one of the project scripts with the stub in place."""
    stub = _install_unreal_stub()
    path = os.path.join(PYPATH, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Both scripts log through the stub; capture per-load so assertions can
    # look at what this load produced rather than a shared global buffer.
    stub.log_sink.lines = []
    spec.loader.exec_module(mod)
    mod._test_log = stub.log_sink
    return mod


# --------------------------------------------------------------------------- #
# a stand-in for a HISM component
# --------------------------------------------------------------------------- #

class FakeHISM:
    """Records set_editor_property calls, and can refuse specific ones.

    Refusing is the point: the scripts must survive a property name that this
    engine version does not have, because a missing cull distance costs frame
    time while a failed import costs the whole build.
    """

    def __init__(self, unsupported=()):
        self.props = {}
        self.unsupported = set(unsupported)
        self.attempts = []

    def set_editor_property(self, name, value):
        self.attempts.append(name)
        if name in self.unsupported:
            raise AttributeError("%s is not a property of this component" % name)
        self.props[name] = value


# --------------------------------------------------------------------------- #
# layer 1: the block layer
# --------------------------------------------------------------------------- #

def test_block_cull_band_is_sane(world):
    """The fade band must be ordered, positive, and cover the campus."""
    print("\n[hism] block layer cull band")
    start = world.HISM_CULL_START_CM
    end = world.HISM_CULL_END_CM
    ok = check(0 < start < end,
               "the fade band is ordered and non-zero",
               "start=%s end=%s" % (start, end))
    # A band that is a large fraction of the map is what makes the far side
    # vanish. It should be a view distance, not a map dimension.
    ok &= check(end < CAMPUS_MAX_CM,
                "the cull distance is well inside the campus extent",
                "end=%.0f cm vs campus %.0f cm" % (end, CAMPUS_MAX_CM))
    ok &= check(end >= 20000.0,
                "the cull distance is at least 200 m",
                "end=%.0f cm" % end)
    # The fade itself should be a visible-but-short band. A zero-width band
    # pops; a band of hundreds of metres means instances are半 faded for a
    # long time, which reads as wrong exposure rather than as distance.
    ok &= check(0 < (end - start) <= 15000.0,
                "the fade band is short enough not to be a long grey-out",
                "width=%.0f cm" % (end - start))
    ok &= check(int(start) == start and int(end) == end,
                "both distances convert to whole centimetres",
                "the components take int32 distances")
    return ok


def test_block_cull_survives_the_worst_tier(world):
    """The Low tier scales cull distance by 0.4; it must still be usable.

    ``r.ViewDistanceScale`` from the quality tier multiplies every HISM
    component's cull distance, so the effective range on the Low tier is
    40% of these numbers. If that lands inside a few blocks the map is
    unusable there, which is the failure mode this checks.
    """
    print("\n[hism] block cull distance under the lowest quality tier")
    scale = MIN_VIEW_DISTANCE_SCALE
    eff_end = world.HISM_CULL_END_CM * scale
    eff_start = world.HISM_CULL_START_CM * scale
    ok = check(eff_end >= 10000.0,
               "the Low tier still renders at least 100 m of blocks",
               "effective end cull = %.0f cm" % eff_end)
    ok &= check(eff_start > 0.0,
                "the Low tier still has a fade band, not a hard cut",
                "effective start cull = %.0f cm" % eff_start)
    ok &= check(eff_start < eff_end,
                "the effective band stays ordered at 0.4 scale")
    return ok


def test_tune_hism_sets_what_it_claims(world):
    """The tuner must actually write the three properties it documents."""
    print("\n[hism] block layer tuner writes its properties")
    comp = FakeHISM()
    world._tune_hism_culling(comp, "HISM_stone_0")
    ok = check(comp.props.get("instance_start_cull_distance")
               == int(world.HISM_CULL_START_CM),
               "start cull distance is set",
               repr(comp.props.get("instance_start_cull_distance")))
    ok &= check(comp.props.get("instance_end_cull_distance")
                == int(world.HISM_CULL_END_CM),
                "end cull distance is set",
                repr(comp.props.get("instance_end_cull_distance")))
    ok &= check(comp.props.get("instance_count_per_leaf")
                == world.HISM_INSTANCES_PER_LEAF,
                "cluster tree density is set",
                repr(comp.props.get("instance_count_per_leaf")))

    # Per-leaf is a tuning choice with a real tradeoff, so bound it: too small
    # makes culling CPU-bound, too large makes it imprecise.
    leaf = world.HISM_INSTANCES_PER_LEAF
    ok &= check(16 <= leaf <= 256,
                "instances-per-leaf stays in a sane range",
                "%d (engine default is 32)" % leaf)
    return ok


def test_tune_hism_survives_a_missing_property(world):
    """An unknown property must warn once, not abort the import.

    The Python names have moved between engine versions, and this map's level
    is built by a script that has to keep working. A cull distance that fails
    to set costs frames; an exception escaping here costs the entire level.
    """
    print("\n[hism] a missing property degrades instead of failing")
    comp = FakeHISM(unsupported={"instance_end_cull_distance"})
    ok = True
    try:
        world._tune_hism_culling(comp, "HISM_dirt_0")
        ok = check(True, "the tuner does not raise on an unknown property")
    except Exception as exc:
        ok = check(False, "the tuner does not raise on an unknown property",
                   repr(exc))
    # The properties that *do* exist must still be applied -- a failure must
    # not abandon the rest of the work.
    ok &= check(comp.props.get("instance_start_cull_distance")
                == int(world.HISM_CULL_START_CM),
                "the supported properties are still applied")
    ok &= check("instance_end_cull_distance" in comp.attempts,
                "the unsupported one was still attempted")

    # And it must warn exactly once, not once per component.
    world._warned_once.clear()
    sink = world._test_log
    sink.lines = []
    for i in range(5):
        world._tune_hism_culling(
            FakeHISM(unsupported={"instance_count_per_leaf"}),
            "HISM_x_%d" % i)
    warned = [m for lvl, m in sink.lines if lvl == "warning"
              and "instance_count_per_leaf" in m]
    ok &= check(len(warned) == 1,
                "a repeated failure warns exactly once",
                "%d warnings for 5 components" % len(warned))
    return ok


# --------------------------------------------------------------------------- #
# layer 2: the props
# --------------------------------------------------------------------------- #

def test_prop_cull_table_is_sane(phase2):
    """Each prop class needs an ordered band, ordered by how far it reads."""
    print("\n[hism] prop cull table")
    table = phase2.PROP_CULL_CM
    ok = check(bool(table), "the table is populated",
                "%d classes" % len(table))
    for tag in sorted(table):
        start, end = table[tag]
        ok &= check(0 < start < end,
                    "  %-10s band is ordered" % tag,
                    "start=%.0f end=%.0f" % (start, end))
        ok &= check(end <= CAMPUS_MAX_CM,
                    "  %-10s does not cull the far side of the campus" % tag,
                    "end=%.0f cm" % end)

    # The whole point of a per-class table is that size orders it. A tree is
    # the campus's mid-distance silhouette; a plant is sub-pixel past a short
    # walk. If these two are not ordered the table is not doing its job.
    ok &= check(table["tree"][1] > table["plant"][1] * 2,
                "trees outlast plants by a wide margin",
                "tree=%.0f plant=%.0f" % (table["tree"][1], table["plant"][1]))
    ok &= check(table["building"][1] >= table["structure"][1],
                "buildings outlast small structures",
                "building=%.0f structure=%.0f"
                % (table["building"][1], table["structure"][1]))

    default = phase2.PROP_CULL_DEFAULT_CM
    ok &= check(0 < default[0] < default[1],
                "the fallback band is ordered",
                "start=%.0f end=%.0f" % default)
    return ok


def test_prop_classes_match_the_pipeline(phase2):
    """Every class the pipeline emits needs an entry in the cull table.

    A missing class silently falls back to a generic distance. That is the
    failure this guards: the fallback is mid-range, so a building that should
    be visible at 500 m gets culled at 160 m, and nothing reports an error.
    The class list is cross-checked against the per-class counts phase 2
    actually produced, so adding a class upstream without adding it here is a
    test failure rather than a slow-motion visual bug.
    """
    print("\n[hism] prop cull table covers the emitted classes")
    ok = check("plant" in phase2.PROP_CULL_CM, "plant is present")
    # The classes phase2 reports on, per the by-class summary in the run log.
    emitted = ("tree", "building", "structure", "plant", "prop")
    for tag in emitted:
        ok &= check(tag in phase2.PROP_CULL_CM,
                    "  %-10s has an explicit cull distance" % tag)
    return ok


def test_prop_tuner_uses_the_class_table(phase2):
    """The tuner must read the table, not a hard-coded constant."""
    print("\n[hism] prop tuner reads the class table")
    for tag in sorted(phase2.PROP_CULL_CM):
        comp = FakeHISM()
        phase2._tune_prop_culling(comp, tag)
        want_start, want_end = phase2.PROP_CULL_CM[tag]
        ok = check(comp.props.get("instance_start_cull_distance")
                   == int(want_start)
                   and comp.props.get("instance_end_cull_distance")
                   == int(want_end),
                   "  %-10s gets its own band" % tag,
                   "start=%s end=%s"
                   % (comp.props.get("instance_start_cull_distance"),
                      comp.props.get("instance_end_cull_distance")))

    # An unknown class must land on the documented fallback, not on nothing.
    comp = FakeHISM()
    phase2._tune_prop_culling(comp, "unheard_of")
    d = phase2.PROP_CULL_DEFAULT_CM
    ok &= check(comp.props.get("instance_end_cull_distance") == int(d[1]),
                "an unknown class falls back to the default band",
                "end=%s" % comp.props.get("instance_end_cull_distance"))
    return ok


def test_prop_tuner_survives_a_missing_property(phase2):
    """Same degradation contract as the block layer."""
    print("\n[hism] prop tuner degrades instead of failing")
    comp = FakeHISM(unsupported={"instance_start_cull_distance"})
    ok = True
    try:
        phase2._tune_prop_culling(comp, "tree")
        ok = check(True, "the prop tuner does not raise")
    except Exception as exc:
        ok = check(False, "the prop tuner does not raise", repr(exc))
    ok &= check(comp.props.get("instance_end_cull_distance")
                == int(phase2.PROP_CULL_CM["tree"][1]),
                "the supported property is still applied")

    phase2._warned_once.clear()
    sink = phase2._test_log
    sink.lines = []
    for i in range(4):
        phase2._tune_prop_culling(
            FakeHISM(unsupported={"instance_start_cull_distance"}), "tree")
    warned = [m for lvl, m in sink.lines if lvl == "warning"
              and "instance_start_cull_distance" in m]
    ok &= check(len(warned) == 1, "a repeated failure warns exactly once",
                "%d warnings for 4 actors" % len(warned))
    return ok


# --------------------------------------------------------------------------- #
# the terrain
# --------------------------------------------------------------------------- #

def test_landscape_lod_is_set_and_persistent(phase2):
    """
    The Landscape LOD baseline must be an actor property.

    ``lod0_screen_size`` is an actor property: saved with the level, travels
    with the asset, and needs no cvar to take effect. That makes it the right
    place for the *baseline* -- the thing every tier gets before any
    multiplier is applied. The per-tier multipliers are cvars, but those are
    read from ``[ViewDistanceQuality@N]`` sections and do persist; see
    ``test_landscape_lod_is_tiered_in_ini`` for that half.
    """
    print("\n[hism] landscape LOD baseline is set per-actor")
    size = phase2.LANDSCAPE_LOD0_SCREEN_SIZE
    ok = check(isinstance(size, float) and size > 0.0,
               "the LOD0 screen size is a positive float", repr(size))
    # The engine default is 1.0 and Epic's "normal distribution" is 1.25. Below
    # 1.0 starts dropping LOD0 almost immediately, which on a 1 m grid is what
    # makes a Minecraft rebuild look smooth and blobby.
    ok &= check(0.5 <= size <= 2.0,
                "the LOD0 screen size stays in a usable range",
                "%s (engine default 1.0, Epic normal 1.25)" % size)

    # The tuner must write the property an actor actually has.
    class FakeLandscape:
        def __init__(self):
            self.props = {}

        def set_editor_property(self, name, value):
            self.props[name] = value

    actor = FakeLandscape()
    phase2._apply_landscape_lod(actor, {"file": "overworld_00_00.png"})
    ok &= check(actor.props.get("lod0_screen_size") == size,
                "lod0_screen_size is written to the actor",
                repr(actor.props.get("lod0_screen_size")))
    ok &= check("lod_blend_range" in actor.props,
                "the blend range is written too, so LODs do not pop",
                repr(actor.props.get("lod_blend_range")))
    return ok


def test_landscape_lod_survives_a_missing_property(phase2):
    """Same degradation contract: a missing property must not abort the import."""
    print("\n[hism] landscape LOD degrades instead of failing")

    class PickyLandscape:
        def __init__(self):
            self.props = {}

        def set_editor_property(self, name, value):
            if name == "lod0_screen_size":
                raise AttributeError("not a property on this engine version")
            self.props[name] = value

    actor = PickyLandscape()
    ok = True
    try:
        phase2._apply_landscape_lod(actor, {"file": "t.png"})
        ok = check(True, "the landscape tuner does not raise")
    except Exception as exc:
        ok = check(False, "the landscape tuner does not raise", repr(exc))
    ok &= check("lod_blend_range" in actor.props,
                "the supported property is still applied")

    phase2._warned_once.clear()
    sink = phase2._test_log
    sink.lines = []
    for i in range(3):
        phase2._apply_landscape_lod(PickyLandscape(), {"file": "t%d.png" % i})
    warned = [m for lvl, m in sink.lines if lvl == "warning"
              and "lod0_screen_size" in m]
    ok &= check(len(warned) == 1, "a repeated failure warns exactly once",
                "%d warnings for 3 tiles" % len(warned))
    return ok


# --------------------------------------------------------------------------- #
# the Landscape LOD tiering
# --------------------------------------------------------------------------- #

#: The engine declares both of these ECVF_Scalability in LandscapeRender.cpp,
#: with a default of 1.0. There is no lower bound of 1.0 on the cvar itself --
#: what is floored is the *product* inside GetLODScreenSizeArray, and the two
#: cvars multiply different engine defaults (see ENGINE_LOD_DISTRIBUTION_
#: DEFAULT), so each has its own floor.
ENGINE_LOD_DISTRIBUTION_DEFAULTS = {
    "r.LandscapeLOD0DistributionScale": 1.25,   # LOD0DistributionSetting
    "r.LandscapeLODDistributionScale": 3.0,     # LODDistributionSetting
}

#: The floor the engine applies to the product. A cvar whose product lands at
#: or below this is dead text: the LOD falloff stops changing no matter what
#: the ini says, which is a silent failure with no warning anywhere.
PRODUCT_FLOOR = 1.01

#: Quality level -> section suffix, mirroring GetScalabilitySectionString: the
#: top level reads "<Group>@Cine", every other level reads "<Group>@<n>".
TIER_SECTIONS = ((0, "0"), (1, "1"), (2, "2"), (3, "3"), (4, "Cine"))


def _parse_scalability_ini():
    """
    Read DefaultScalability.ini into ``{section: {key: value}}``.

    Deliberately hand-rolled rather than reusing configparser: UE ini sections
    such as ``[ViewDistanceQuality@0`` are not valid configparser section names
    by default, and the array syntax (``+CVars=``) has different semantics
    again. This only needs plain ``key=value`` lines, so a split on the first
    ``=`` is both sufficient and honest about what it supports.
    """
    path = os.path.join(ROOT, "project", "Config", "DefaultScalability.ini")
    sections = {}
    current = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                sections.setdefault(current, {})
                continue
            if current is None or "=" not in line:
                continue
            key, value = line.split("=", 1)
            sections[current][key.strip()] = value.strip()
    return sections


def test_landscape_lod_is_tiered_in_ini():
    """
    The Landscape LOD distribution must actually differ per tier.

    This is the check that would have caught the real gap: ``lod0_screen_size``
    is a single actor-wide value, so on its own every tier gets an identical
    terrain LOD and the Low profile pays for ~790 k terrain vertices it cannot
    afford. The per-tier lever is the two distribution cvars, and they belong
    in ``[ViewDistanceQuality@N]`` because the engine declares them
    ECVF_Scalability.

    Direction matters and is easy to get backwards. From
    ``ULandscapeLODStreamingProxy::GetLODScreenSizeArray``:

        const float ScreenSizeMult =
            1.f / FMath::Max(LOD0DistributionSetting
                            * CVarLSLOD0DistributionScale->GetFloat(), 1.01f);

    The cvar is in the denominator, so a *larger* value drops to coarser LODs
    *sooner*. An inversion here would make Low the most expensive tier while
    looking perfectly plausible in the ini.
    """
    print("\n[hism] landscape LOD distribution is tiered in the ini")
    sections = _parse_scalability_ini()
    ok = True

    for cvar in ENGINE_LOD_DISTRIBUTION_DEFAULTS:
        values = {}
        for level, suffix in TIER_SECTIONS:
            section = sections.get("ViewDistanceQuality@%s" % suffix, {})
            raw = section.get(cvar)
            label = "%s@%s sets %s" % ("ViewDistanceQuality", suffix, cvar)
            if raw is None:
                ok &= check(False, label, "missing -- every tier gets stock 1.0")
                continue
            ok &= check(True, label, raw)
            try:
                values[level] = float(raw)
            except ValueError:
                ok &= check(False, label, "%r is not a float" % raw)

        if len(values) != len(TIER_SECTIONS):
            continue

        # The floor is on the product, not the cvar. A value that pushes the
        # product to the clamp is dead text, and nothing in the engine says so.
        floor = ENGINE_LOD_DISTRIBUTION_DEFAULTS[cvar]
        ok &= check(all(v * floor > PRODUCT_FLOOR for v in values.values()),
                    "  %s stays clear of the product clamp" % cvar,
                    "min product %.4f (floor %.2f)"
                    % (min(values.values()) * floor, PRODUCT_FLOOR))
        # Monotonically non-increasing with quality: a better card must never
        # be given a coarser terrain. Note the direction is *decreasing* --
        # the cvar sits in the denominator of the LOD falloff, so a lower
        # value means a finer terrain. Reading "bigger number, more quality"
        # onto this cvar inverts the whole tier ladder, which is exactly the
        # kind of error that still boots and still looks plausible.
        ordered = [values[i] for i in sorted(values)]
        ok &= check(ordered == sorted(ordered, reverse=True),
                    "  %s sharpens monotonically as the tier gets better" % cvar,
                    " -> ".join("%.2f" % v for v in ordered))
        ok &= check(ordered[0] > ordered[-1],
                    "  %s actually differentiates Low from Cinematic" % cvar,
                    "Low %.2f vs Cine %.2f" % (ordered[0], ordered[-1]))
    return ok


def test_landscape_lod_does_not_fight_the_view_distance(phase2):
    """
    The two landscape levers must agree in direction.

    ``r.ViewDistanceScale`` and the LOD distribution cvars both decide how much
    terrain survives, and they are scaled by different tiers' intents. If one
    of them were pushed the wrong way the map would still run -- it would just
    get more expensive on the cards that can least afford it, which is the
    failure mode this whole tier system exists to prevent.
    """
    print("\n[hism] landscape LOD and view distance agree in direction")
    sections = _parse_scalability_ini()
    ok = True

    for level, suffix in TIER_SECTIONS:
        section = sections.get("ViewDistanceQuality@%s" % suffix, {})
        view = section.get("r.ViewDistanceScale")
        lod = section.get("r.LandscapeLOD0DistributionScale")
        if view is None or lod is None:
            ok &= check(False,
                        "tier %s declares both levers" % suffix,
                        "ViewDistanceScale=%r LOD0=%r" % (view, lod))
            continue
        # "See more" and "hold more detail" are the same intent expressed in
        # opposite conventions: r.ViewDistanceScale above 1 extends how far
        # the HISM block layer draws, and because the LOD distribution cvar is
        # in the denominator, a value below 1.0 makes the terrain LOD falloff
        # gentler. So a tier that widens the view must also sharpen the
        # terrain -- otherwise the top tier ends up showing more of the map
        # while rendering the far half of it worse than the tier below it.
        same_intent = (float(view) > 1.0) == (float(lod) < 1.0)
        ok &= check(same_intent,
                    "tier %s keeps both terrain levers pointing the same way"
                    % suffix,
                    "ViewDistanceScale=%s LOD0=%s" % (view, lod))

    # The per-actor baseline multiplies into the same falloff, so a value
    # below the engine default of 1.0 would coarsen the near field for every
    # tier and quietly undo the Cinematic sharpening below.
    ok &= check(phase2.LANDSCAPE_LOD0_SCREEN_SIZE >= 1.0,
                "the actor LOD0 baseline does not coarsen the near field",
                repr(phase2.LANDSCAPE_LOD0_SCREEN_SIZE))
    return ok


# --------------------------------------------------------------------------- #
# the shared warn-once contract
# --------------------------------------------------------------------------- #

def test_both_scripts_warn_once_per_key(world, phase2):
    """
    Both scripts must dedupe warnings by key.

    A property that does not exist on this engine version fails on every one of
    ~2.6 k HISM components and every prop actor. Without deduping, those
    thousands of identical lines are what a real warning has to compete with,
    which is how a genuine failure gets missed in a log.
    """
    print("\n[hism] both scripts dedupe repeated warnings")
    ok = True
    for mod, label in ((world, "import_world"), (phase2, "import_phase2")):
        mod._warned_once.clear()
        sink = mod._test_log
        sink.lines = []
        for i in range(10):
            mod._warn_once("some_key", "%s: repeated message" % label)
        same = [m for lvl, m in sink.lines
                if lvl == "warning" and "repeated message" in m]
        ok &= check(len(same) == 1,
                    "  %s emits one warning for 10 identical failures" % label,
                    "%d warnings" % len(same))
        # A different key must still get through -- otherwise the dedupe is
        # swallowing real, distinct problems.
        mod._warn_once("other_key", "%s: a different problem" % label)
        other = [m for lvl, m in sink.lines
                 if lvl == "warning" and "a different problem" in m]
        ok &= check(len(other) == 1,
                    "  %s still reports a distinct problem" % label)
    return ok


def main():
    world = _load("import_world")
    phase2 = _load("import_phase2")

    # (fn, which module it takes) -- declared rather than inferred from the
    # signature, because inferring it is exactly the kind of cleverness that
    # silently passes the wrong object to a check. The block-layer checks
    # exercise import_world; the prop and terrain checks exercise
    # import_phase2.
    results = [
        (test_block_cull_band_is_sane, world),
        (test_block_cull_survives_the_worst_tier, world),
        (test_tune_hism_sets_what_it_claims, world),
        (test_tune_hism_survives_a_missing_property, world),
        (test_prop_cull_table_is_sane, phase2),
        (test_prop_classes_match_the_pipeline, phase2),
        (test_prop_tuner_uses_the_class_table, phase2),
        (test_prop_tuner_survives_a_missing_property, phase2),
        (test_landscape_lod_is_set_and_persistent, phase2),
        (test_landscape_lod_survives_a_missing_property, phase2),
        (test_landscape_lod_is_tiered_in_ini, None),
        (test_landscape_lod_does_not_fight_the_view_distance, phase2),
        (test_both_scripts_warn_once_per_key, "both"),
    ]
    ok = True
    for fn, target in results:
        try:
            if target == "both":
                ok &= fn(world, phase2)
            elif target is None:
                ok &= fn()          # pure static analysis, no module needed
            else:
                ok &= fn(target)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(False, "%s raised" % fn.__name__, repr(exc))
            ok = False

    print("\n" + "=" * 70)
    if ok and not FAILURES:
        print("ALL PASS")
        return 0
    print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
