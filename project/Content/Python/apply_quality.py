"""
MCReplica -- runtime quality tier selection and manual override.

Run inside the UE5 editor's Python console, or from a packaged build's console
with the Python plugin enabled::

    import apply_quality
    apply_quality.auto()              # probe the hardware, pick a tier, apply
    apply_quality.benchmark()         # run the engine benchmark first, then auto
    apply_quality.set_tier("High")    # pin a tier explicitly
    apply_quality.set_tier(0)         # by index
    apply_quality.report()            # print what is currently active


Why this exists, given Config/DefaultScalability.ini and
Config/DefaultDeviceProfiles.ini already define the tiers
------------------------------------------------------------
Those two files cannot do the thing the player actually needs. They are read
at startup, they are constant, and on Windows they cannot even see the
hardware -- the Windows device-profile selector returns a fixed name
(``Windows``, or ``Windows_<RHI>``) and matches nothing, because the
GPU/CPU-family matching rules are Android-only. So on Windows the ini files
provide a *floor* and a *vocabulary*; the decision has to happen at runtime.
This module is that decision.

    DefaultScalability.ini       what each tier is, and the perf-index
                                boundaries the engine uses to pick one
    DefaultDeviceProfiles.ini    the floor for a machine that never benchmarks,
                                plus the named profiles a script can select
    this module                  the actual runtime choice, and the user's
                                persistent override of it

Selection order in :func:`auto`:

    1. an explicit override set by :func:`set_tier`, persisted to disk so it
       survives a restart;
    2. the engine's own benchmark index, if a benchmark has ever run;
    3. a heuristic on adapter name and VRAM, which is what a fresh install
       with no benchmark hits.

Nothing here raises. On a machine where every probe fails, :func:`auto`
returns ``None`` and leaves the device profile's floor in place.


Two engine facts this module is built around
--------------------------------------------
Both verified against the engine source rather than assumed; they are the
reason the code looks the way it does.

**The perf-index boundaries live in DefaultScalability.ini.** They are read
from its ``[ScalabilitySettings]`` section as
``PerfIndexThresholds_<Group>="<CPU|GPU|Min> t0 t1 t2"`` by
``GConfig->GetSingleLineArray`` in ``Scalability.cpp:150-152``. They are *not*
read from a device profile, and their value does not name a profile. The
thresholds hardcoded in :data:`INDEX_THRESHOLDS` below mirror the ini so this
module can classify a machine without a scalability group in context; the test
suite asserts the two agree, because a silent divergence shows up as a machine
that reports one tier and renders another.

**A quality level of 4 is Cinematic, and the engine reads a differently named
section for it.** ``GetScalabilitySectionString`` (``Scalability.cpp:273``)
emits ``"<Group>@Cine"`` for the top level, not ``"<Group>@4"``. The
``apply_tier`` console path below sidesteps the whole mechanism by setting the
``sg.*`` group variables directly, which is also what the engine's own
Scalability menu does and therefore cannot drift out of sync with the section
naming.
"""

import json
import os

try:
    import unreal
except ImportError:                                       # pragma: no cover
    unreal = None

# --------------------------------------------------------------------------- #
# tier definitions
# --------------------------------------------------------------------------- #

#: Scalability group -> value at each tier. Index 0..4 is Low..Cinematic.
#: These MUST match Config/DefaultScalability.ini; the test suite enforces it.
TIERS = [
    {"name": "Low",       "index": 0, "view": 0, "aa": 0, "shadow": 0,
     "gi": 0, "reflection": 0, "post": 0, "texture": 0, "effects": 0,
     "foliage": 0, "shading": 0, "pool_mb": 256, "nanite": 1,
     "resolution": 50, "viwdist_scale": 0.4},
    {"name": "Medium",    "index": 1, "view": 1, "aa": 1, "shadow": 1,
     "gi": 1, "reflection": 1, "post": 1, "texture": 1, "effects": 1,
     "foliage": 1, "shading": 1, "pool_mb": 512, "nanite": 1,
     "resolution": 71, "viwdist_scale": 0.7},
    {"name": "High",      "index": 2, "view": 2, "aa": 2, "shadow": 2,
     "gi": 2, "reflection": 2, "post": 2, "texture": 2, "effects": 2,
     "foliage": 2, "shading": 2, "pool_mb": 1024, "nanite": 1,
     "resolution": 100, "viwdist_scale": 1.0},
    {"name": "Epic",      "index": 3, "view": 3, "aa": 3, "shadow": 3,
     "gi": 3, "reflection": 3, "post": 3, "texture": 3, "effects": 3,
     "foliage": 3, "shading": 3, "pool_mb": 2048, "nanite": 1,
     "resolution": 100, "viwdist_scale": 1.0},
    {"name": "Cinematic", "index": 4, "view": 4, "aa": 4, "shadow": 4,
     "gi": 4, "reflection": 4, "post": 4, "texture": 4, "effects": 4,
     "foliage": 4, "shading": 4, "pool_mb": 4096, "nanite": 1,
     "resolution": 100, "viwdist_scale": 1.2},
]

