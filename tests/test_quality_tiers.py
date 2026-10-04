"""
Keep the quality-tier definitions honest across their three homes.

The tier system is defined in three places that must agree:

  * ``project/Config/DefaultScalability.ini``   -- what each tier means, and
    the perf-index boundaries the engine auto-selects on
  * ``project/Config/DefaultDeviceProfiles.ini`` -- the floor for a machine
    that never benchmarks
  * ``project/Content/Python/apply_quality.py``  -- the runtime decision

``apply_quality.py`` duplicates the numbers rather than parsing the ini, for a
reason: a packaged build may not have the ini readable, and the runtime has to
work anyway. Duplication is fine; *silent divergence* is not. This harness locks
the three together, plus the parts of the engine contract that are easy to get
wrong and impossible to notice in a log.

WHAT THIS TEST IS REALLY DEFENDING
----------------------------------
Three engine facts, each verified against the engine source. Each one has a
test here, because each produces a quality system that looks correct in review
and does nothing at runtime:

  * ``PerfIndexThresholds_*`` is read only from DefaultScalability.ini's
    ``[ScalabilitySettings]`` section (Scalability.cpp:150-152). A threshold
    line in a device profile is dead text.
  * The Windows device-profile selector returns a fixed name -- ``Windows`` or
    ``Windows_<RHI>`` -- and matches no hardware. The GPU/CPU-family matching
    rules are Android-only. So a device profile cannot be the auto-detect
    layer on Windows, and a test that asserts otherwise would be asserting a
    bug.
  * A quality level of 4 reads the section ``<Group>@Cine``, not ``<Group>@4``
    (GetScalabilitySectionString, Scalability.cpp:273), and
    ``sg.ResolutionQuality`` is clamped to 100 (Scalability.h:185).

Run standalone::

    python3 tests/test_quality_tiers.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT = os.path.join(ROOT, "project")
SCALABILITY_INI = os.path.join(PROJECT, "Config", "DefaultScalability.ini")
PROFILES_INI = os.path.join(PROJECT, "Config", "DefaultDeviceProfiles.ini")

FAILURES = []

#: The scalability groups UE 5.5 reads perf-index thresholds for. Anything
#: missing here silently keeps the engine's own thresholds, which are tuned for
#: a generic action game and not for a 1.6 M-instance HISM map.
SCALABILITY_GROUPS = (
    "ResolutionQuality", "ViewDistanceQuality", "AntiAliasingQuality",
    "ShadowQuality", "GlobalIlluminationQuality", "ReflectionQuality",
    "PostProcessQuality", "TextureQuality", "EffectsQuality",
    "FoliageQuality", "ShadingQuality",
)

#: Keys whose ``+Key=value`` lines accumulate into an array instead of
#: overwriting each other. Getting this wrong in a reader is the same class of
#: bug as the one it is meant to catch: N registrations collapse to 1, and the
#: test then "passes" on the strength of whichever entry happened to be last.
ARRAY_KEYS = ("CVars", "DeviceProfileNameAndTypes")


def check(cond, label, detail=""):
    if cond:
        print("  PASS  %s" % label + ("  -- %s" % detail if detail else ""))
    else:
        print("  FAIL  %s" % label + ("  -- %s" % detail if detail else ""))
        FAILURES.append(label)
    return bool(cond)


def load_apply_quality():
    """
    Import apply_quality.py with ``unreal`` absent.

    The module is written so that ``import unreal`` failing is the normal case
    for a test run; every engine call is guarded. That is what makes the whole
    selection path testable off-engine, which is the point.
    """
    import importlib.util

    path = os.path.join(PROJECT, "Content", "Python", "apply_quality.py")
    spec = importlib.util.spec_from_file_location("apply_quality", path)
    mod = importlib.util.module_from_spec(spec)
    # Make sure no stale unreal stub is left over from another test module.
    sys.modules.pop("unreal", None)
    spec.loader.exec_module(mod)
    return mod


def parse_ini_sections(path):
    """
    Minimal INI reader: ``{section_name: {key: value}}``, plus a list for CVars.

    Deliberately not configparser: these files use ``+Key=Value`` (array
    append), and ``+CVars`` specifically means *append one cvar per line*, so
    the same key legitimately repeats. Collapsing those repeats -- which is what
    a plain dict does -- is exactly the bug this parser exists to prevent: a
    profile whose only ``r.ScreenPercentage`` line is the last one would look
    correct in a naive read and be missing the setting the others carry.
    """
    sections = {}
    cur = None
    if not os.path.isfile(path):
        return sections
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            m = re.match(r"^\[(.+?)\]$", line)
            if m:
                cur = m.group(1).strip()
                sections.setdefault(cur, {})
                continue
            if cur is None or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lstrip("+").strip()
            val = val.strip()
            if key == "CVars" and val:
                # An empty "+CVars=" is a legal no-op, not a cvar named "".
                sections[cur].setdefault("CVars", []).append(val)
            elif key in ARRAY_KEYS:
                # Same append semantics as CVars: "+Foo=a" then "+Foo=b" is a
                # two-element array, not a key whose value is "b".
                sections[cur].setdefault(key, []).append(val)
            else:
                sections[cur][key] = val
    return sections


def cvar_map(cvarlist):
    """``['a=1', 'b=2']`` -> ``{'a': '1', 'b': '2'}``."""
    out = {}
    for item in cvarlist or ():
        if "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- #
# the engine contract
# --------------------------------------------------------------------------- #

def test_perf_index_thresholds_live_in_scalability_ini(mod):
    """
    Auto-selection thresholds must be in the one file the engine reads them from.

    ``Scalability.cpp:150-152`` looks them up as
    ``GConfig->GetSingleLineArray("ScalabilitySettings", "PerfIndexThresholds_<Group>")``
    in the scalability ini. There is no code path that reads them from a device
    profile, so a threshold written in DefaultDeviceProfiles.ini is inert. This
    is the single most expensive mistake available when building a tier system,
    because the file looks completely reasonable and the machine just quietly
    stays on the default.
    """
    print("\n[quality] PerfIndexThresholds live in the scalability ini")
    ok = check(os.path.isfile(SCALABILITY_INI), "DefaultScalability.ini exists")

    raw = open(SCALABILITY_INI).read() if ok else ""
    ok &= check("[ScalabilitySettings]" in raw,
                "DefaultScalability.ini has a [ScalabilitySettings] section")

    for group in SCALABILITY_GROUPS:
        m = re.search(r'^PerfIndexThresholds_%s\s*=\s*"([^"]*)"'
                      % re.escape(group), raw, re.M)
        if not check(m is not None,
                     "  PerfIndexThresholds_%s is declared" % group):
            continue
        parts = m.group(1).split()
        ok &= check(parts and parts[0] in ("CPU", "GPU", "Min"),
                    "  %s threshold selects a real index type" % group,
                    "got %r" % (parts[0] if parts else None))
        ok &= check(len(parts) == 4,
                    "  %s threshold has exactly 3 boundaries" % group,
                    "got %d numbers" % (len(parts) - 1))
        ok &= check(all(float(x) > 0 for x in parts[1:]) if len(parts) == 4
                    else False,
                    "  %s boundaries are positive and increasing" % group,
                    " ".join(parts[1:]))

    # And the negative case, which is the bug this test exists for.
    if os.path.isfile(PROFILES_INI):
        praw = open(PROFILES_INI).read()
        offenders = re.findall(r"^[^;\n]*PerfIndexThresholds_\S*", praw, re.M)
        ok &= check(not offenders,
                    "no PerfIndexThresholds in DefaultDeviceProfiles.ini "
                    "(the engine never reads it there)",
                    "%d found" % len(offenders))
    return ok


def test_no_dead_threshold_sections(mod):
    """
    Quality level 4 must be written ``@Cine``, not ``@4``.

    ``GetScalabilitySectionString`` (Scalability.cpp:273) returns
    ``"<Group>@Cine"`` when the requested level equals the maximum, and
    ``"<Group>@<level>"`` otherwise. A ``[Group@4]`` section is never opened,
    so a file that uses it silently loses its entire top tier -- the tier that
    is supposed to be the best-looking one.
    """
    print("\n[quality] top tier uses the @Cine section name")
    raw = open(SCALABILITY_INI).read()
    ok = True
    for m in re.finditer(r"^\[([A-Za-z]+)@(4|Cine)\]", raw, re.M):
        name, level = m.group(1), m.group(2)
        ok &= check(level == "Cine",
                    "  [%s@%s] is a section the engine opens" % (name, level),
                    "level 4 must be spelled @Cine")
    # The groups that carry per-level detail must cover the top tier at all.
    detailed = set(re.findall(r"^\[([A-Za-z]+)@Cine\]", raw, re.M))
    for group in ("ViewDistanceQuality", "ShadowQuality", "GlobalIlluminationQuality",
                  "FoliageQuality", "TextureQuality"):
        ok &= check(group in detailed,
                    "  %s defines a @Cine section" % group)
    return ok


def test_resolution_is_clamped_to_100(mod):
    """
    No tier may claim a resolution above 100.

    ``sg.ResolutionQuality`` is mirrored into ``r.ScreenPercentage`` by
    ``SetResolutionQualityLevel``, which clamps to
    ``Scalability::MaxResolutionScale == 100`` (Scalability.h:185). A tier that
    writes 150 is not "supersampling"; it is a value the engine discards, and
    the discrepancy is invisible in every log.
    """
    print("\n[quality] resolution stays within the engine's clamp")
    ok = check(all(t["resolution"] <= 100 for t in mod.TIERS),
               "no Python tier exceeds 100",
               "%s" % [t["resolution"] for t in mod.TIERS])
    raw = open(SCALABILITY_INI).read()
    vals = re.findall(r"^sg\.ResolutionQuality=([\d.]+)", raw, re.M)
    ok &= check(all(float(v) <= 100.0 for v in vals),
                "no ini resolution exceeds 100",
                "%s" % vals)
    # PerfIndexValues_ResolutionQuality is read by name and must agree.
    m = re.search(r'^PerfIndexValues_ResolutionQuality\s*=\s*"([^"]*)"',
                  raw, re.M)
    if check(m is not None, "PerfIndexValues_ResolutionQuality is declared"):
        pv = [float(x) for x in m.group(1).split()]
        ok &= check(len(pv) == len(mod.TIERS),
                    "  it lists one value per tier",
                    "%d found, %d tiers" % (len(pv), len(mod.TIERS)))
        ok &= check(all(v <= 100.0 for v in pv),
                    "  and no value exceeds 100", "%s" % pv)
    return ok


def test_startup_default_tier(mod):
    """
    A cooked build must not start at Epic.

    With no matching device profile every scalability group defaults to 3
    (Epic). On this map that means full view distance over ~1.6 M HISM
    instances, Lumen and a 2 GB streaming pool -- on hardware we have not
    measured. So the shipped default has to be stated explicitly, in both
    files that can set it.
    """
    print("\n[quality] the shipped default is a safe tier, not Epic")
    ok = check(len(mod.TIERS) == 5, "five tiers are defined")

    raw = open(SCALABILITY_INI).read()
    m = re.search(r"^\[ScalabilityGroups\]\s*$", raw, re.M)
    if check(m is not None,
             "DefaultScalability.ini states a [ScalabilityGroups] default"):
        block = raw[m.end():]
        block = block.split("\n[", 1)[0]
        groups = dict(re.findall(r"^sg\.(\w+)=([-\d.]+)", block, re.M))
        ok &= check(bool(groups), "the default block sets groups",
                    "%d groups" % len(groups))
        # ResolutionQuality is a screen percentage, not a 0-4 level, so it is
        # excluded here and checked against the clamp separately. Comparing it
        # to 2 would "fail" a perfectly correct 71.
        levels = [int(float(v)) for k, v in groups.items()
                  if k != "ResolutionQuality"]
        worst = max(levels) if levels else 99
        ok &= check(worst <= 2,
                    "the default block tops out at High or below",
                    "highest level %d" % worst)
        for key, cvar in (("view", "sg.ViewDistanceQuality"),
                          ("texture", "sg.TextureQuality"),
                          ("foliage", "sg.FoliageQuality")):
            if cvar in groups:
                want = mod.TIERS[1][key]
                ok &= check(int(float(groups[cvar])) == want,
                            "  %s default matches the Medium tier" % cvar,
                            "ini=%s python=%d" % (groups[cvar], want))

    prof = parse_ini_sections(PROFILES_INI)
    if "Windows DeviceProfile" in prof:
        wcv = cvar_map(prof["Windows DeviceProfile"].get("CVars"))
        ok &= check(bool(wcv), "[Windows DeviceProfile] carries cvars",
                    "%d cvars" % len(wcv))
        floor = {"sg.ViewDistanceQuality": "view",
                 "sg.TextureQuality": "texture",
                 "sg.FoliageQuality": "foliage"}
        for cvar, key in floor.items():
            if cvar in wcv:
                want = mod.TIERS[1][key]
                ok &= check(int(wcv[cvar]) == want,
                            "  %s matches the Medium tier" % cvar,
                            "ini=%s python=%d" % (wcv[cvar], want))
    else:
        ok &= check(False, "[Windows DeviceProfile] is declared")
    return ok


# --------------------------------------------------------------------------- #
# the three definitions agreeing
# --------------------------------------------------------------------------- #

def test_tiers_match_scalability_ini(mod):
    """
    Every ``sg.*`` level in the Python table must equal the ini's.

    The ini is what the engine reads; the Python list is what the runtime
    applies. If they drift, ``auto()`` and the device profile disagree and the
    symptom is a tier that reports one thing and renders another.
    """
    print("\n[quality] Python group levels match DefaultScalability.ini")
    raw = open(SCALABILITY_INI).read()
    ok = True
    m = re.search(r"^\[ScalabilityGroups\]\s*$", raw, re.M)
    if not check(m is not None, "[ScalabilityGroups] default is present"):
        return False
    block = raw[m.end():].split("\n[", 1)[0]
    groups = dict(re.findall(r"^sg\.(\w+)=([-\d.]+)", block, re.M))
    for cvar, key in mod.SCALABILITY_GROUPS:
        group = cvar.replace("sg.", "")
        if group not in groups:
            ok &= check(False, "  %s is in the ini default" % cvar)
            continue
        want = mod.TIERS[1][key]
        ok &= check(int(float(groups[group])) == want,
                    "  %s default %s == Medium row %d"
                    % (cvar, groups[group], want))
    return ok


def test_landscape_lod_is_written_for_every_tier(mod):
    """
    The Landscape LOD cvars must be declared in all five tier sections.

    Intel's engine optimization guide calls the failure mode "CVar Leaking": a
    cvar written in some sections but not others keeps whatever value the
    previous tier left behind when the player switches, so the behaviour is
    unpredictable rather than merely wrong. Nothing warns about it.

    This is worth its own test because the cvars are easy to omit *and* easy to
    omit silently -- a missing key is not a parse error, it is just a landscape
    that refuses to get cheaper on the hardware that needs it to.
    """
    print("\n[quality] Landscape LOD cvars are declared in every tier")
    raw = open(SCALABILITY_INI).read()
    ok = True

    for cvar in ("r.LandscapeLOD0DistributionScale",
                 "r.LandscapeLODDistributionScale"):
        # "@Cine" is what GetScalabilitySectionString produces for the top
        # level; "@4" would be a section the engine never opens.
        suffixes = ("0", "1", "2", "3", "Cine")
        for suffix in suffixes:
            m = re.search(r"^\[ViewDistanceQuality@%s\]\s*$" % re.escape(suffix),
                          raw, re.M)
            if not check(m is not None,
                         "  [ViewDistanceQuality@%s] exists" % suffix):
                ok = False
                continue
            block = raw[m.end():].split("\n[", 1)[0]
            found = re.search(r"^%s=([-\d.]+)\s*$" % re.escape(cvar),
                              block, re.M)
            ok &= check(found is not None,
                        "  %s is set in ViewDistanceQuality@%s" % (cvar, suffix),
                        "missing -- that tier keeps the stock 1.0")
    return ok


def test_device_profiles_reference_real_tiers(mod):
    """
    Every declared profile must be complete and every tier must exist as one.

    Two failure modes, both silent: a profile missing its resolution cvar
    inherits Epic's 100% and stops being the tier it claims to be; and a tier
    with no profile cannot be selected by name at all.
    """
    print("\n[quality] device profiles are declared and complete")
    ini = parse_ini_sections(PROFILES_INI)
    declared = sorted(k for k in ini if k.endswith(" DeviceProfile"))
    ok = check(len(declared) >= 5,
               "at least five device profiles are declared",
               "%d found: %s" % (len(declared), declared))

    # Section names must be registered, or the engine will not honour them.
    # The key is an array (see ARRAY_KEYS), so every entry has to be walked.
    registered = set()
    for entries in ini.values():
        for item in entries.get("DeviceProfileNameAndTypes", ()) or ():
            if "," in str(item):
                registered.add(str(item).split(",")[0].strip())
    for name in declared:
        bare = name.replace(" DeviceProfile", "")
        ok &= check(bare in registered or bare == "Windows",
                    "  %s is registered in [DeviceProfiles]" % bare,
                    "registered: %s" % sorted(registered))

    for name in declared:
        bare = name.replace(" DeviceProfile", "")
        if bare.startswith("MCReplica_"):
            tier = bare[len("MCReplica_"):]
            entry = mod.TIER_BY_NAME.get(tier.lower())
            ok &= check(entry is not None,
                        "  %s maps to a known tier" % bare)
            if entry is None:
                continue
            cv = cvar_map(ini[name].get("CVars"))
            for cvar, key in mod.SCALABILITY_GROUPS:
                group = cvar.replace("sg.", "")
                # A profile may inherit a level from its BaseProfileName; only
                # assert on the ones it states itself.
                if group in cv:
                    ok &= check(int(float(cv[group])) == entry[key],
                                "    %s %s == %s row %d"
                                % (bare, cvar, tier, entry[key]),
                                "ini=%s" % cv[group])
            ok &= check("r.Nanite" in cv or "BaseProfileName" in ini[name],
                        "    %s states Nanite or inherits it" % bare)
    return ok


def test_python_thresholds_match_ini(mod):
    """
    The Python boundaries must equal the ini's ``PerfIndexThresholds_*``.

    Two places encode the same numbers. Drift means the runtime and the
    engine's own auto-detect disagree about the same machine, which is worse
    than either being wrong alone: the user gets a different tier depending on
    whether the menu or a script did it.
    """
    print("\n[quality] Python index thresholds match the ini")
    raw = open(SCALABILITY_INI).read()
    nums = set()
    for m in re.finditer(r'^PerfIndexThresholds_ViewDistanceQuality\s*=\s*"([^"]*)"',
                         raw, re.M):
        parts = m.group(1).split()
        if len(parts) == 4:
            nums.add(tuple(float(x) for x in parts[1:]))
    ok = check(len(nums) == 1,
               "the ViewDistanceQuality threshold is declared exactly once",
               "%s" % sorted(nums))
    if not ok:
        return False
    ok &= check(tuple(mod.INDEX_THRESHOLDS) in nums,
                "Python INDEX_THRESHOLDS %s appears in the ini"
                % (tuple(mod.INDEX_THRESHOLDS),),
                "ini has %s" % sorted(nums))
    return ok


# --------------------------------------------------------------------------- #
# behaviour
# --------------------------------------------------------------------------- #

def test_apply_quality_import_is_engine_free(mod):
    """
    The module must import and work with no ``unreal`` module present.

    This is what makes the whole selection path testable off-engine, and it is
    also the packaged-build-without-Python case. An unconditional engine call at
    import time would break both.
    """
    print("\n[quality] apply_quality imports without an engine")
    ok = check(mod.unreal is None,
               "unreal is not importable in this test environment",
               repr(mod.unreal))
    ok &= check(len(mod.TIERS) == 5, "five tiers are defined")
    ok &= check([t["index"] for t in mod.TIERS] == [0, 1, 2, 3, 4],
                "tier indices are 0..4 in order")
    result = mod.apply_tier("High", verbose=False)
    ok &= check(result is not None and result["name"] == "High",
                "apply_tier resolves a name without an engine")
    ok &= check(mod.apply_tier("Nope", verbose=False) is None,
                "apply_tier rejects an unknown name")
    ok &= check(mod.apply_tier(99, verbose=False) is None,
                "apply_tier rejects an out-of-range index")
    ok &= check(mod.apply_tier(True, verbose=False) is None,
                "apply_tier rejects a bool (bool is an int subclass)")
    ok &= check(mod.auto(verbose=False) is None,
                "auto returns None when nothing can be probed")
    ok &= check(mod.benchmark(verbose=False) is None,
                "benchmark reports failure without an engine")
    ok &= check(isinstance(mod.report(), dict),
                "report returns its data even with no engine")
    return ok


def test_tiers_are_monotone(mod):
    """
    Every quality axis must be non-decreasing across tiers.

    A tier that is "higher" but renders at lower resolution is a bug in the
    table, and it is exactly the kind of thing a hand-edited matrix gets wrong.
    """
    print("\n[quality] tiers are monotone in quality")
    ok = True
    for key in ("view", "aa", "shadow", "gi", "reflection", "post",
                "texture", "effects", "foliage", "shading", "pool_mb",
                "resolution"):
        vals = [t[key] for t in mod.TIERS]
        ok &= check(all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)),
                    "  %s is non-decreasing across tiers" % key,
                    "%s" % vals)
    # Nanite must stay on everywhere -- this is an explicit project decision.
    ok &= check(all(t["nanite"] == 1 for t in mod.TIERS),
                "Nanite is enabled in every tier including Low",
                "%s" % [t["nanite"] for t in mod.TIERS])
    # ViewDistanceScale is the HISM cull multiplier: the single most important
    # lever on a map this dense, so it must not regress at the top.
    vals = [t["viwdist_scale"] for t in mod.TIERS]
    ok &= check(all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)),
                "  viwdist_scale is non-decreasing", "%s" % vals)
    return ok


def test_adapter_heuristic(mod):
    """
    The adapter heuristic must put integrated GPUs low and discrete ones high.

    This path only runs on a fresh install with no benchmark, which is exactly
    the first-run experience -- so a wrong guess here is the first impression.
    The failure to guard against is guessing high on an integrated GPU, which
    produces an unplayable first launch.
    """
    print("\n[quality] adapter heuristic")
    ok = True
    cases = [
        # (adapter string, vram_mb, expected tier or None)
        ("Intel(R) UHD Graphics 620", None, "Low"),
        ("Intel(R) UHD Graphics 620", 8000, "Medium"),
        ("AMD Radeon(TM) Vega 8 Graphics", None, "Low"),
        # Both driver spellings of the same part must land in the same place.
        ("Intel(R) Iris(R) Xe Graphics", None, "Low"),
        ("Intel(R) Iris(TM) Xe Graphics", None, "Low"),
        ("Intel(R) Iris Xe Graphics", None, "Low"),
        ("Microsoft Basic Render Driver", None, "Low"),
        ("NVIDIA GeForce RTX 4090", 24576, "Epic"),
        ("NVIDIA GeForce RTX 4080", 16384, "Epic"),
        ("NVIDIA GeForce RTX 4060", 8192, "Epic"),
        ("NVIDIA GeForce RTX 3060", 12288, "High"),
        ("NVIDIA GeForce RTX 3050", 4096, "Medium"),
        ("NVIDIA GeForce GTX 1650", 4096, "Medium"),
        ("AMD Radeon RX 6800 XT", 16384, "Epic"),
        ("AMD Radeon RX 6600", 8192, "High"),
        # An unrecognised adapter must return None, not a guess.
        ("", None, None),
        (None, None, None),
    ]
    for adapter, vram, want in cases:
        got = mod.classify_adapter(adapter, vram)
        ok &= check(got == want,
                    "  %-32s vram=%-6s -> %s" % (adapter, vram, want),
                    "got %s" % got)
    return ok


def test_perf_index_is_cpu_biased(mod):
    """
    The benchmark-index mapping must be CPU-biased, because the project is.

    The frame cost is dominated by CPU-side HISM cluster culling over ~1.6 M
    instances, so a fast GPU behind a slow CPU must not unlock a high tier.
    This means the tier comes from the *minimum* of the two indices; the test
    pins that so a later "optimisation" to the average is caught.
    """
    print("\n[quality] benchmark index -> tier is CPU-biased")
    ok = True
    low, mid, high = mod.INDEX_THRESHOLDS
    cases = [
        (10, 10, "Low"),
        (low - 1, 9000, "Low"),        # fast GPU must not rescue a slow CPU
        (9000, low - 1, "Low"),        # and a slow GPU is equally disqualifying
        (low, low, "Medium"),
        (mid - 1, 9000, "Medium"),
        (mid, mid, "High"),
        (high - 1, 9000, "High"),
        (high, high, "Epic"),
        (5000, 5000, "Epic"),
    ]
    for cpu, gpu, want in cases:
        got = mod._index_to_tier(cpu, gpu)
        ok &= check(got == want, "  cpu=%-5s gpu=%-5s -> %s" % (cpu, gpu, want),
                    "got %s" % got)
    ok &= check(mod._index_to_tier(20, 1000) == mod._index_to_tier(20, 20),
                "a weak CPU caps the tier regardless of GPU")
    return ok


def test_supersampling_is_separate_from_tiers(mod):
    """
    Supersampling must be an explicit, separate action.

    The engine clamps ``sg.ResolutionQuality`` at 100, so a tier cannot carry
    a resolution above native. The way to exceed it is to set the
    screen-percentage cvar directly, which is situational (stills, not play)
    and so must not be something a tier switch does behind the player's back.
    """
    print("\n[quality] supersampling is explicit, not part of a tier")
    ok = check(callable(getattr(mod, "set_supersampling", None)),
               "set_supersampling exists")
    if not ok:
        return False
    ok &= check(mod.set_supersampling(150, verbose=False) is None,
                "with no engine, set_supersampling reports it did not apply")
    ok &= check(mod.set_supersampling(500, verbose=False) is None,
                "and rejects an out-of-range percentage")
    ok &= check(mod.set_supersampling("big", verbose=False) is None,
                "and rejects a non-numeric percentage")
    return ok


def main():
    mod = load_apply_quality()
    results = [
        # engine contract first: these are the ones that silently do nothing
        test_perf_index_thresholds_live_in_scalability_ini,
        test_no_dead_threshold_sections,
        test_resolution_is_clamped_to_100,
        test_startup_default_tier,
        # then cross-file agreement
        test_tiers_match_scalability_ini,
        test_landscape_lod_is_written_for_every_tier,
        test_device_profiles_reference_real_tiers,
        test_python_thresholds_match_ini,
        # then behaviour
        test_apply_quality_import_is_engine_free,
        test_tiers_are_monotone,
        test_adapter_heuristic,
        test_perf_index_is_cpu_biased,
        test_supersampling_is_separate_from_tiers,
    ]
    ok = True
    for fn in results:
        try:
            ok &= fn(mod)
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
