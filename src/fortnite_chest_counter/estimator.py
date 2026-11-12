"""
Streaming chest estimator: consumes frames one at a time (no future frames, no rescans),
runs the existing clue detectors, merges their evidence into ONE event per chest, and
says how each event should be counted.

Frame source  ->  ChestEstimator.process_frame(t, frame)  ->  finished events
(MP4 today; a capture card / OBS source later: anything yielding (seconds, 720p RGB frame).)

Clues used (all existing detectors, parameters untouched):
  popup        word CHEST / BAU in the XP popup           (visual_detector.py)
               + scale-aware channel: CHEST drawn larger (bigger HUD), blurred match at smaller scales - APPROX only
               + small-HUD channel: CHEST drawn smaller, a native small template - APPROX, or CERTAIN when both of its matches are strong
               + stacked pop-ups: chests ADDED to a pop-up on screen (new line, X2, X3) - popup_lines.py
  sprite       "+75 CONTAINER OPENED" tag                  (sprite_detector.py)
  ammo         ammo-counter jumps = the local player fires (evidence.py)
  prompt       interaction-button icon + chest-object colours around it (evidence.py / object_cue.py)
NOT in the live path yet: the audio detector (it needs a streaming audio buffer and was the
weakest clue; it never gated anything).

An event is FINISHED a little after it starts, because its evidence needs a short look-ahead:
  popup events   1.0 s after the popup appears (peak score, ammo steps, sprite tag)
  prompt events  5.6 s after the prompt (waiting to see whether a popup merges with it)

Counting rules (see the constants below; all explicit and configurable):
  CERTAIN count      popup events whose adjusted tier is CERTAIN or HIGH.
  APPROX count       CERTAIN + HIGH, plus
                     - MEDIUM popups of languages whose word is long enough to be reliable
                       (config), plus
                     - MEDIUM popups backed by a prompt + chest-object, plus
                     - prompt + chest-object events whose popup never fired, plus
                     - scale-aware / small-HUD pop-ups (exact match too low): MEDIUM, APPROX only (a small-HUD pop-up whose
                       small-word matches reach SMALL_HIGH_* is HIGH, so CERTAIN), plus
                     - stacked steps: a chest line added (normal tier rules) or a multiplier step (APPROX only).
  never counted      LOW (e.g. MEDIUM while the player is shooting), generic prompts
                     (pickups, ammo crates, cannons...), anything else. They are still logged.
"""

import time
from collections import Counter, deque

import numpy as np
from scipy.ndimage import gaussian_filter

from fortnite_chest_counter import paths
from fortnite_chest_counter.detection import chest_types
from fortnite_chest_counter.detection import evidence
from fortnite_chest_counter.detection import fusion
from fortnite_chest_counter.detection import object_cue
from fortnite_chest_counter.detection import popup_lines
from fortnite_chest_counter.detection import sprite as sprite_detector
from fortnite_chest_counter.detection import visual as V

# ==================================================================
# COUNTING RULES (explicit; change here, not in the code)
# ==================================================================

CERTAIN_TIERS = ("CERTAIN", "HIGH")

# MEDIUM popups counted as APPROX even without other support, per popup template.
# English CHEST (27 px word): every popup >= 0.75 on the English clip was a real chest (6/6).
# Portuguese BAU (14 px word): only 2 of 9 MEDIUM popups were real chests on the PS5 clip.
APPROX_COUNTS_MEDIUM_ALONE = {"en:CHEST": True, "pt:BAÚ": False, "fr:COFFRE": True}

# A MEDIUM popup that also has a prompt >= INTERACTION_SUPPORT_PROMPT_MIN with a chest-like
# object near it counts as APPROX. PROVISIONAL: this 0.60 level was fixed after seeing the
# prompt scores of the verified PS5 events, so treat it as unvalidated.
APPROX_MEDIUM_WITH_OBJECT_SUPPORT = True

# "Shooting" (ammo-counter steps around the popup) demotes an ambiguous MEDIUM popup to LOW. That is right for SHORT
# chest words: on the PS5 clip the 14 px BAU template matched combat-time lavender text at 0.77-0.82 and all 3 shooting
# demotions removed verified-false popups. It is wrong for a SPECIFIC word: across ~25 min of footage (both clips,
# including heavy combat) the 27 px CHEST template never scored above 0.65 on anything that was not a chest (max 0.37 on
# the PS5 combat popups), while live users saw real English chests opened mid-fight demoted to LOW and lost.
# So shooting only demotes popups whose word is NOT reliable on its own (APPROX_COUNTS_MEDIUM_ALONE). v0.1.0 = True.
SHOOTING_DEMOTES_RELIABLE_WORDS = False

# Compression-robust confirmation. Streaming compression smears the 7 px-high popup word by a fraction of a pixel, so the
# exact binary match drops (real English chests: 0.78-1.00 clean -> 0.77-0.88 at a 2.5 Mbps 1080p encode = MEDIUM).
# Correlating the Gaussian-blurred mask with the equally blurred template (sigma 0.6 px) is insensitive to that smear:
# real English chests scored >= 0.92 in EVERY condition (clean, 6 Mbps, 2.5 Mbps) while nothing that was not a chest
# scored above 0.78 with the English template (25 min of footage incl. Portuguese combat popups); Portuguese: real
# >= 0.91 under compression, verified-false popups <= 0.83. This score never creates or removes a popup and never
# lowers a tier: a MEDIUM popup whose blurred match is >= ROBUST_HIGH_MIN is promoted to HIGH (= CERTAIN by default).
ROBUST_PROMOTION = True
ROBUST_SIGMA = 0.6
ROBUST_HIGH_MIN = 0.88
ROBUST_SEARCH_PX = 8          # the blurred match is searched within this many px of the exact match

# Scale-aware pop-ups (docs/reference/INVESTIGATION_2026-11-07.md). Some streams draw the HUD ~1.2x larger, so the pop-up word is
# ~33 px wide instead of 27 px: the exact match (one fixed size) stays near 0.25 and nearly every chest is missed, while a
# blurred match of the picture made smaller (x0.8) scores 0.82-0.92. This is a second channel BESIDE the exact path:
#   - it runs only when the pop-up box has >= SCALED_PREFILTER_PX lavender pixels (production computes that mask anyway)
#   - it looks only for long chest words (SCALED_POPUP_TEMPLATES) and only shrinks the picture (= larger HUD);
#     enlarging, or the short Portuguese word, matched non-chest text on the PS5 reference
#   - after two strong pop-ups agree on a scale (SCALED_LOCK_MIN), only that scale and its neighbours are searched
#   - a blurred onset becomes a pop-up only when the exact path has not fired within SCALED_WAIT seconds of it, so every
#     exact-path event (incl. back-to-back chests and the CERTAIN rules) is exactly as before
#   - no "position jumped" re-fire (a stacked "WEAKPOINT STREAK / CHEST" pop-up would count twice)
#   - such a pop-up is merged with prompts like any pop-up. French can also earn HIGH after repeated sharp matches.
# Validation: event-identical on every reference clip (English and PS5, clean and compressed); on three large-HUD sessions
# 11 of 11 verified chests (exact path alone: 3), 0 duplicates, 0 wrong counts.
SCALE_AWARE_POPUPS = True
SCALED_POPUP_TEMPLATES = ("en:CHEST", "fr:COFFRE")  # long words can count at MEDIUM without a prompt
PORTUGUESE_POPUP = "pt:BAÚ"
LOCALIZED_SMALL_HUD_SCALE = 1.25
SCALED_SCALES = (1.0, 0.9, 0.85, 0.8, 0.75)  # picture made smaller = HUD drawn larger
WIDE_HUD_SCALE = 0.70
REFERENCE_HUD_SCALE = 0.70  # native size in the calibrated 720p desktop capture
SCALED_TRIGGER = 0.80
FRENCH_SCALED_CONFIRM_FRAMES = 3
FRENCH_TYPE_READ_MIN = 0.72  # type-only evidence for an existing popup; never starts or confirms an opening
SCALED_REARM = 0.55
SCALED_PREFILTER_PX = 60
SCALED_WAIT = 2.0
SCALED_LOCK_MIN = 0.84

# Smaller HUD (docs/reference/INVESTIGATION_2026-11-08.md). Thiefs' stream draws the HUD at ~0.8x: the chest word is 22 px wide, the
# exact match stays at 0.21-0.32 and every chest was missed. Enlarging the picture was rejected: a Portuguese combat
# pop-up reached 0.806 at x1.25 against 0.81-0.83 for the real chests, and it cost ~34 ms per frame. This channel matches
# a CHEST word cut at its NATIVE small size (templates/chest/en/chest_small.npy, 22 x 7 px, from that stream):
#   - candidate: exact match >= SMALL_TRIGGER and the blurred match at that place >= SMALL_CONFIRM (real small-HUD
#     chests: exact 0.82-1.00 / blurred 0.91-1.00; everything else on the reference footage: <= 0.62 / <= 0.87)
#   - the scale-aware channel's rules: English word only, 60 px prefilter, the exact path owns any pop-up it fires on
#     within SCALED_WAIT, counted APPROX (MEDIUM), or CERTAIN when both matches are as strong as below (SMALL_HIGH_*)
#   - no longer searched once the session's HUD scale is known to be normal or larger (scale vote)
# M9H (docs/M9_VALIDATION.md): on a 720p stream the 22 x 7 px word loses its exact shape (chroma at 640 x 360), so real chests scored
# exact 0.59-0.78 and 26 of 34 verified chests were missed with a 0.75 trigger. The blurred match kept them at 0.86-0.94. The trigger is now
# 0.60 and the blurred confirmation stays 0.90: nothing that was not a chest reached 0.90 blurred at its best exact place (normal-HUD
# English and PS5 footage 0.865, Thiefs' non-chest frames 0.86).
SMALL_HUD_POPUPS = True
SMALL_TEMPLATE_FILE = "chest/en/chest_small.npy"
SMALL_SCALE = 1.25                 # its picture-scale equivalent, for the session's HUD-scale vote
SMALL_TRIGGER = 0.60
SMALL_CONFIRM = 0.90
SMALL_REARM = 0.50
# A small-HUD pop-up as trustworthy as a normal one reaches HIGH (= CERTAIN by default), like the normal word at exact >= 0.85. Both matches
# must pass. Non-chests never exceeded 0.62 exact or 0.865 blurred (normal word: 0.65 / 0.78 against 0.85 / 0.88). Real 1080p small-HUD
# chests reached 0.85-0.92 / 0.95-0.98; 720p ones never do, so they stay APPROX.
SMALL_HIGH_EXACT = 0.85
SMALL_HIGH_BLUR = 0.95
# A single pop-up shows for about 2 s, and a chest opened meanwhile is added to it rather than starting another, so separate pop-ups began
# 2.33 s or more apart (M9H). The same word at the same place sooner than this after an onset is that pop-up back after a moment hidden
# (a 0.6 s full-screen map at 362.4 s of the M9H 720p capture), not a new chest.
SMALL_SAME_POPUP_SECONDS = 2.0
SMALL_SAME_POPUP_PIXELS = 4
# Where the small word is searched (normalized frame pixels): the pop-up keeps its place when the HUD is smaller (Thiefs'
# word at x 889-910, y 228-234 against 885-911, 229-235), so a box around it is enough - a third of the pop-up box's work.
SMALL_SEARCH_BOX = (800, 170, 1010, 300)