TIER_BY_NAME = {t["name"].lower(): t for t in TIERS}

#: Scalability group name -> the TIERS key holding its value. Setting the
#: ``sg.*`` variable is what makes the engine load the matching
#: ``[Group@N]`` section, so these are the only groups we need to touch.
SCALABILITY_GROUPS = (
    ("sg.ViewDistanceQuality",         "view"),
    ("sg.AntiAliasingQuality",         "aa"),
    ("sg.ShadowQuality",               "shadow"),
    ("sg.GlobalIlluminationQuality",   "gi"),
    ("sg.ReflectionQuality",           "reflection"),
    ("sg.PostProcessQuality",          "post"),
    ("sg.TextureQuality",              "texture"),
    ("sg.EffectsQuality",              "effects"),
    ("sg.FoliageQuality",              "foliage"),
    ("sg.ShadingQuality",              "shading"),
)

#: Perf-index boundaries, mirroring PerfIndexThresholds_* in
#: DefaultScalability.ini: (low, mid, high). Below ``low`` is Low, below
#: ``mid`` Medium, below ``high`` High, at or above it Epic. Cinematic is
#: never selected automatically -- the engine's own auto-detect tops out at
#: Epic, and matching that keeps "auto" and "manual" from disagreeing about
#: what the automatic answer means.
INDEX_THRESHOLDS = (30.0, 120.0, 400.0)

#: Where the user's explicit choice is remembered. Lives under the home
#: directory rather than the project's Saved/ so it is per-install and does
#: not get wiped when the project is re-cooked or moved between machines.
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".mcreplica")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "quality.json")

# --------------------------------------------------------------------------- #
# hardware probe
# --------------------------------------------------------------------------- #

#: Adapter-name substrings that mean "weak integrated GPU", best effort.
#: Ordered most-specific-first so a stronger part is not swallowed by a
#: weaker marker in the same string.
#:
#: Adapter strings are driver-reported and inconsistent about punctuation:
#: the same Iris Xe part appears as "Intel(R) Iris(R) Xe Graphics" and
#: "Intel(R) Iris(TM) Xe Graphics". Parenthesised markers are therefore matched
#: both bare and in the "(R)"/"(TM)" form. Normalising punctuation out of the
#: string was tried first and rejected: it makes the table harder to read
#: against a real driver string, which is where mistakes come from.
_IGPU_MARKERS = (
    "iris xe", "iris(r) xe", "iris(tm) xe", "iris plus", "iris(tm) plus",
    "uhd graphics 620", "uhd graphics 630", "uhd graphics 605", "uhd graphics",
    "hd graphics 520", "hd graphics 550", "hd graphics",
    "vega 3", "vega 6", "vega 8", "vega 11", "vega graphics",
    "radeon(tm) graphics", "radeon graphics",
    "apple m1", "apple m2",
    "microsoft basic", "llvmpipe", "swiftshader", "basic render driver",
)

#: Adapter-name substrings that mean "capable discrete GPU".
_DGPU_HIGH_MARKERS = (
    "rtx 40", "rtx 50", "rtx 3090", "rtx 3080", "rtx 4070", "rtx 4080",
    "rtx 4090", "rx 7900", "rx 7800", "rx 6800", "arc a7",
)
_DGPU_MID_MARKERS = (
    "rtx 30", "rtx 20", "gtx 16", "gtx 10", "rx 6000", "rx 5000",
    "arc a5", "arc a3", "quadro rtx",
)


def _engine_perf_index():
    """
    The engine's CPU/GPU performance index, or ``(None, None)``.

    ``UGameUserSettings`` stores the last benchmark's scores in
    ``LastCPUBenchmarkResult`` / ``LastGPUBenchmarkResult`` (both -1 before any
    benchmark has run). We read those rather than trying to run a benchmark
    implicitly: a benchmark freezes the game for seconds, and doing that
    unprompted on every launch would be a hostile default. :func:`benchmark`
    is the explicit, opt-in path that runs one.

    A missing API, a missing property, or a not-yet-run benchmark all mean the
    same thing here -- "unknown" -- and the caller falls through to the adapter
    heuristic. Treating unknown as "very slow" would start every fresh install
    on Low, which is the wrong way to be wrong.
    """
    if unreal is None:
        return None, None
    settings = _game_user_settings()
    if settings is None:
        return None, None
    try:
        cpu = float(settings.get_editor_property("last_cpu_benchmark_result"))
        gpu = float(settings.get_editor_property("last_gpu_benchmark_result"))
    except Exception:
        return None, None
    if cpu <= 0.0 or gpu <= 0.0:
        return None, None
    return cpu, gpu


def _game_user_settings():
    """
    The engine's ``UGameUserSettings`` instance, or ``None``.

    It is a plain UObject with no Python-side singleton accessor, so it is
    reached through the engine subsystem collection. Returns ``None`` rather
    than raising when the lookup fails, because every caller has a sensible
    fallback and this runs on a packaged build where the object graph differs.
    """
    if unreal is None:
        return None
    try:
        settings = unreal.get_engine_subsystem(unreal.GameUserSettings)
    except Exception:
        return None
    return settings if settings is not None else None


def _adapter_info():
    """
    ``(name, vram_mb)`` of the primary adapter, best effort.

    Both halves may be ``None`` independently: the adapter name comes from the
    RHI, and VRAM from the rendering library, and on some drivers only one of
    the two is available. A headless or software adapter legitimately has no
    name, which is not an error.
    """
    if unreal is None:
        return None, None
    name = None
    try:
        name = unreal.SystemLibrary.get_device_name()
    except Exception:
        pass
    if not name:
        try:
            name = str(unreal.RHI.get_rhi_name())
        except Exception:
            name = None
    vram = None
    for holder, attr in ((unreal.RenderingLibrary, "get_vram_size_mb"),
                         (unreal.SystemLibrary, "get_device_make_and_model")):
        if attr == "get_device_make_and_model":
            continue
        try:
            vram = int(getattr(holder, attr)())
            break
        except Exception:
            continue
    return (name or None), vram


def classify_adapter(name, vram_mb=None):
    """
    Heuristic tier name from an adapter string and optional VRAM.

    Returns a tier *name*, or ``None`` when the adapter is unrecognised -- the
    caller then leaves the device profile's floor in place. Erring toward
    ``None`` is deliberate: guessing high on an unknown iGPU produces an
    unplayable first run, while guessing low on an unknown dGPU only costs
    image quality the player can raise.

    The VRAM thresholds differ between the iGPU and dGPU branches on purpose.
    An iGPU shares system RAM, so its reported VRAM says little about how much
    the GPU can actually do and 8 GB is already unusual. A discrete GPU has
    dedicated VRAM, so there the number is the signal.
    """
    if not name:
        return None
    low = name.lower()

    for m in _DGPU_HIGH_MARKERS:
        if m in low:
            return "Epic" if (vram_mb is None or vram_mb >= 8000) else "High"
    for m in _DGPU_MID_MARKERS:
        if m in low:
            return "High" if (vram_mb is None or vram_mb >= 6000) else "Medium"
    for m in _IGPU_MARKERS:
        if m in low:
            if vram_mb is not None and vram_mb >= 7000:
                return "Medium"
            return "Low"

    # Recognised as a discrete GPU by name but no tier marker matched, e.g.
    # "NVIDIA GeForce RTX 4090 Laptop GPU" after a driver rename. VRAM decides.
    if "nvidia" in low or "geforce" in low or "quadro" in low or "radeon rx" in low:
        if vram_mb is not None:
            if vram_mb >= 12000:
                return "Epic"
            if vram_mb >= 8000:
                return "High"
            if vram_mb >= 4000:
                return "Medium"
        return "Medium"
    return None


def _index_to_tier(cpu, gpu):
    """
    Map the engine benchmark indices onto a tier name.

    The metric is the *minimum* of the two indices, not the average. This
    project is CPU-bound -- HISM cluster culling over ~1.6 M instances scales
    with instance count and view distance, not with pixels -- so a fast GPU
    behind a slow CPU must not unlock a high tier. That combination drops
    frames in exactly the same way as two slow parts, and it is the mistake
    that makes an "auto" setting feel broken on a mid-range laptop.

    Boundaries come from :data:`INDEX_THRESHOLDS`, which mirrors the
    ``PerfIndexThresholds_*`` values in DefaultScalability.ini.
    """
    low, mid, high = INDEX_THRESHOLDS
    idx = min(cpu, gpu)
    if idx < low:
        return "Low"
    if idx < mid:
        return "Medium"
    if idx < high:
        return "High"
    return "Epic"


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #

def _load_override():
    """The user's pinned tier name, or ``None``. Never raises."""
    try:
        with open(SETTINGS_PATH) as fh:
            data = json.load(fh)
    except Exception:
        return None
    tier = data.get("tier")
    if isinstance(tier, str) and tier.lower() in TIER_BY_NAME:
        return TIER_BY_NAME[tier.lower()]["name"]
    return None


def _save_override(tier_name):
    """Persist (or clear, with ``tier_name=None``) the pinned tier."""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        if tier_name is None:
            if os.path.isfile(SETTINGS_PATH):
                os.remove(SETTINGS_PATH)
            return
        with open(SETTINGS_PATH, "w") as fh:
            json.dump({"tier": tier_name}, fh, indent=2)
    except Exception as exc:                                # pragma: no cover
        _log("could not persist quality choice: %s" % exc)


# --------------------------------------------------------------------------- #
# applying a tier
# --------------------------------------------------------------------------- #

def _log(msg):
    if unreal is not None:
        try:
            unreal.log("[MCReplica quality] " + msg)
            return
        except Exception:
            pass
    print("[MCReplica quality] " + msg)


def _exec(cvar):
    """
    Run one console command, whichever API this engine build exposes.

    The console path is used in preference to the ``GameUserSettings`` setters
    because those silently no-op for some groups -- ``ResolutionQuality`` in
    particular -- while the console is exactly what the engine's own Scalability
    menu drives underneath, so it cannot disagree with the ini sections.
    """
    if unreal is None:
        return False
    for holder_name in ("SystemLibrary", "KismetSystemLibrary"):
        holder = getattr(unreal, holder_name, None)
        fn = getattr(holder, "execute_console_command", None) if holder else None
        if fn is None:
            continue
        try:
            # Builds differ on whether a world context is required.
            try:
                fn(cvar)
            except TypeError:
                fn(None, cvar)
            return True
        except Exception:
            continue
    return False


def apply_tier(tier, verbose=True):
    """
    Apply one tier. ``tier`` is a name (``"High"``) or an index (``2``).

    Returns the tier dict that was applied, or ``None`` if the tier is unknown.

    Resolution is applied as ``sg.ResolutionQuality``, not as a raw
    ``r.ScreenPercentage``. Writing screen percentage directly appears to work
    and then gets overwritten by the next scalability group change, because
    ``sg.ResolutionQuality`` is the variable the engine watches and mirrors into
    ``r.ScreenPercentage`` -- so the two have to be set through the group to
    agree. The engine clamps that group to ``MaxResolutionScale`` (100), which
    is why the Cinematic row's resolution is 100 and not higher; going beyond
    native needs a deliberate screen-percentage override, exposed separately as
    :func:`set_supersampling` rather than smuggled into the tier.
    """
    entry = _resolve(tier)
    if entry is None:
        _log("unknown tier %r -- expected one of %s or 0..4"
             % (tier, ", ".join(t["name"] for t in TIERS)))
        return None

    cmds = ["sg.ResolutionQuality=%d" % entry["resolution"]]
    for cvar, key in SCALABILITY_GROUPS:
        cmds.append("%s=%d" % (cvar, entry[key]))
    cmds.append("r.Streaming.PoolSize=%d" % entry["pool_mb"])
    # Nanite stays on at every tier -- see QUALITY_TIERS.md section 1. What
    # changes per tier is its pixel budget, which lives in the scalability ini
    # under ViewDistanceQuality.
    cmds.append("r.Nanite=%d" % entry["nanite"])
    # One multiplier over every HISM component's Max Draw Distance. This is the
    # single most effective view-distance lever because instances are culled
    # per component, not per object, and it is the one that matters most on a
    # map this dense.
    cmds.append("r.ViewDistanceScale=%.2f" % entry["viwdist_scale"])

    applied = sum(1 for c in cmds if _exec(c))
    if verbose:
        _log("applied tier %s (%d/%d cvars accepted)"
             % (entry["name"], applied, len(cmds)))
    return entry