# Stacked pop-ups (detection/popup_lines.py). While a pop-up is on screen Fortnite ADDS the next chest to it (a new
# "... CHEST" line, or "X2" / "X3" after the word) instead of showing a new pop-up. Before v0.2.2 only a new line could be
# counted, by the exact path's "position jumped" re-fire - which also counted the SAME chest again when a non-chest line
# (SPRITE LEVEL) pushed the word up, or when the scale-in animation moved it by 8 px (live E0015). For the English word
# the pop-up tracker replaces that re-fire: it counts only a real content change (a chest line added, a multiplier
# appearing or changing, each with a change of the XP total). A new chest line keeps the normal tier rules, as the re-fire
# did; a multiplier step and every step of a scale-aware / small-HUD pop-up count APPROX only. The Portuguese word keeps
# the position re-fire (no stacked Portuguese footage yet).
STACKED_POPUPS = True
STACKED_LINE_MIN = 0.75            # chest-line score (blurred) of the normal word; non-chest lines scored <= 0.70
STACKED_LINE_MIN_SMALL = 0.85      # ...of the small word ("SPRITE" scores 0.81 against it)

# Chest types (detection/chest_types.py): every counted chest is labelled with the type of ITS pop-up line - the line of a
# new pop-up (its newest line), the added line, or the line whose multiplier went up - from the per-frame readings while
# the pop-up stays on screen. A label never changes CERTAIN / APPROX. A label that becomes clearer after the chest was
# counted is sent as a type update (take_type_updates) and changes only the type statistics. Chests without a tracked
# line (Portuguese word, larger HUD, prompt-only events) are "unknown".
CHEST_TYPES = True
# An exact-path onset on the pop-up the tracker is already following, after that pop-up was counted (by the scale-aware
# or small channel, or by an earlier exact onset), is the SAME pop-up - not a chest. v0.2.1 counted it: on Replays' stream
# a "+600 XP / CHEST" counted by the scale-aware channel at 14.07 s was counted again when the exact match first passed
# 0.75 at 16.67 s (it only looked right because the pop-up had become "CHEST X2" - which the tracker now counts).

# Prompt + chest-object events whose popup never fired: counted as APPROX only, never CERTAIN.
INTERACTION_ONLY_COUNTS_APPROX = True
INTERACTION_CAN_BE_CERTAIN = False

# ==================================================================
# EVIDENCE PARAMETERS
# ==================================================================

INTERACTION_COUNT_PROMPT_MIN = 0.70      # a prompt this strong starts an interaction event
INTERACTION_SUPPORT_PROMPT_MIN = 0.60    # weaker prompts can still support a MEDIUM popup
INTERACTION_REFRACTORY_SECONDS = 6.0
MERGE_POPUP_BEFORE_PROMPT = 1.0          # popup onset may lead the prompt by this much...
MERGE_POPUP_AFTER_PROMPT = 4.5           # ...or follow it by this much (M001: ~3 s, M004: ~2.7 s)
POPUP_FINISH_DELAY = 1.0
INTERACTION_FINISH_DELAY = MERGE_POPUP_AFTER_PROMPT + POPUP_FINISH_DELAY + 0.1
SPRITE_NEAR_SECONDS = 1.5

SAMPLE_FPS = 15                          # frames per second the estimator expects
PROMPT_EVERY_N_FRAMES = 8                # ~1.9 prompt scores per second (as in the offline analysis)


ENABLED_LANGUAGES = {"en", "pt"}

# Keep a copy of the popup region at each popup's peak so the session can save it as a PNG ([logging] save_snapshots).
SAVE_SNAPSHOTS = False


def configure(detector_cfg):
    """Apply the user's [detector] settings (settings.py) to the counting rules above."""
    global CERTAIN_TIERS, APPROX_COUNTS_MEDIUM_ALONE, APPROX_MEDIUM_WITH_OBJECT_SUPPORT
    global INTERACTION_ONLY_COUNTS_APPROX, ENABLED_LANGUAGES, SAMPLE_FPS, PROMPT_EVERY_N_FRAMES
    CERTAIN_TIERS = ("CERTAIN",) if detector_cfg["certain_min_tier"] == "CERTAIN" else ("CERTAIN", "HIGH")
    alone = set(detector_cfg["medium_counts_alone_languages"])
    APPROX_COUNTS_MEDIUM_ALONE = {t["id"]: t["id"].split(":")[0] in alone for t in V.CHEST_TEMPLATES}
    APPROX_MEDIUM_WITH_OBJECT_SUPPORT = detector_cfg["count_supported_medium_as_approx"]
    INTERACTION_ONLY_COUNTS_APPROX = detector_cfg["count_interaction_object_as_approx"]
    ENABLED_LANGUAGES = set(detector_cfg["languages"])
    SAMPLE_FPS = detector_cfg["process_fps"]
    PROMPT_EVERY_N_FRAMES = max(1, round(SAMPLE_FPS / 1.9))      # keep ~1.9 prompt scores per second


class OnsetDetector:
    """The offline hysteresis event detector (visual_detector.detect_events), one sample at a time."""

    def __init__(self, high, low, use_position):
        self.high, self.low, self.use_position = high, low, use_position
        self.events, self.armed, self.low_since, self.last_pos = [], True, None, None

    def update(self, t, score, y=0, x=0, allow_jump=True):
        fired = False
        spaced = not self.events or t - self.events[-1] >= V.EVENT_MIN_SEPARATION_SECONDS
        if score >= self.high:
            jumped = (self.use_position and allow_jump and self.last_pos is not None
                      and max(abs(y - self.last_pos[0]), abs(x - self.last_pos[1])) >= V.EVENT_POSITION_JUMP_PIXELS)
            if spaced and (self.armed or jumped):
                self.events.append(t)
                self.armed, fired = False, True
            self.last_pos = (y, x)
            self.low_since = None
        elif score < self.low:
            self.low_since = t if self.low_since is None else self.low_since
            if not self.armed and t - self.low_since >= V.EVENT_REARM_HOLD_SECONDS:
                self.armed, self.last_pos = True, None
        else:
            self.low_since = None
        return fired


def adjust_tier(base_tier, shooting, interaction):
    """Same rule as refine.adjust: only MEDIUM can move; CERTAIN/HIGH are protected."""
    if base_tier != "MEDIUM":
        return base_tier
    if shooting and not interaction:
        return "LOW"
    if interaction and not shooting:
        return "HIGH"
    return "MEDIUM"


class ChestEstimator:
    def __init__(self, scale_aware=None, small_hud=None, stacked=None, hud_scale=None):
        """scale_aware / small_hud / stacked: None = the module constants; False / True to compare (tests, research)."""
        self.templates = [t for t in V.load_templates() if t["id"].split(":")[0] in ENABLED_LANGUAGES]
        if not self.templates:
            raise ValueError(f"no chest-word template for languages {sorted(ENABLED_LANGUAGES)}; "
                             f"available: {[t['id'] for t in V.CHEST_TEMPLATES]}")
        self.robust_exact_templates = {tpl["id"]: tpl["array"] for tpl in self.templates}
        self.robust_templates = {t["id"]: V.blur_template(t["array"], ROBUST_SIGMA) for t in self.templates}
        self.french_template = next((t["array"] for t in self.templates if t["id"] == "fr:COFFRE"), None)
        self.french_suffixes = chest_types.load_french_suffixes() if self.french_template is not None else {}
        self.french_reads = deque()
        self.french_faint_reads = {}
        self.french_saturated_reads = deque()
        self.french_bright_reads = deque()
        self.french_exp_reads = deque()
        self.french_stacked_exp_reads = deque()
        self.french_exp_candidates = deque()
        self.recent_counted_popups = deque()
        self.last_french_exp = -float("inf")
        self.french_type_history = deque()
        self.french_contrast_history = deque()
        self.french_scaled_reads = {}       # no votes shared between different glyph scales
        self.french_plane = None           # this frame's winning (scale, RGB ROI, mask)
        self.french_xp_templates = {}
        if self.french_template is not None:
            # Each pair is the EXP line and chest line, plus the word's column within the template.
            self.french_xp_templates = {
                kind: (np.load(paths.TEMPLATES_DIR / "chest/fr" / file), dx)
                for kind, file, dx in ((chest_types.NORMAL, "normal_xp.npy", 10),
                                       (chest_types.RARE, "rare_xp.npy", 2))
            }
        self.sprite_template = sprite_detector.load_or_build_template()
        evidence.load_disc_template()
        self.popup = OnsetDetector(V.EVENT_HIGH_THRESHOLD, V.EVENT_LOW_THRESHOLD, True)
        self.sprite = OnsetDetector(sprite_detector.EVENT_HIGH_THRESHOLD, sprite_detector.EVENT_LOW_THRESHOLD, False)
        self.pending_popups, self.pending_interactions = [], []
        self.sprite_events = []
        if self.french_xp_templates:
            normal_xp = self.french_xp_templates[chest_types.NORMAL][0]
            self.french_normal_xp_blurred = V.blur_template(normal_xp, ROBUST_SIGMA)
            self.french_exp_blurred = V.blur_template(normal_xp[:14, :22], ROBUST_SIGMA)
        self.prompt_series = deque()       # (t, score) every prompt sample
        self.prompt_obs = deque()          # (t, score, gold, gem, chest_like) for scores >= SUPPORT_MIN
        self.ammo_steps = deque()          # times of ammo-region steps
        self.popup_hist = deque()          # (t, best score, y, x, template index) every frame; diagnostics only
        self.ammo_prev, self.ammo_prev_diff = None, 0
        self.last_interaction_time = -1e9
        self.frame_index = 0
        self.now = 0.0
        on = SCALE_AWARE_POPUPS if scale_aware is None else scale_aware
        self.scaled_templates = {tid: self.robust_templates[tid] for tid in SCALED_POPUP_TEMPLATES
                                 if on and tid in self.robust_templates and APPROX_COUNTS_MEDIUM_ALONE.get(tid, False)}
        if on and PORTUGUESE_POPUP in self.robust_templates:
            self.scaled_templates[PORTUGUESE_POPUP] = self.robust_templates[PORTUGUESE_POPUP]
        self.preferred_hud_scale = None
        if hud_scale:
            target = REFERENCE_HUD_SCALE / hud_scale
            self.preferred_hud_scale = min((*SCALED_SCALES, WIDE_HUD_SCALE), key=lambda scale: abs(scale - target))
        self.wide_hud_roi = None
        self.scaled_onset = OnsetDetector(SCALED_TRIGGER, SCALED_REARM, False)
        self.pending_scaled = []
        self.exact_onsets = deque()
        self.scale_votes, self.scale_locked = Counter(), None
        self.scaled_stats = {"prefilter_pass": 0, "onsets": 0, "dropped_for_exact": 0, "search_ms_total": 0.0,
                             "search_ms_max": 0.0, "small_onsets": 0, "small_searches": 0, "stacked_changes": 0,
                             "stacked_rejected_no_xp": 0, "tracker_ms_total": 0.0}
        english = "en:CHEST" in self.robust_templates and APPROX_COUNTS_MEDIUM_ALONE.get("en:CHEST", False)
        self.small = None
        if (SMALL_HUD_POPUPS if small_hud is None else small_hud) and english:
            tpl = np.load(paths.TEMPLATES_DIR / SMALL_TEMPLATE_FILE).astype(np.float32)
            self.small = {"array": tpl, "blur": V.blur_template(tpl, ROBUST_SIGMA)}
        self.small_onset = OnsetDetector(SMALL_TRIGGER, SMALL_REARM, False)
        self.small_last_onset = None       # (t, y, x) of the last small-HUD pop-up taken as a new one
        self.stack_word = next((word for word in SCALED_POPUP_TEMPLATES if word in self.robust_templates), None)
        self.stacked = bool((STACKED_POPUPS if stacked is None else stacked) and self.stack_word)
        self.french_group = chest_types.FrenchQualifiedPopups() if self.stacked and self.stack_word == "fr:COFFRE" else None
        self.trackers = {}                 # "normal" / "small" -> popup_lines.PopupTracker
        self.tracker_scales = {}           # fixed normalized plane for enlarged French HUD runs
        self.tracking = None               # the key of the tracker following the pop-up on screen
        self.tracked_scaled = None         # {"blur", "scale"} when that pop-up came from a scale-aware / small channel
        self.robust_hist = deque()         # (t, {template: blurred match}) while a pop-up is followed (stacked steps)
        self.runs = {}                     # (tracker key, run) -> that pop-up's line votes (chest types)
        self.open_labels = []              # (event, slot): counted chests whose pop-up is still on screen
        self.type_updates = []             # labels that changed after their chest was counted
        self.tracked_run = None            # the tracker run (one per pop-up) of the pop-up on screen...
        self.tracked_counted = False       # ...and whether that pop-up has already been counted

    # ------------------------------------------------------------------ per frame
    def process_frame(self, t, frame):
        """Feed one 1280x720 RGB frame taken at video time `t`. Returns the events finished at this time."""
        self.now = t
        self._popup(t, frame)
        self._sprite(t, frame)
        self._ammo(t, frame)
        if self.frame_index % PROMPT_EVERY_N_FRAMES == 0:
            self._prompt(t, frame)
        self.frame_index += 1
        self._trim(t)
        if self.open_labels:
            self._refresh_labels()
        return self._finish(t, final=False)

    def flush(self):
        """End of stream: finish whatever is pending with the evidence that exists."""
        for p in self.pending_scaled:
            self._promote_scaled(p)
        self.pending_scaled = []
        out = self._finish(self.now, final=True)
        self._refresh_labels(final=True)
        return out

    # ------------------------------------------------------------------ clue updates
    def _popup(self, t, frame):
        roi = frame[V.ROI_Y:V.ROI_Y + V.ROI_H, V.ROI_X:V.ROI_X + V.ROI_W]
        mask = V.lavender_mask(roi)
        self.wide_hud_roi = None
        # A large XP group leaves enough lavender ink in the text area to
        # justify one extra scale, even after the normal scale has locked.
        text_mask = mask[35:112, 100:250]
        if (self.scaled_templates or self.french_group is not None) and text_mask.sum() >= 110:
            scale = WIDE_HUD_SCALE
            self.wide_hud_roi = V.resize_box(roi, round(roi.shape[1] * scale), round(roi.shape[0] * scale))
        if self.french_group is not None:
            enlarged = (WIDE_HUD_SCALE, self.wide_hud_roi) if self.wide_hud_roi is not None else None
            faint_scale = 0.9 if text_mask.sum() >= 110 else None
            for score, y, x, kind, _ in self.french_group.observe(t, roi, enlarged, faint_scale=faint_scale):
                self.pending_popups.append({"onset": t, "y": y, "x": x, "score": score, "template": "fr:COFFRE",
                                            "scaled": {"blur": score, "scale": 1.0, "confirmed": True},
                                            "french_type": kind, "qualified_french": True})
            self.pending_scaled[:] = [p for p in self.pending_scaled if not self._qualified_french_owns(p)]
            self.pending_popups[:] = [p for p in self.pending_popups
                                      if p.get("qualified_french") or not self._qualified_french_owns(p)]
        best = (0.0, 0, 0, 0)
        where = {}                                          # template id -> top-left of its best exact match
        if mask.sum() >= 20:
            for k, tpl in enumerate(self.templates):
                score, y, x = V.best_template_correlation(mask, tpl["array"])
                where[tpl["id"]] = (y, x)
                if score > best[0]:
                    h, w = tpl["array"].shape
                    best = (score, y + h // 2, x + w // 2, k)
        score, y, x, k = best
        self.popup_hist.append((t, score, y, x, k if score > 0 else -1))
        for p in self.pending_popups:                       # keep the peak over the first second
            if t <= p.get("until", p["onset"] + POPUP_FINISH_DELAY) and score > p["score"]:
                p.update(score=score, y=y, x=x, template=self.templates[k]["id"])
                if SAVE_SNAPSHOTS:
                    p["roi"] = roi.copy()
        word = self.templates[k]["id"] if score > 0 else "-"
        exact_fired = self.popup.update(t, score, y, x, allow_jump=not (self.stacked and word == self.stack_word))
        if exact_fired and word == self.stack_word and self._same_popup_counted():
            exact_fired = False                              # the pop-up on screen, already counted: not a new chest
            self.scaled_stats["exact_same_popup"] = self.scaled_stats.get("exact_same_popup", 0) + 1
            self.exact_onsets.append(t)                      # ...but it still owns any blurred onset near it
        if exact_fired and self.french_group is not None and word == "fr:COFFRE" and self.french_group.owns(y, x):
            exact_fired = False
        if exact_fired:
            self.pending_popups.append({"onset": t, "score": score, "y": y, "x": x, "template": word})
            if SAVE_SNAPSHOTS:
                self.pending_popups[-1]["roi"] = roi.copy()
            if word == self.stack_word:
                run = self._track(t, "normal", None, (y, x))
                if run is not None:
                    self.pending_popups[-1]["slot"] = (self.tracking, run, "first")
        # Compression-robust glyph score, only while a popup is being evaluated (cheap: ~15 frames per popup).
        live = [p for p in self.pending_popups if t <= p.get("until", p["onset"] + POPUP_FINISH_DELAY)]
        tracked = self.stacked and self.tracking is not None and self.trackers[self.tracking].active
        if (live or tracked) and ROBUST_PROMOTION and where:
            frame_r = {}
            for tpl in self.templates:
                # Only around the exact match's position (+-8 px): the confirmation must come from the SAME glyph, and
                # it costs ~1 ms instead of ~12 ms for the whole popup region.
                (y0, x0), (h, w) = where[tpl["id"]], tpl["array"].shape
                crop = mask[max(0, y0 - ROBUST_SEARCH_PX):y0 + h + ROBUST_SEARCH_PX, max(0, x0 - ROBUST_SEARCH_PX):x0 + w + ROBUST_SEARCH_PX]
                r = V.robust_correlation(crop, self.robust_templates[tpl["id"]], ROBUST_SIGMA)
                frame_r[tpl["id"]] = r
                for p in live:
                    best_r = p.setdefault("robust", {})
                    best_r[tpl["id"]] = max(best_r.get(tpl["id"], 0.0), r)
            if tracked:
                self.robust_hist.append((t, frame_r))
        while self.robust_hist and self.robust_hist[0][0] < t - 4.0:
            self.robust_hist.popleft()
        if self.scaled_templates or self.small is not None:
            self._scaled_popup(t, roi, mask, exact_fired)
        if self.stacked and self.tracking is not None:
            self._stacked(t, roi, mask)

    # ------------------------------------------------------------------ stacked pop-ups (STACKED_POPUPS)
    def _same_popup_counted(self):
        return (self.stacked and self.tracking is not None and self.trackers[self.tracking].active
                and self.trackers[self.tracking].run == self.tracked_run and self.tracked_counted)

    def _track(self, t, key, scaled, center):
        """A pop-up onset: follow its content from now on (one pop-up at a time; the one on screen keeps its tracker).
        center = (y, x) of the matched word in pop-up box pixels."""
        if not self.stacked:
            return None
        if self.tracking is not None and self.trackers[self.tracking].active:
            if key == "normal" and scaled is None and self.tracking == "normal":
                self.tracked_scaled = None             # the exact path claimed this pop-up: its steps follow its rules
                self.tracked_counted = True
            return self.trackers[self.tracking].run
        if key not in self.trackers:
            if key == "small":
                tpl, line_min = self.small["array"], STACKED_LINE_MIN_SMALL
            else:
                tpl, line_min = next(x["array"] for x in self.templates if x["id"] == self.stack_word), STACKED_LINE_MIN
            self.trackers[key] = popup_lines.PopupTracker(tpl, line_min, SAMPLE_FPS)
            if CHEST_TYPES and self.stack_word == "en:CHEST":
                prefixes = chest_types.load_prefixes(key)
                th, tw = tpl.shape
                self.trackers[key].classify = lambda m, ln, p=prefixes, h=th, w=tw: chest_types.classify_line(
                    m, ln.y, ln.x, h, w, p)[0]
            elif CHEST_TYPES and self.stack_word == "fr:COFFRE":
                suffixes = self.french_suffixes
                th, tw = tpl.shape
                self.trackers[key].classify = lambda m, ln, s=suffixes, h=th, w=tw: chest_types.classify_french_line(
                    m, ln.y, ln.x, h, w, s)
        self.trackers[key].start(t, center)
        self.runs[(key, self.trackers[key].run)] = self.trackers[key].info
        while len(self.runs) > 50:
            self.runs.pop(next(iter(self.runs)))
        self.tracking, self.tracked_scaled = key, scaled
        self.tracked_run, self.tracked_counted = self.trackers[key].run, scaled is None    # an exact onset counts at once
        return self.tracked_run

    def _stacked(self, t, roi, mask):
        tracker = self.trackers[self.tracking]
        scale = self.tracker_scales.get(self.tracking, 1.0)
        if scale != 1.0:
            if self.french_plane is not None and self.french_plane[0] == scale:
                mask = self.french_plane[2]
            else:
                mask = V.lavender_mask(V.resize_box(roi, round(roi.shape[1] * scale), round(roi.shape[0] * scale)))
        a = time.perf_counter()
        before = tracker.rejected_without_xp
        change = tracker.update(t, mask)
        self.scaled_stats["tracker_ms_total"] += 1000 * (time.perf_counter() - a)
        self.scaled_stats["stacked_rejected_no_xp"] += tracker.rejected_without_xp - before
        if not tracker.active:
            self.tracking = None
        if change is None:
            return
        self.scaled_stats["stacked_changes"] += 1
        since = [h for h in self.popup_hist if h[0] >= change["t"]] or list(self.popup_hist)[-1:]
        score, y, x = max(since, key=lambda h: h[1])[1:4] if since else (0.0, 0, 0)   # its frames since the change began
        popup = {"onset": float(change["t"]), "until": t + POPUP_FINISH_DELAY, "score": score, "y": y, "x": x,
                 "template": self.stack_word, "increment": {k: (float(v) if k == "t" else v) for k, v in change.items()}}
        robust = [r.get(self.stack_word, 0.0) for tt, r in self.robust_hist if tt >= change["t"]]
        if robust:                                           # its compression-robust match since the change began
            popup["robust"] = {self.stack_word: max(robust)}
        if self.tracked_scaled is not None:
            popup["scaled"] = dict(self.tracked_scaled)
        popup["slot"] = (self.tracking, tracker.run, change.get("line"))
        if SAVE_SNAPSHOTS:
            popup["roi"] = roi.copy()
        if not self._qualified_french_owns(popup):
            self.pending_popups.append(popup)

    def _qualified_french_owns(self, popup):
        return (self.french_group is not None and popup["template"] == "fr:COFFRE"
                and self.french_group.owns(popup["y"], popup["x"]))

    # ------------------------------------------------------------------ scale-aware pop-ups (SCALE_AWARE_POPUPS)
    # ------------------------------------------------------------------ chest types (CHEST_TYPES)
    def _label(self, slot):
        """slot (tracker key, run, line index | "first") -> (type, confidence, evidence)."""
        if slot is None or not CHEST_TYPES:
            return chest_types.UNKNOWN, 0.0, {"read": "no tracked pop-up line"}
        key, run, line = slot
        info = self.runs.get((key, run))
        if info is None:
            return chest_types.UNKNOWN, 0.0, {"read": "pop-up no longer known"}
        if line == "first":                                  # a new pop-up's newest line (its only one, usually)
            if info["first_count"] is not None:
                line = info["first_count"] - 1
            else:                                            # it never stood still: the newest line that was read
                line = max(info["votes"]) if info["votes"] else None
        if line is None:
            return chest_types.UNKNOWN, 0.0, {"read": "line not settled yet", "hud": key}
        votes = info["votes"].get(line, Counter())
        kind, confidence = chest_types.decide(votes)
        return kind, confidence, {"line": line, "hud": key, "votes": dict(votes)}

    def _refresh_labels(self, final=False):
        """Counted chests whose pop-up is still followed: if their line's type became clearer, update the label."""
        keep = []
        for ev, slot in self.open_labels:
            kind, confidence, evidence = self._label(slot)
            if kind != ev["chest_type"] and (kind != chest_types.UNKNOWN or final):
                if kind != chest_types.UNKNOWN:
                    self.type_updates.append({"time": ev["time"], "chest_type": kind, "previous": ev["chest_type"],
                                              "type_confidence": confidence, "type_evidence": evidence})
                    ev.update(chest_type=kind, type_confidence=confidence, type_evidence=evidence, type_refined=True)
            tracker = self.trackers.get(slot[0])
            if not final and tracker is not None and tracker.active and tracker.run == slot[1]:
                keep.append((ev, slot))
        self.open_labels = keep

    def take_type_updates(self):
        """Labels that changed after their chest was counted -> [{"time", "chest_type", "previous", ...}], then cleared."""
        out, self.type_updates = self.type_updates, []
        return out

    def _scaled_popup(self, t, roi, mask, exact_fired):
        """Second pop-up channel, after the exact path of the same frame. Only adds pop-ups the exact path did not find."""
        self.french_plane = None
        if exact_fired:                                     # the exact path owns this pop-up: drop blurred onsets near it
            self.exact_onsets.append(t)
            kept = [p for p in self.pending_scaled if p["onset"] < t - SCALED_WAIT]
            self.scaled_stats["dropped_for_exact"] += len(self.pending_scaled) - len(kept)
            self.pending_scaled = kept
        while self.exact_onsets and self.exact_onsets[0] < t - SCALED_WAIT - 1.0:
            self.exact_onsets.popleft()
        best = (0.0, 0, 0, 1.0, None, False)
        small = (0.0, 0.0, 0, 0)
        supported_portuguese = (PORTUGUESE_POPUP in self.scaled_templates
                                and self._object_support(t - MERGE_POPUP_AFTER_PROMPT, t) is not None)
        if mask.sum() >= SCALED_PREFILTER_PX:
            a = time.perf_counter()
            if self.scaled_templates and self.scale_locked != SMALL_SCALE:
                best = (self._scaled_best(roi, mask, supported_portuguese=True) if supported_portuguese
                        else self._scaled_best(roi, mask))
            if self.small is not None and (self.scale_locked is None or self.scale_locked == SMALL_SCALE):
                small = self._small_best(mask)
                self.scaled_stats["small_searches"] += 1
            ms = 1000 * (time.perf_counter() - a)
            st = self.scaled_stats
            st["prefilter_pass"] += 1
            st["search_ms_total"] += ms
            st["search_ms_max"] = max(st["search_ms_max"], ms)
        if self.small is not None:
            self._small_popup(t, roi, small)
        score, cy, cx, s, tid, strong = best
        plane = self.french_plane if tid == "fr:COFFRE" and self.french_plane is not None else None
        if plane is not None and plane[0] != 1.0:
            recovered = self._recover_french_line(t, plane[1], plane[2], score,
                                                  round(cy * s), round(cx * s), scale=s)
            if recovered is not None:
                ry, rx, kind, confirmed = recovered
                recovered = (round(ry / s), round(rx / s), kind, confirmed)
            elif 0.62 <= score < SCALED_TRIGGER:
                # A weak resized word can beat the native word even when only the
                # native plane has three aligned readings of the full XP header.
                recovered = self._recover_french_line(t, roi, mask, 0.0, 0, 0)
                if recovered is not None:
                    plane = None
        else:
            recovered = self._recover_french_line(t, roi, mask, score, cy, cx) if "fr:COFFRE" in self.scaled_templates else None
        if recovered is None and plane is not None and 0.9 in self._scaled_scales() and score >= 0.58:
            # A shrunken word can be readable in the bright text pixels even
            # when the normal lavender mask drops parts of the EXP header.
            bright_scale = 0.9
            bright_roi = (plane[1] if plane[0] == bright_scale else
                          V.resize_box(roi, round(roi.shape[1] * bright_scale),
                                       round(roi.shape[0] * bright_scale)))
            recovered = self._recover_bright_french(t, bright_roi)
            if recovered is None:
                exp_line = self._recover_bright_exp_line(t, bright_roi)
                if exp_line is not None:
                    self._queue_french_exp(t, exp_line, score, roi)
            if recovered is not None:
                ry, rx, kind, confirmed = recovered
                recovered = (round(ry / bright_scale), round(rx / bright_scale), kind, confirmed)
                s = bright_scale
        if recovered is None and plane is not None and score >= 0.62 and 1.0 in self._scaled_scales():
            stacked_line = self._recover_french_stacked_exp_line(t, roi, mask)
            if stacked_line is not None:
                self._queue_french_exp(t, stacked_line, score, roi, scale=1.0)
        saturated_french = False
        if recovered is None and score < 0.50 and "fr:COFFRE" in self.scaled_templates:
            recovered = self._recover_saturated_french(t, roi, mask)
            if recovered is not None:
                plane = None
                saturated_french = True
        if recovered is not None:
            cy, cx, kind, confirmed = recovered
            s, tid = (s if plane is not None else 1.0), "fr:COFFRE"
        near_french = []
        if tid == "fr:COFFRE":
            th, tw = self.french_template.shape
            near_french = [p for p in self.pending_scaled if p["template"] == tid
                           and t - p["onset"] <= SCALED_WAIT
                           and abs(p["y"] - cy) <= th + 1 and abs(p["x"] - cx) <= tw]
        if self.scaled_onset.update(t, max(score, SCALED_TRIGGER) if recovered is not None else score, cy, cx):
            owned = self.french_group is not None and tid == "fr:COFFRE" and self.french_group.owns(cy, cx)
            if tid == "fr:COFFRE" and self.french_group is not None and recovered is None:
                owned = owned or self.french_group.owns_fading_sprite(t, cy, cx)
            # A normalized French tracker owns this visible group, including
            # fade/reflow. Only its XP-confirmed content changes add openings.
            tracked_french = (tid == "fr:COFFRE" and self.tracking in self.tracker_scales
                              and self.trackers[self.tracking].active)
            same_french_line = any(p.get("confirmed") or recovered is not None for p in near_french)
            if not (owned or same_french_line or tracked_french) and not any(abs(o - t) <= SCALED_WAIT for o in self.exact_onsets) and \
                    not any(abs(p["onset"] - t) <= POPUP_FINISH_DELAY for p in self.pending_popups):
                self.pending_scaled.append({"onset": t, "blur": score, "scale": s, "y": cy, "x": cx, "template": tid})
                if tid == "fr:COFFRE":
                    self.pending_scaled[-1]["origin"] = (s, cy, cx)
                if recovered is not None:
                    self.pending_scaled[-1]["confirmed"] = confirmed
                    self.pending_scaled[-1]["french_type"] = kind
                    if saturated_french:
                        self.pending_scaled[-1]["saturated_french"] = True
                if SAVE_SNAPSHOTS:
                    self.pending_scaled[-1]["roi"] = roi.copy()
                self.scaled_stats["onsets"] += 1
                if s == 1.0 and tid == self.stack_word:
                    self.pending_scaled[-1]["run"] = self._track(t, "normal", {"blur": score, "scale": s}, (cy, cx))
                elif tid == "fr:COFFRE" and tid == self.stack_word:
                    key = ("fr", s)
                    self.tracker_scales[key] = s
                    self.pending_scaled[-1]["run"] = self._track(
                        t, key, {"blur": score, "scale": s}, (round(cy * s), round(cx * s)))
                    self.pending_scaled[-1]["tracker_key"] = self.tracking
        if recovered is not None:
            # The first flash can shift horizontally before the word settles. Associate the reading
            # with just the closest pending opening on that XP line, within one word's width.
            candidate = min(near_french, key=lambda p: (abs(p["y"] - cy) + abs(p["x"] - cx), -p["onset"]),
                            default=None)
            if candidate is not None:
                if confirmed:
                    candidate["confirmed"] = True
                if kind != chest_types.UNKNOWN and (confirmed or "french_type" not in candidate):
                    candidate["french_type"] = kind
        if plane is not None and score >= FRENCH_TYPE_READ_MIN:
            self._vote_french_type(t, best, plane[2])
        if plane is not None and score >= 0.58 and 0.9 in self._scaled_scales():
            self._observe_french_contrast_type(t, roi, plane)
        self._observe_shifted_french_type(t, roi)
        for p in list(self.pending_scaled):                 # not claimed by the exact path in time -> a pop-up
            if not p.get("small") and t <= p["onset"] + POPUP_FINISH_DELAY:
                origin = p.get("origin")
                same = (origin is None or (tid == p["template"] and s == origin[0]
                                           and abs(origin[1] - cy) <= 5 and abs(origin[2] - cx) <= 5))
                if same and score > p["blur"]:
                    p.update(blur=score, scale=s, y=cy, x=cx)
                    if SAVE_SNAPSHOTS:
                        p["roi"] = roi.copy()
                if origin is not None:
                    count = p.get("confirmation_frames", 0) + 1 if same and strong else 0
                    p["confirmation_frames"] = count
                    if count >= FRENCH_SCALED_CONFIRM_FRAMES:
                        p["confirmed"] = True
            if t >= p["onset"] + SCALED_WAIT:
                self.pending_scaled.remove(p)
                self._promote_scaled(p)

    def _observe_shifted_french_type(self, t, roi):
        """Read a settled bare word after a faint French notification moves horizontally."""
        if len(self.pending_scaled) != 1 or self.french_template is None:
            return
        popup = self.pending_scaled[0]
        if (popup["template"] != "fr:COFFRE" or popup.get("confirmed")
                or popup["scale"] not in (0.85, 0.9) or t - popup["onset"] > SCALED_WAIT
                or any(abs(popup["onset"] - stamp) <= 1.4 for stamp in self.recent_counted_popups)
                or any(abs(popup["onset"] - p["onset"]) <= 1.4 for p in self.pending_popups)):
            return
        scale = 0.9
        normalized = V.resize_box(roi, round(roi.shape[1] * scale), round(roi.shape[0] * scale))
        mask = V.cool_highlight_mask(normalized)
        h, w = self.french_template.shape
        y, x = round(popup["y"] * scale), round(popup["x"] * scale)
        y0, x0 = max(0, y - h), max(0, x - 55)
        region = mask[y0:y + h, x0:x + 55]
        score, word_y, word_x = V.robust_correlation_at(region, self.robust_templates["fr:COFFRE"], ROBUST_SIGMA)
        if score < 0.84:
            return
        word_y, word_x = y0 + word_y, x0 + word_x
        if chest_types.classify_french_line(mask, word_y, word_x, h, w, self.french_suffixes) != chest_types.NORMAL:
            return
        reads = popup.setdefault("shifted_type_reads", deque(maxlen=6))
        reads.append((t, word_y, word_x))
        aligned = sum(t - stamp <= 0.55 and abs(py - word_y) <= 2 and abs(px - word_x) <= 3
                      for stamp, py, px in reads)
        votes = popup.get("french_type_votes", {})
        corroborated = votes.get(chest_types.NORMAL, 0) > 0 and set(votes) == {chest_types.NORMAL}
        if aligned >= FRENCH_SCALED_CONFIRM_FRAMES or (aligned >= 2 and corroborated):
            popup["french_type"] = chest_types.NORMAL

    def _vote_french_type(self, t, match, mask):
        _, word_y, word_x, scale, _, _ = match
        th, tw = self.french_template.shape
        y, x = round(word_y * scale) - th // 2, round(word_x * scale) - tw // 2
        reading = chest_types.classify_french_line(mask, y, x, th, tw, self.french_suffixes)
        history = self.french_type_history
        while history and t - history[0][0] > 0.45:
            history.popleft()
        # The word can already be legible before the opening crosses the threshold.
        # Save these readings briefly and assign them only to one nearby onset.
        for pending in reversed(self.pending_scaled):
            origin = pending.get("origin")
            if (origin is None or t - pending["onset"] > SCALED_WAIT
                    or abs(origin[1] - word_y) > 4 or abs(origin[2] - word_x) > 24):
                continue
            votes = pending.setdefault("french_type_votes", Counter())
            votes.update(kind for _, prev_y, prev_x, kind in history
                         if abs(prev_y - word_y) <= 4 and abs(prev_x - word_x) <= 8)
            history.clear()
            if reading != chest_types.UNKNOWN:
                votes[reading] += 1
            return
        if self.scaled_onset.armed and reading != chest_types.UNKNOWN:
            history.append((t, word_y, word_x, reading))

    def _observe_french_contrast_type(self, t, roi, plane):
        """Collect type-only readings of the bright glyph core on noisy backgrounds."""
        history = self.french_contrast_history
        while history and t - history[0][0] > SCALED_WAIT + 0.9:
            history.popleft()
        scale = 0.9
        normalized = (plane[1] if plane[0] == scale else
                      V.resize_box(roi, round(roi.shape[1] * scale), round(roi.shape[0] * scale)))
        y0, x0 = 38, 90
        crop = normalized[y0:105, x0:230]
        light = crop.min(axis=2).astype(np.float32)
        contrast = light - gaussian_filter(light, 2.0)
        mask = ((contrast > 40) & (crop[:, :, 1] > 125)).astype(np.float32)
        score, y, x = V.robust_correlation_at(mask, self.robust_templates["fr:COFFRE"], ROBUST_SIGMA)
        if score < 0.76:
            return
        h, w = self.french_template.shape
        kind = chest_types.classify_french_line(mask, y, x, h, w, self.french_suffixes)
        if kind == chest_types.NORMAL:
            # A lower contrast reading can still identify a faint qualifier.
            soft = ((contrast > 14) & (crop[:, :, 1] > 125)).astype(np.float32)
            alternate = chest_types.classify_french_line(soft, y, x, h, w, self.french_suffixes)
            if alternate in (chest_types.RARE, chest_types.SPRITE):
                kind = alternate
        if kind != chest_types.UNKNOWN:
            history.append((t, round((y0 + y + h // 2) / scale), round((x0 + x + w // 2) / scale), kind))

    def _contrast_type_votes(self, popup):
        history = self.french_contrast_history
        onset = popup["onset"]
        # Do not borrow a readable row from a second opening in the same XP group.
        if (any(abs(stamp - onset) <= 1.4 for stamp in self.recent_counted_popups)
                or any(abs(other["onset"] - onset) <= 1.4
                       for other in (*self.pending_scaled, *self.pending_popups) if other is not popup)):
            return Counter()
        nearby = [row for row in history if onset - 0.9 <= row[0] <= onset + 1.4
                  and abs(row[2] - popup["x"]) <= 34
                  and abs(row[1] - popup["y"]) <= (10 if abs(row[2] - popup["x"]) <= 10 else 5)]
        if any(row[3] != chest_types.NORMAL for row in nearby):
            return Counter()
        for _, y, x, _ in nearby:
            aligned = [row for row in nearby if abs(row[1] - y) <= 3 and abs(row[2] - x) <= 4]
            if len(aligned) >= FRENCH_SCALED_CONFIRM_FRAMES:
                used = set(nearby)
                self.french_contrast_history = deque(row for row in history if row not in used)
                return Counter({chest_types.NORMAL: len(aligned)})
        return Counter()

    def _recover_french_line(self, t, roi, mask, score, cy, cx, scale=1.0):
        """Read a French line, returning position, type and whether it earns HIGH."""
        th, tw = self.french_template.shape
        y, x = cy - th // 2, cx - tw // 2
        kind = chest_types.UNKNOWN
        sprite = self.french_suffixes[chest_types.SPRITE]
        xp_region = mask[32:112, 108:236]  # centered on the HUD XP notification, in popup-box pixels
        normal, rare = (V.best_template_correlation(xp_region, self.french_xp_templates[k][0])
                        for k in (chest_types.NORMAL, chest_types.RARE))
        if score >= 0.62 and chest_types.french_sprite_score(mask, y, x, th, tw, sprite) >= 0.78:
            kind = chest_types.SPRITE
        elif rare[0] >= 0.77 and rare[0] >= normal[0] + 0.06:
            kind = chest_types.RARE
            y, x = 32 + rare[1] + 14, 108 + rare[2] + self.french_xp_templates[kind][1]
        elif normal[0] >= 0.72:
            # The two lines give a stronger reading than the small word by itself.
            y, x = 32 + normal[1] + 14, 108 + normal[2] + self.french_xp_templates[chest_types.NORMAL][1]
            kind = chest_types.classify_french_line(mask, y, x, th, tw, self.french_suffixes)
        elif score >= 0.62:
            y0, x0 = max(0, cy - 13), max(0, cx - 57)
            bright = (roi[y0:cy + 13, x0:cx + 40].min(axis=2) > 180).astype(np.float32)
            bright_score, by, bx = V.best_template_correlation(bright, self.french_template)
            y, x = y0 + by, x0 + bx
            ink = mask[y:y + th, x:x + tw].mean()
            if bright_score < 0.71 or not 0.40 <= ink <= 0.85:
                return self._recover_faint_french(t, mask, score, cy, cx, scale, normal)
            # Only a positively read bare word earns a normal-chest label.
            kind = chest_types.classify_french_line(mask, y, x, th, tw, self.french_suffixes)
        else:
            return None
        reads = self.french_reads if scale == 1.0 else self.french_scaled_reads.setdefault(scale, deque())
        while reads and t - reads[0][0] > 0.36:
            reads.popleft()
        reads.append((t, y, x, kind))
        votes = Counter(read_kind for _, read_y, read_x, read_kind in reads
                        if abs(read_y - y) <= 4 and abs(read_x - x) <= 7)
        if sum(votes.values()) < FRENCH_SCALED_CONFIRM_FRAMES:
            return None
        return y + th // 2, x + tw // 2, chest_types.decide(votes)[0], True

    def _recover_bright_french(self, t, roi):
        """Confirm a normal French chest from the bright EXP header and bare word."""
        y0, x0 = 25, 85
        bright = (roi[y0:112, x0:250].min(axis=2) > 145).astype(np.float32)
        xp, xp_y, xp_x = V.robust_correlation_at(bright, self.french_normal_xp_blurred, ROBUST_SIGMA)
        if xp < 0.62:
            return None
        h, w = self.french_template.shape
        y, x = xp_y + 14, xp_x + self.french_xp_templates[chest_types.NORMAL][1]
        top, left = max(0, y - 3), max(0, x - 5)
        crop = bright[top:y + h + 4, left:x + w + 6]
        word, word_y, word_x = V.robust_correlation_at(crop, self.robust_templates["fr:COFFRE"], ROBUST_SIGMA)
        y, x = top + word_y, left + word_x
        if word < 0.69 or chest_types.classify_french_line(
                bright, y, x, h, w, self.french_suffixes) != chest_types.NORMAL:
            return None
        reads = self.french_bright_reads
        while reads and t - reads[0][0] > 0.36:
            reads.popleft()
        reads.append((t, y, x))
        if sum(abs(ry - y) <= 2 and abs(rx - x) <= 3 for _, ry, rx in reads) < FRENCH_SCALED_CONFIRM_FRAMES:
            return None
        return y0 + y + h // 2, x0 + x + w // 2, chest_types.NORMAL, True

    def _recover_bright_exp_line(self, t, roi):
        """Find a chest line beside the EXP glyph when the complete header is unreadable."""
        y0, x0 = 25, 85
        bright = (roi[y0:112, x0:250].min(axis=2) > 145).astype(np.float32)
        word, y, x = V.robust_correlation_at(bright, self.robust_templates["fr:COFFRE"], ROBUST_SIGMA)
        if word < 0.78:
            return None
        h, w = self.french_template.shape
        kind = chest_types.classify_french_line(bright, y, x, h, w, self.french_suffixes)
        if kind not in (chest_types.NORMAL, chest_types.UNKNOWN):
            return None
        # The EXP glyph can sit left of an isolated chest or right of the
        # left-aligned chest line when a different reward occupies the row above.
        xp = max(V.robust_correlation_at(
            bright[max(0, y - dy - 3):max(0, y - dy + 17), max(0, x + dx - 4):max(0, x + dx + 26)],
            self.french_exp_blurred, ROBUST_SIGMA)[0] for dy, dx in ((14, -10), (29, 25)))
        if xp < 0.55:
            return None
        reads = self.french_exp_reads
        while reads and t - reads[0][0] > 0.75:
            reads.popleft()
        reads.append((t, y, x, kind))
        aligned = [seen for _, ry, rx, seen in reads if abs(ry - y) <= 2 and abs(rx - x) <= 3]
        if len(aligned) < FRENCH_SCALED_CONFIRM_FRAMES or chest_types.NORMAL not in aligned:
            return None
        return y0 + y + h // 2, x0 + x + w // 2, chest_types.NORMAL, False

    def _recover_french_stacked_exp_line(self, t, roi, mask):
        """Read a bare COFFRE under another XP reward at native HUD scale."""
        y0, x0 = 60, 105
        word_mask = mask[y0:100, x0:176]
        template = self.robust_templates["fr:COFFRE"]
        score, y, x = V.robust_correlation_at(word_mask, template, ROBUST_SIGMA)
        if score < 0.66:
            return None
        y, x = y0 + y, x0 + x
        h, w = self.french_template.shape
        if chest_types.classify_french_line(mask, y, x, h, w, self.french_suffixes) != chest_types.NORMAL:
            return None
        # A reward occupies the middle row. The EXP glyph is one row higher
        # than in a standalone chest notification and sits right of COFFRE.
        if mask[y - 14:y - 3, x:x + 2 * w].sum() < 24:
            return None
        xp, _, _ = V.robust_correlation_at(mask[y - 28:y - 8, x + 24:x + 54],
                                           self.french_exp_blurred, ROBUST_SIGMA)
        if xp < 0.55:
            return None
        crop = roi[y - 4:y + h + 4, x - 4:x + w + 4]
        light = crop.min(axis=2).astype(np.float32)
        contrast = ((light - gaussian_filter(light, 2.0) > 24) & (crop[:, :, 1] > 125)).astype(np.float32)
        sharp, sy, sx = V.robust_correlation_at(contrast, template, ROBUST_SIGMA)
        if sharp < 0.78 or abs(sy - 4) > 2 or abs(sx - 4) > 2:
            return None
        reads = self.french_stacked_exp_reads
        while reads and t - reads[0][0] > 0.75:
            reads.popleft()
        reads.append((t, y, x))
        if sum(abs(ry - y) <= 2 and abs(rx - x) <= 3 for _, ry, rx in reads) < FRENCH_SCALED_CONFIRM_FRAMES:
            return None
        return y + h // 2, x + w // 2, chest_types.NORMAL, False

    def _queue_french_exp(self, t, line, score, roi, scale=0.9):
        if t - self.last_french_exp > 0.75:
            y, x, kind, _ = line
            candidate = {"onset": t, "score": score, "y": round(y / scale), "x": round(x / scale),
                         "template": "fr:COFFRE", "french_type": kind, "bright_exp": True,
                         "scaled": {"blur": score, "scale": scale}}
            if SAVE_SNAPSHOTS:
                candidate["roi"] = roi.copy()
            self.french_exp_candidates.append(candidate)
        self.last_french_exp = t

    def _recover_saturated_french(self, t, roi, mask):
        """Recover a bare French XP notification when the lavender mask is saturated."""
        y0, x0 = 32, 108
        if mask[y0:112, x0:236].mean() < 0.95:
            return None
        bright = (roi[y0:112, x0:236].min(axis=2) > 165).astype(np.float32)
        xp_score, xp_y, xp_x = V.robust_correlation_at(bright, self.french_normal_xp_blurred, ROBUST_SIGMA)
        if xp_score < 0.75:
            return None
        h, w = self.french_template.shape
        y, x = xp_y + 14, xp_x + self.french_xp_templates[chest_types.NORMAL][1]
        crop = bright[max(0, y - 2):y + h + 3, max(0, x - 4):x + w + 5]
        word_score, _, _ = V.robust_correlation_at(crop, self.robust_templates["fr:COFFRE"], ROBUST_SIGMA)
        if word_score < 0.55:
            return None
        reads = self.french_saturated_reads
        while reads and t - reads[0][0] > 0.36:
            reads.popleft()
        reads.append((t, y, x))
        if sum(abs(ry - y) <= 2 and abs(rx - x) <= 3 for _, ry, rx in reads) < FRENCH_SCALED_CONFIRM_FRAMES:
            return None
        return y0 + y + h // 2, x0 + x + w // 2, chest_types.NORMAL, False

    def _recover_faint_french(self, t, mask, score, cy, cx, scale, xp):
        """Three aligned bare COFFRE + XP pairs can start an estimated opening."""
        if score < 0.74 or xp[0] < 0.48:
            return None
        th, tw = self.french_template.shape
        y, x = cy - th // 2, cx - tw // 2
        xp_y = 32 + xp[1] + 14
        xp_x = 108 + xp[2] + self.french_xp_templates[chest_types.NORMAL][1]
        if abs(y - xp_y) > 2 or abs(x - xp_x) > 3:
            return None
        if chest_types.classify_french_line(mask, y, x, th, tw, self.french_suffixes) != chest_types.NORMAL:
            return None
        reads = self.french_faint_reads.setdefault(scale, deque())
        while reads and t - reads[0][0] > 0.45:
            reads.popleft()
        reads.append((t, y, x))
        if sum(abs(read_y - y) <= 3 and abs(read_x - x) <= 6 for _, read_y, read_x in reads) >= FRENCH_SCALED_CONFIRM_FRAMES:
            return cy, cx, chest_types.NORMAL, False
        return None

    def _small_best(self, mask):
        """Native small-HUD word: exact match, confirmed by the blurred match at the same place -> (gated exact score,
        blurred score, centre y, centre x). Without the confirmation the score is held just under the trigger."""
        tpl = self.small["array"]
        bx0, by0, bx1, by1 = SMALL_SEARCH_BOX
        oy, ox = by0 - V.ROI_Y, bx0 - V.ROI_X
        mask = mask[oy:by1 - V.ROI_Y, ox:bx1 - V.ROI_X]
        if mask.sum() < 20:
            return 0.0, 0.0, 0, 0
        exact, y, x = V.best_template_correlation(mask, tpl)
        th, tw = tpl.shape
        blur = 0.0
        if exact >= SMALL_TRIGGER:
            blur = V.robust_correlation(mask[max(0, y - 3):y + th + 3, max(0, x - 3):x + tw + 3], self.small["blur"],
                                        ROBUST_SIGMA)
            if blur < SMALL_CONFIRM:
                exact = min(exact, SMALL_TRIGGER - 0.01)
        return exact, blur, oy + y + th // 2, ox + x + tw // 2

    def _small_popup(self, t, roi, small):
        exact, blur, cy, cx = small
        for p in self.pending_scaled:                       # keep the peak over the pop-up's first second
            if p.get("small") and t <= p["onset"] + POPUP_FINISH_DELAY and blur > p["blur"] and exact >= SMALL_TRIGGER:
                p.update(blur=blur, exact_small=exact, y=cy, x=cx)
        if self.small_onset.update(t, exact, cy, cx):
            last = self.small_last_onset
            back = last is not None and t - last[0] < SMALL_SAME_POPUP_SECONDS and \
                max(abs(cy - last[1]), abs(cx - last[2])) <= SMALL_SAME_POPUP_PIXELS
            if back or (self.stacked and self.tracking == "small" and self.trackers["small"].active and (
                    self._same_popup_counted() or any(p.get("small") and p.get("run") == self.tracked_run for p in self.pending_scaled))):
                # the pop-up on screen, already counted, matched again (a dip and a return): not a new chest. A chest added to it is the
                # pop-up tracker's to count.
                self.scaled_stats["small_same_popup"] = self.scaled_stats.get("small_same_popup", 0) + 1
            elif not any(abs(o - t) <= SCALED_WAIT for o in self.exact_onsets) and \
                    not any(abs(p["onset"] - t) <= POPUP_FINISH_DELAY for p in self.pending_popups):
                self.pending_scaled.append({"onset": t, "blur": blur, "exact_small": exact, "scale": SMALL_SCALE,
                                            "y": cy, "x": cx, "template": "en:CHEST", "small": True})
                if SAVE_SNAPSHOTS:
                    self.pending_scaled[-1]["roi"] = roi.copy()
                self.scaled_stats["small_onsets"] += 1
                self.pending_scaled[-1]["run"] = self._track(t, "small", {"blur": blur, "scale": SMALL_SCALE}, (cy, cx))
                self.small_last_onset = (t, cy, cx)

    def _scaled_scales(self):
        if self.scale_locked is None:
            return SCALED_SCALES
        if self.scale_locked not in SCALED_SCALES:
            return ()
        i = SCALED_SCALES.index(self.scale_locked)
        return SCALED_SCALES[max(0, i - 1):i + 2]

    def _scaled_best(self, roi, mask, supported_portuguese=False):
        """Best blurred match, with a sharp-glyph check at the same position for French."""
        best = (0.0, 0, 0, 1.0, None)
        winner = None
        self.french_plane = None
        scales = (*self._scaled_scales(), WIDE_HUD_SCALE)
        if self.scale_locked is None and self.preferred_hud_scale in scales:
            preferred = self.preferred_hud_scale
            scales = (preferred, *(scale for scale in scales if scale != preferred))
        localized_small = "fr:COFFRE" in self.scaled_templates or supported_portuguese
        if localized_small:
            scales = (*scales, LOCALIZED_SMALL_HUD_SCALE)
        for s in scales:
            if s == LOCALIZED_SMALL_HUD_SCALE and best[0] >= SCALED_TRIGGER:
                continue
            if s == WIDE_HUD_SCALE:
                scaled_roi = self.wide_hud_roi
                if scaled_roi is None or best[0] >= SCALED_TRIGGER:
                    continue
            else:
                scaled_roi = roi if s == 1.0 else V.resize_box(roi, round(roi.shape[1] * s), round(roi.shape[0] * s))
            m = mask if s == 1.0 else V.lavender_mask(scaled_roi)
            if m.sum() < 20:
                continue
            for tid, tpl in self.scaled_templates.items():
                if s == LOCALIZED_SMALL_HUD_SCALE and tid not in ("fr:COFFRE", PORTUGUESE_POPUP):
                    continue
                if tid == PORTUGUESE_POPUP and (s == 1.0 or not supported_portuguese):
                    continue  # the native exact detector already handles this case
                score, y, x = V.robust_correlation_at(m, tpl, ROBUST_SIGMA)
                if tid == PORTUGUESE_POPUP:
                    if score < ROBUST_HIGH_MIN or score <= best[0]:
                        continue
                    h, w = tpl.shape
                    sharp = self.robust_exact_templates[tid]
                    if V.best_template_correlation(m[max(0, y - 2):y + h + 2,
                                                     max(0, x - 2):x + w + 2], sharp)[0] < V.EVENT_HIGH_THRESHOLD:
                        continue
                if s == LOCALIZED_SMALL_HUD_SCALE and score < SCALED_TRIGGER:
                    continue
                if s == WIDE_HUD_SCALE and score < SCALED_TRIGGER:
                    continue  # weak wide matches must not steal frames from native XP recovery
                if score > best[0]:
                    h, w = tpl.shape
                    best = (score, int((y + h // 2) / s), int((x + w // 2) / s), s, tid)
                    winner = (m, y + h // 2, x + w // 2)
                    self.french_plane = (s, scaled_roi, m) if tid == "fr:COFFRE" else None
        strong = False
        if best[4] == "fr:COFFRE" and best[0] >= ROBUST_HIGH_MIN:
            m, y, x = winner
            h, w = self.french_template.shape
            region = m[max(0, y - h // 2 - 3):y + h // 2 + 4, max(0, x - w // 2 - 3):x + w // 2 + 4]
            if region.shape[0] >= h and region.shape[1] >= w:
                strong = V.best_template_correlation(region, self.french_template)[0] >= V.EVENT_HIGH_THRESHOLD
        return (*best, strong)

    def _promote_scaled(self, p):
        """A blurred onset the exact path did not claim becomes a pending pop-up (finished and counted like any other)."""
        if p.get("run") is not None and p["run"] == self.tracked_run and \
                p.get("tracker_key", self.tracking) == self.tracking:
            self.tracked_counted = True
        exact_peak = max((h[1] for h in self.popup_hist if p["onset"] <= h[0] <= p["onset"] + POPUP_FINISH_DELAY),
                         default=0.0)
        popup = {"onset": p["onset"], "score": exact_peak, "y": p["y"], "x": p["x"], "template": p["template"],
                 "scaled": {"blur": p["blur"], "scale": p["scale"]}}
        if p.get("small"):
            popup["scaled"]["exact_small"] = p.get("exact_small", 0.0)
        if p.get("confirmed"):
            popup["scaled"]["confirmed"] = True
        if "french_type" in p:
            popup["french_type"] = p["french_type"]
        if "french_type_votes" in p:
            popup["french_type_votes"] = p["french_type_votes"]
        if (p["template"] == "fr:COFFRE" and p.get("origin", (0,))[0] in (0.85, 0.9)
                and p.get("french_type", chest_types.UNKNOWN) == chest_types.UNKNOWN
                and not any(p.get("french_type_votes", {}).get(kind) for kind in (chest_types.RARE, chest_types.SPRITE))):
            contrast_votes = self._contrast_type_votes(p)
            if contrast_votes:
                popup["french_contrast_votes"] = contrast_votes
        if p.get("saturated_french"):
            popup["saturated_french"] = True
        if p.get("run") is not None:
            popup["slot"] = (p.get("tracker_key", "small" if p.get("small") else "normal"), p["run"], "first")
        if "roi" in p:
            popup["roi"] = p["roi"]
        self.pending_popups.append(popup)

    def _vote_scale(self, p):
        """Strong pop-ups vote for the stream's HUD scale (scaled: its scale; strong exact: 1.0); 2 agreeing votes lock it."""
        s = p.get("scaled")
        if s is not None:
            # A resized French/Portuguese fallback must not disable the native scan.
            if s["blur"] >= SCALED_LOCK_MIN and (s["scale"] <= 1.0 or "exact_small" in s):
                self.scale_votes[s["scale"]] += 1
        elif p["score"] >= ROBUST_HIGH_MIN:
            self.scale_votes[1.0] += 1
        if self.scale_votes and self.scale_locked is None:
            (scale, n), *rest = self.scale_votes.most_common()
            if n >= 2 and (not rest or n >= 2 * rest[0][1]):
                self.scale_locked = scale

    def _sprite(self, t, frame):
        x0, y0, w, h = sprite_detector.ROI_X, sprite_detector.ROI_Y, sprite_detector.ROI_W, sprite_detector.ROI_H
        mask = sprite_detector.white_mask(frame[y0:y0 + h, x0:x0 + w])
        score = V.best_template_correlation(mask, self.sprite_template)[0] if mask.sum() >= 40 else 0.0
        if self.sprite.update(t, score):
            self.sprite_events.append(t)

    def _ammo(self, t, frame):
        x0, y0, x1, y1 = evidence.AMMO_BOX
        mask = evidence.white_mask(frame[y0:y1, x0:x1], 200)
        diff = 0 if self.ammo_prev is None else int((mask ^ self.ammo_prev).sum())
        if diff >= evidence.AMMO_STEP_MIN_PIXELS and self.ammo_prev_diff <= evidence.AMMO_STABLE_MAX_PIXELS:
            self.ammo_steps.append(t)
        self.ammo_prev, self.ammo_prev_diff = mask, diff

    def _prompt(self, t, frame):
        score, y, x = evidence.locate_prompt(frame)
        self.prompt_series.append((t, score))
        if score >= INTERACTION_SUPPORT_PROMPT_MIN:
            f = object_cue.object_features(frame, y, x)
            self.prompt_obs.append((t, score, f["gold"], f["gem"], object_cue.chest_like(f)))
        if score >= INTERACTION_COUNT_PROMPT_MIN and t - self.last_interaction_time >= INTERACTION_REFRACTORY_SECONDS:
            self.pending_interactions.append({"t": t, "score": score, "merged": False})
            self.last_interaction_time = t

    def _trim(self, t):
        while self.popup_hist and self.popup_hist[0][0] < t - 4.0:
            self.popup_hist.popleft()
        for dq in (self.prompt_series, self.prompt_obs):
            while dq and dq[0][0] < t - 12.0:
                dq.popleft()
        while self.ammo_steps and self.ammo_steps[0] < t - 12.0:
            self.ammo_steps.popleft()
        self.sprite_events = [s for s in self.sprite_events if s > t - 12.0]

    # ------------------------------------------------------------------ finishing events
    def _finish(self, t, final):
        out = []
        for p in list(self.pending_popups):
            if final or p.get("until", p["onset"] + POPUP_FINISH_DELAY) <= t:
                self.pending_popups.remove(p)
                if p.get("template") == "fr:COFFRE" and p.get("french_type", chest_types.UNKNOWN) == chest_types.UNKNOWN:
                    matches = [line for line in self.french_exp_candidates
                               if abs(line["onset"] - p["onset"]) <= 1.0
                               and abs(line["y"] - p["y"]) <= 6 and abs(line["x"] - p["x"]) <= 12]
                    if len(matches) == 1:
                        p["french_type"] = matches[0]["french_type"]
                out.append(self._popup_event(p))
        for i in list(self.pending_interactions):
            if final or i["t"] + INTERACTION_FINISH_DELAY <= t:
                self.pending_interactions.remove(i)
                if not i["merged"]:
                    out.append(self._interaction_event(i))
        if self.french_template is not None:
            self.recent_counted_popups.extend(e["time"] for e in out if e["approx_counted"])
            while self.recent_counted_popups and t - self.recent_counted_popups[0] > 7.0:
                self.recent_counted_popups.popleft()
            while self.french_exp_candidates and (final or t - self.french_exp_candidates[0]["onset"] >= 3.25):
                candidate = self.french_exp_candidates.popleft()
                onset = candidate["onset"]
                if any(abs(stamp - onset) <= 1.35 for stamp in self.recent_counted_popups):
                    continue
                event = self._popup_event(candidate)
                out.append(event)
                if event["approx_counted"]:
                    self.recent_counted_popups.append(onset)
        return sorted(out, key=lambda e: e["time"])

    def _object_support(self, a, b):
        """Best chest-like prompt observation in [a, b] -> (score, gold, gem) or None."""
        hits = [o for o in self.prompt_obs if a <= o[0] <= b and o[4]]
        if not hits:
            return None
        best = max(hits, key=lambda o: o[1])
        return best[1], best[2], best[3]

    def _popup_trace(self, p):
        """Temporal summary of the popup over [onset-1 s, onset+1 s]. DIAGNOSTICS ONLY: logged with the event, never used to decide."""
        onset = p["onset"]
        win = [h for h in self.popup_hist if onset - 1.0 <= h[0] <= onset + POPUP_FINISH_DELAY]
        scores = np.array([h[1] for h in win]) if win else np.zeros(1)
        strong = [h for h in win if h[1] >= 0.65]
        lit = [h[0] for h in win if h[1] >= 0.60]
        win_k = next((i for i, t in enumerate(self.templates) if t["id"] == p["template"]), -1)
        return {"frames_ge_075": int((scores >= 0.75).sum()), "frames_ge_065": int((scores >= 0.65).sum()),
                "span_ge_060_s": round(max(lit) - min(lit), 2) if lit else 0.0,
                "top5_mean": round(float(np.sort(scores)[-5:].mean()), 3), "median_ge_065": round(float(np.median([h[1] for h in strong])), 3) if strong else None,
                "y_std": round(float(np.std([h[2] for h in strong])), 1) if len(strong) > 1 else None,
                "x_std": round(float(np.std([h[3] for h in strong])), 1) if len(strong) > 1 else None,
                "template_agreement": round(sum(h[4] == win_k for h in strong) / len(strong), 2) if strong else None}

    def _popup_event(self, p):
        onset = p["onset"]
        sprite_present = any(abs(s - onset) <= SPRITE_NEAR_SECONDS for s in self.sprite_events)
        base_tier, rule = fusion.confidence_tier(p["score"], sprite_present, False)
        steps = sum(1 for s in self.ammo_steps if onset - 1.0 <= s <= onset + 0.5)
        shooting = steps >= evidence.SHOOTING_MIN_STEPS
        prompts = [sc for (tt, sc) in self.prompt_series if onset - 2.5 <= tt <= onset]
        interaction_score = max(prompts) if prompts else 0.0
        interaction = interaction_score >= 0.70
        reliable_word = APPROX_COUNTS_MEDIUM_ALONE.get(p["template"], False)
        shooting_demotes = shooting and (SHOOTING_DEMOTES_RELIABLE_WORDS or not reliable_word)
        adjusted = adjust_tier(base_tier, shooting_demotes, interaction)
        robust = p.get("robust", {}).get(p["template"], 0.0)
        promoted = ROBUST_PROMOTION and adjusted == "MEDIUM" and robust >= ROBUST_HIGH_MIN

        merged = None
        for i in self.pending_interactions:
            if not i["merged"] and i["t"] - MERGE_POPUP_BEFORE_PROMPT <= onset <= i["t"] + MERGE_POPUP_AFTER_PROMPT:
                if merged is None or abs(i["t"] - onset) < abs(merged["t"] - onset):
                    merged = i
        support = self._object_support(onset - MERGE_POPUP_AFTER_PROMPT, onset + POPUP_FINISH_DELAY)
        if merged is not None:
            merged["merged"] = True

        certain = adjusted in CERTAIN_TIERS
        approx = certain
        reason = f"popup {p['score']:.2f} {p['template']} -> {base_tier} ({rule})"
        if adjusted != base_tier:
            reason += f"; adjusted to {adjusted} (shooting={shooting}, interaction={interaction})"
        elif shooting and not shooting_demotes and base_tier == "MEDIUM":
            reason += f"; shooting ignored: {p['template']} word is specific (combat text does not match it)"
        if promoted:
            adjusted = "HIGH"
            certain = adjusted in CERTAIN_TIERS
            approx = certain
            reason += f"; promoted to HIGH: compression-robust glyph match {robust:.2f} >= {ROBUST_HIGH_MIN:.2f}"
        if not certain and adjusted == "MEDIUM":
            if APPROX_COUNTS_MEDIUM_ALONE.get(p["template"], False):
                approx, reason = True, reason + f"; MEDIUM counted approx: {p['template']} word is reliable"
            elif APPROX_MEDIUM_WITH_OBJECT_SUPPORT and support is not None:
                approx, reason = True, reason + f"; MEDIUM counted approx: chest-object prompt support ({support[0]:.2f})"
        scaled = p.get("scaled")
        inc = p.get("increment")
        if scaled is not None and "exact_small" in scaled:
            # HUD drawn smaller: a native small-word match; the normal word's exact score says nothing here. HIGH only when both small-word
            # matches are as strong as SMALL_HIGH_*, otherwise MEDIUM and APPROX only.
            exact_small = scaled.get("exact_small", 0.0)
            strong = exact_small >= SMALL_HIGH_EXACT and scaled["blur"] >= SMALL_HIGH_BLUR
            adjusted, approx = ("HIGH" if strong else "MEDIUM"), True
            certain = adjusted in CERTAIN_TIERS
            reason = (f"small-HUD pop-up: native small CHEST match {exact_small:.2f} / blurred {scaled['blur']:.2f} (HUD drawn ~0.8x; "
                      f"normal-word match {p['score']:.2f}) -> " +
                      (f"HIGH: both at least {SMALL_HIGH_EXACT:.2f} / {SMALL_HIGH_BLUR:.2f}" if strong else
                       f"MEDIUM, counted approx only (CERTAIN needs {SMALL_HIGH_EXACT:.2f} / {SMALL_HIGH_BLUR:.2f})"))
        elif scaled is not None and p["template"] == PORTUGUESE_POPUP:
            adjusted, certain = "MEDIUM", False
            approx = APPROX_MEDIUM_WITH_OBJECT_SUPPORT and support is not None and not shooting
            reason = f"scaled Portuguese BAÚ match {scaled['blur']:.2f}; requires chest-object prompt support"
        elif scaled is not None:
            # The unscaled exact score does not apply here. A French glyph can earn HIGH after rescaling.
            adjusted, certain, approx = "MEDIUM", False, True
            reason = (f"scale-aware pop-up: blurred {p['template']} match {scaled['blur']:.2f} at x{scaled['scale']} "
                      f"(unscaled exact match {p['score']:.2f}) -> MEDIUM, counted approx only")
            if p.get("bright_exp"):
                reason = "French COFFRE and EXP glyph aligned across three frames -> MEDIUM"
            if p.get("saturated_french"):
                reason = "French COFFRE and EXP header aligned across three frames on saturated background -> MEDIUM"
            if scaled.get("confirmed"):
                adjusted = "HIGH"
                certain = adjusted in CERTAIN_TIERS
                reason = ("French qualified chest line and quantity confirmed across three frames -> HIGH"
                          if p.get("qualified_french") else "French COFFRE confirmed across three aligned frames -> HIGH")
        if inc is not None:
            step = {"new_line": "a chest line was added to the pop-up on screen",
                    "multiplier": "the chest line's multiplier " + ("appeared (X2)" if inc.get("change") == "appeared"
                                                                     else "went up")}[inc["kind"]]
            step += " and its XP total changed"
            if inc["kind"] == "multiplier" or scaled is not None or p["score"] < V.EVENT_HIGH_THRESHOLD:
                # a multiplier step, a step of a scale-aware / small pop-up, or a new line whose word never matched the
                # exact template (>= 0.75, what v0.2.1's re-fire needed): the tracker saw it, but APPROX only
                adjusted, certain, approx = "MEDIUM", False, True
                reason = f"stacked pop-up: {step} -> MEDIUM, counted approx only"
            else:
                reason = f"stacked pop-up: {step}; " + reason
        if (self.scaled_templates or self.small is not None) and inc is None and not (
                p.get("qualified_french") or p.get("bright_exp")) and (approx or p["template"] != PORTUGUESE_POPUP):
            self._vote_scale(p)
        if not approx:
            reason += "; NOT counted"
        extra = {"_snapshot": p["roi"]} if "roi" in p else {}
        kind, confidence, type_evidence = self._label(p.get("slot"))
        qualified_type = p.get("qualified_french") and p.get("french_type") in chest_types.KNOWN
        if qualified_type or (kind == chest_types.UNKNOWN and p.get("french_type", chest_types.UNKNOWN) != chest_types.UNKNOWN):
            kind, confidence, type_evidence = p["french_type"], 1.0, {"read": "aligned French XP line", "frames": 3}
        votes = p.get("french_type_votes")
        if kind == chest_types.UNKNOWN and votes:
            observed, agreement = chest_types.decide(votes)
            if observed != chest_types.UNKNOWN:
                kind, confidence = observed, agreement
                type_evidence = {"read": "aligned French blurred line", "votes": dict(votes)}
        if kind == chest_types.UNKNOWN and p.get("french_contrast_votes"):
            kind, confidence = chest_types.decide(p["french_contrast_votes"])
            type_evidence = {"read": "aligned high-contrast French line", "votes": dict(p["french_contrast_votes"])}
        extra.update(chest_type=kind, type_confidence=confidence, type_evidence=type_evidence)
        if scaled is not None:
            extra["scaled_popup"] = {"blur": round(float(scaled["blur"]), 3), "scale": scaled["scale"]}
            if scaled.get("confirmed"):
                extra["scaled_popup"]["confirmed"] = True
        if inc is not None:
            extra["increment"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in inc.items()}
        event = {**extra, "time": onset, "source": "fused" if merged is not None else "popup",
                "base_tier": base_tier, "adjusted_tier": adjusted,
                "popup_score": p["score"], "popup_template": p["template"],
                "interaction_score": max(interaction_score, merged["score"] if merged else 0.0),
                "chest_object_gold": support[1] if support else None, "chest_object_gem": support[2] if support else None,
                "sprite_present": sprite_present, "audio_present": None, "shooting_negative": shooting,
                "ammo_steps": steps, "robust_score": round(robust, 3), "popup_trace": self._popup_trace(p),
                "certain_counted": certain, "approx_counted": approx, "reason": reason}
        slot = p.get("slot")
        tracker = self.trackers.get(slot[0]) if slot else None
        # A qualified line already identifies this particular chest. Generic COFFRE
        # tracking can follow another row as the stack reflows and must not relabel it.
        if approx and not qualified_type and tracker is not None and tracker.active and tracker.run == slot[1]:
            self.open_labels.append((event, slot))          # its pop-up is still on screen: the label may still improve
        return event

    def _interaction_event(self, i):
        tp = i["t"]
        obs = [o for o in self.prompt_obs if tp - 0.5 <= o[0] <= tp + 2.5 and o[4]]
        chest_like = bool(obs)
        best = max(obs, key=lambda o: o[1]) if obs else None
        counted = chest_like and INTERACTION_ONLY_COUNTS_APPROX
        steps = sum(1 for s in self.ammo_steps if tp - 1.0 <= s <= tp + 1.0)
        shooting = steps >= evidence.SHOOTING_MIN_STEPS
        if chest_like:
            reason = (f"prompt {i['score']:.2f} + chest-like object (gold {best[2] * 100:.1f}%, gems {best[3] * 100:.2f}%), "
                      f"no popup merged" + ("" if counted else "; NOT counted"))
        else:
            reason = f"prompt {i['score']:.2f} but no chest-like object: generic container/pickup, NOT counted"
        return {"chest_type": chest_types.UNKNOWN, "type_confidence": 0.0,
                "type_evidence": {"read": "no pop-up (prompt + chest-like object only)"},
                "time": tp, "source": "interaction_object", "base_tier": "-", "adjusted_tier": "-",
                "popup_score": None, "popup_template": None, "interaction_score": i["score"],
                "chest_object_gold": best[2] if best else None, "chest_object_gem": best[3] if best else None,
                "sprite_present": False, "audio_present": None, "shooting_negative": shooting, "ammo_steps": steps,
                "certain_counted": bool(counted and INTERACTION_CAN_BE_CERTAIN), "approx_counted": bool(counted),
                "reason": reason}