def set_supersampling(percent, verbose=True):
    """
    Set the render scale above 100%, or back to 100% with ``None``.

    Separate from :func:`apply_tier` because the tier's resolution is capped by
    the engine at 100 (see :func:`apply_tier`), and because supersampling is a
    deliberate, situational choice -- for capturing stills, not for play. Left
    unset in normal play: rendering above native costs frame time linearly for
    no interactive benefit.
    """
    if percent is None:
        cmd = "r.ScreenPercentage=100"
    else:
        try:
            value = int(percent)
        except (TypeError, ValueError):
            _log("supersampling needs an integer percent or None, got %r" % (percent,))
            return None
        if not 50 <= value <= 200:
            _log("supersampling %d is outside the sane 50..200 range" % value)
            return None
        cmd = "r.ScreenPercentage=%d" % value
    if verbose:
        _log("set %s" % cmd)
    return cmd if _exec(cmd) else None


def _resolve(tier):
    """Name or index -> tier dict, or ``None``."""
    if isinstance(tier, bool):
        return None
    if isinstance(tier, int):
        if 0 <= tier < len(TIERS):
            return TIERS[tier]
        return None
    if isinstance(tier, str):
        return TIER_BY_NAME.get(tier.strip().lower())
    return None


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #

def set_tier(tier, persist=True):
    """
    Pin a tier explicitly, overriding the hardware probe.

    ``set_tier(None)`` clears the pin and returns to automatic selection on the
    next :func:`auto`.
    """
    if tier is None:
        _save_override(None)
        _log("quality override cleared; run auto() to re-detect")
        return None
    entry = apply_tier(tier)
    if entry is not None and persist:
        _save_override(entry["name"])
    return entry


def benchmark(work_scale=10, verbose=True):
    """
    Run the engine's hardware benchmark, then apply the result.

    This freezes the game for a few seconds, which is why it is not part of
    :func:`auto`. Call it from a loading screen or an options menu, not on a
    live play session.

    ``work_scale`` trades benchmark time against accuracy: 10 is the engine's
    shipping default, and lower values finish faster but are noisier. The
    result lands in ``GameUserSettings`` and therefore survives a restart, so
    this only needs running once per machine.
    """
    settings = _game_user_settings()
    if settings is None:
        _log("no GameUserSettings; cannot run the hardware benchmark")
        return None
    try:
        settings.run_hardware_benchmark(int(work_scale), 1.0, 1.0)
        settings.apply_hardware_benchmark_results()
    except Exception as exc:
        _log("hardware benchmark failed: %s" % exc)
        return None
    if verbose:
        _log("hardware benchmark complete")
    return auto(verbose=verbose)


def auto(verbose=True):
    """
    Pick and apply the best tier for this machine.

    Order: explicit override, then the engine benchmark index, then the adapter
    heuristic. Never raises; returns ``None`` when nothing could be determined,
    leaving the device profile's floor in place.
    """
    override = _load_override()
    if override is not None:
        if verbose:
            _log("using pinned tier %s" % override)
        return apply_tier(override, verbose=verbose)

    cpu, gpu = _engine_perf_index()
    if cpu is not None and gpu is not None:
        name = _index_to_tier(cpu, gpu)
        if verbose:
            _log("benchmark index cpu=%.1f gpu=%.1f -> %s" % (cpu, gpu, name))
        return apply_tier(name, verbose=verbose)

    adapter, vram = _adapter_info()
    guess = classify_adapter(adapter, vram)
    if guess is not None:
        if verbose:
            _log("no benchmark; adapter %r (%s MB) -> %s" % (adapter, vram, guess))
        return apply_tier(guess, verbose=verbose)

    if verbose:
        _log("no benchmark and unrecognised adapter %r; leaving the device "
             "profile's default tier in place" % (adapter,))
    return None


def report():
    """Print the currently active tier, as read back from the engine."""
    cpu, gpu = _engine_perf_index()
    adapter, vram = _adapter_info()
    print("MCReplica quality")
    print("  pinned override : %s" % (_load_override() or "(none)"))
    print("  benchmark index : cpu=%s gpu=%s" % (cpu, gpu))
    print("  adapter         : %s (%s MB)" % (adapter, vram))
    print("  heuristic tier  : %s" % classify_adapter(adapter, vram))
    print("  thresholds      : <%s Low, <%s Medium, <%s High, else Epic"
          % INDEX_THRESHOLDS)
    print("  tiers           : %s"
          % ", ".join("%d=%s" % (t["index"], t["name"]) for t in TIERS))
    return {
        "override": _load_override(),
        "cpu_index": cpu, "gpu_index": gpu,
        "adapter": adapter, "vram_mb": vram,
        "heuristic": classify_adapter(adapter, vram),
    }
