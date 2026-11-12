"""
Offline visual chest-opening detector experiment.

Works ONLY on captured video frames. No game interaction.

What it looks for
-----------------
When a chest is opened, Fortnite shows a small XP-feed popup just right of the
crosshair, for example:

        +900 XP
        RARE CHEST

The popup text is a distinctive pink-white lavender. Other XP popups appear in
the same place (SHIELD CRACKED, ELIMINATION, CROWN BONUS, NEW STORM PHASE...),
so detecting "an XP popup appeared" is NOT enough (measured: ~70 false
positives). What is specific to chests is the word CHEST on the second line
(also in "RARE CHEST", "SPRITE CHEST", "DARK CHEST"...). That word has a fixed
font, so it can be found by template matching.

Pipeline
--------
1. Stream the video through FFmpeg, scaled to a fixed 1280x720 so the HUD
   always has the same pixel size, cropped to a region of interest (ROI).
2. Convert each frame to a binary "lavender text" mask by colour.
3. Slide a small bitmap of the word CHEST over the mask and take the best
   Pearson correlation (a flat blob of lavender scores ~0; the word scores ~1).
4. An event is the moment the score rises above a high threshold. It re-arms
   only after the score has stayed below a low threshold for 0.3 s (and never
   fires within 1 s of the previous event), so one popup that stays on screen
   for ~2 s counts once.

The template bitmap is cut from the first labelled chest (see TEMPLATE_*),
so that chest is scored in-sample; the other four are out-of-sample.

Usage
-----
    python -m fortnite_chest_counter.detection.visual                 # score video (cached), evaluate, plot
    python -m fortnite_chest_counter.detection.visual --rescan        # ignore the cached scores
    python -m fortnite_chest_counter.detection.visual --fusion        # also compare with the audio detector
    python -m fortnite_chest_counter.detection.visual --no-show
"""

import argparse
import subprocess
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from fortnite_chest_counter import paths
from fortnite_chest_counter.detection.audio import GROUND_TRUTH_FILE, OUTPUT_DIR, load_ground_truth, match_events, report


# ==================================================================
# FILES
# ==================================================================

VIDEO_FILE = Path("samples/video tests/Fortnites-NEW-HD-No-Audio.mp4")
SCORE_CACHE = OUTPUT_DIR / "visual_scores.npz"


# ==================================================================
# VIDEO / ROI PARAMETERS
# ==================================================================

# Every frame is scaled to this size first so the HUD text is always the same
# pixel size, whatever resolution the source was recorded at.
NORMALIZED_WIDTH, NORMALIZED_HEIGHT = 1280, 720

# Region of interest (pixels in the normalized frame): the area to the right of
# the crosshair where the XP popup appears. NOTE: width/height/offsets must be
# EVEN or FFmpeg silently rounds them (chroma subsampling) and frames shear.
ROI_X, ROI_Y, ROI_W, ROI_H = 716, 158, 360, 202

# Score every Nth frame (the popup lasts ~2 s, so 2 -> ~15 scores/second is plenty).
SCORE_EVERY_N_FRAMES = 2


# ==================================================================
# TEXT-COLOUR MASK (measured from the popup: R~222 G~195 B~242, core ~255,235,255)
# ==================================================================

# A pixel is "popup text" if red and blue are both bright and green is clearly
# lower than both. Warm white (muzzle flash) and yellow (coins) have G >= B and
# are rejected.
MASK_MIN_RED_BLUE = 170
MASK_GREEN_BELOW = 8


# ==================================================================
# CHEST-WORD TEMPLATES (one per supported game language)
# ==================================================================

# The chest-specific word on the popup's second line. Each language is just
# another template; whichever matches best wins, and every match means the same
# thing: "this popup is a chest". To add a language, cut the word from a popup
# (source video + time + box in normalized 1280x720 pixels) and add a row.
# The box only has to contain the word; it is shrunk to the lit pixels.
#
# The English template is cut from the first labelled chest's popup. The
# Portuguese one is cut from the confirmed ~64 s chest popup of the PS5 recording.
SAMPLES_DIR = Path("samples/video tests")
CHEST_TEMPLATES = [
    {"id": "en:CHEST", "file": paths.TEMPLATES_DIR / "chest/en/chest.npy",
     "source_video": SAMPLES_DIR / "Fortnites-NEW-HD-No-Audio.mp4", "source_time": 51.8,
     "box": (885, 229, 912, 236)},
    {"id": "pt:BAÚ", "file": paths.TEMPLATES_DIR / "chest/pt/bau.npy",
     "source_video": SAMPLES_DIR / "Fortnite-PS5-Pro-HD-No-Audio.mp4", "source_time": 64.0,
     "box": (898, 226, 918, 238)},
    {"id": "fr:COFFRE", "file": paths.TEMPLATES_DIR / "chest/fr/coffre.npy",
     "source_video": SAMPLES_DIR / "Fortnite-ARCHIS-2026-11-07.mp4", "source_time": 69.5,
     "box": (890, 229, 920, 236)},
]


# ==================================================================
# EVENT DETECTION PARAMETERS (tunable)
# ==================================================================

EVENT_HIGH_THRESHOLD = 0.75     # score must rise above this to fire
EVENT_LOW_THRESHOLD = 0.50      # ...and fall below this before it can fire again
# The popup text animates (scales in), which can dip the score below the
# re-arm threshold for a single sample while the popup is still on screen (seen
# at 52.1 s inside the 51.3 s chest's popup). A popup that is really gone stays
# low, so the score must stay below the re-arm threshold this long to re-arm.
EVENT_REARM_HOLD_SECONDS = 0.3
# A chest search takes about a second: never fire twice within this window.
EVENT_MIN_SEPARATION_SECONDS = 1.0
# While a popup is on screen the word CHEST stays put, even as the XP number
# animates. A different popup (e.g. a second chest 2 s after the first) puts the
# word somewhere else, so a jump of this many pixels while the score is high is
# treated as a NEW popup even though the old one never fully disappeared.
EVENT_POSITION_JUMP_PIXELS = 8
EVALUATION_TOLERANCE_SECONDS = 0.75


# ==================================================================
# FRAME HELPERS
# ==================================================================

def lavender_mask(frame_rgb):
    """Boolean-as-float mask of popup-coloured pixels. frame_rgb: (h, w, 3) uint8."""
    red = frame_rgb[..., 0].astype(np.int16)
    green = frame_rgb[..., 1].astype(np.int16)
    blue = frame_rgb[..., 2].astype(np.int16)
    min_red_blue = np.minimum(red, blue)
    return ((min_red_blue > MASK_MIN_RED_BLUE) & (green < min_red_blue - MASK_GREEN_BELOW)).astype(np.float32)


def cool_highlight_mask(rgb):
    """Preserve the pale XP text core while suppressing warm backgrounds."""
    red, green, blue = rgb.astype(np.int16).transpose(2, 0, 1)
    return ((blue >= red - 7) & (green > 140) & (blue > 145)).astype(np.float32)


def scale_filter():
    return f"scale={NORMALIZED_WIDTH}:{NORMALIZED_HEIGHT}"


def read_single_frame(video, seconds, crop=True):
    """One normalized frame at `seconds`, cropped to the ROI unless crop=False."""
    vf = scale_filter() + (f",crop={ROI_W}:{ROI_H}:{ROI_X}:{ROI_Y}" if crop else "")
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(video), "-vf", vf,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    w, h = (ROI_W, ROI_H) if crop else (NORMALIZED_WIDTH, NORMALIZED_HEIGHT)
    return np.frombuffer(out, np.uint8).reshape(h, w, 3)


def load_templates():
    """Load every chest-word template, cutting it from its source video the first time."""
    loaded = []
    for spec in CHEST_TEMPLATES:
        if not spec["file"].exists():
            print(f"Building template {spec['id']} from {spec['source_video'].name} at {spec['source_time']}s ...")
            frame = read_single_frame(spec["source_video"], spec["source_time"], crop=False)
            x0, y0, x1, y1 = spec["box"]
            sub = lavender_mask(frame)[y0:y1, x0:x1]
            ys, xs = np.where(sub > 0)
            spec["file"].parent.mkdir(parents=True, exist_ok=True)
            np.save(spec["file"], sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1])
        loaded.append({"id": spec["id"], "array": np.load(spec["file"])})
    return loaded


def best_template_correlation(mask, template):
    """
    Best Pearson correlation between `template` and any same-sized window of
    `mask`, and where it was found -> (score, y, x).

    Mean-subtracted, so a solid lavender blob (glow, sky) scores ~0 and only a
    matching glyph pattern scores near 1.
    """
    n = template.size
    template_sum = template.sum()
    template_var = template_sum - template_sum ** 2 / n
    overlap = fftconvolve(mask, template[::-1, ::-1], mode="valid")
    window_sum = fftconvolve(mask, np.ones_like(template), mode="valid")
    numerator = overlap - window_sum * template_sum / n
    window_var = window_sum - window_sum ** 2 / n
    correlation = numerator / np.sqrt(np.maximum(template_var * window_var, 1e-6))
    y, x = np.unravel_index(np.argmax(correlation), correlation.shape)
    return float(correlation[y, x]), int(y), int(x)


def _box_coefficients(in_size, out_size):
    """Pillow's BOX resampling weights for one axis (Resample.c precompute_coeffs + normalize_coeffs_8bpc):
    -> (first source index per output pixel, integer weights [out, taps] in 22-bit fixed point)."""
    precision = 22
    scale = in_size / out_size
    filterscale = max(scale, 1.0)
    support = 0.5 * filterscale
    taps = int(np.ceil(support)) * 2 + 1
    first = np.zeros(out_size, np.int64)
    weights = np.zeros((out_size, taps), np.float64)
    for xx in range(out_size):
        center = (xx + 0.5) * scale
        xmin = max(int(center - support + 0.5), 0)
        xmax = min(int(center + support + 0.5), in_size) - xmin
        w = np.array([1.0 if -0.5 < (x + xmin - center + 0.5) / filterscale <= 0.5 else 0.0 for x in range(xmax)])
        if w.sum():
            w = w / w.sum()
        first[xx] = xmin
        weights[xx, :xmax] = w
    fixed = np.where(weights < 0, np.trunc(-0.5 + weights * (1 << precision)), np.trunc(0.5 + weights * (1 << precision)))
    return first, fixed


_BOX_CACHE = {}


def _box_taps(in_size, out_size):
    """Cached per axis -> (i0, i1) source pixels per output pixel, or None. Pillow's BOX weights are 0 or 1 before
    normalising (a source pixel counts when its centre is inside the output pixel's box), so when every output pixel
    covers one source pixel or two neighbours - always when shrinking by at most 2x - the 22-bit fixed-point sum
    (2**21 + (a + b) * 2**21) >> 22 is exactly (a + b + 1) >> 1, and one source pixel gives itself (i1 = i0)."""
    key = (in_size, out_size)
    if key not in _BOX_CACHE:
        first, fixed = _box_coefficients(in_size, out_size)
        nz = fixed != 0
        cnt = nz.sum(1)
        k = nz.argmax(1)
        rows = np.arange(out_size)
        one = (cnt == 1) & (fixed[rows, k] == 1 << 22)
        two = (cnt == 2) & (fixed[rows, k] == 1 << 21) & (fixed[rows, np.minimum(k + 1, fixed.shape[1] - 1)] == 1 << 21)
        _BOX_CACHE[key] = (first + k, first + k + (cnt == 2)) if (one | two).all() else None
    return _BOX_CACHE[key]


def resize_box(rgb, out_w, out_h):
    """Shrink an RGB uint8 image (by at most 2x per axis) like Pillow's Image.resize(..., Image.BOX): same weights, same
    22-bit fixed-point rounding, horizontal pass then vertical pass through an 8-bit intermediate. Bit-identical to
    Pillow 13 for the detector's pop-up box at its scales (unit-tested); Pillow itself may do the vertical pass first on
    tall images. Pillow is not part of the release (it would add unsigned binaries), so the detector uses this."""
    h, w = rgb.shape[:2]
    hpairs = _box_taps(w, out_w) if out_w != w else None
    vpairs = _box_taps(h, out_h) if out_h != h else None
    if (out_w != w and hpairs is None) or (out_h != h and vpairs is None):
        raise ValueError(f"resize_box only shrinks by up to 2x: {w}x{h} -> {out_w}x{out_h}")
    out = rgb.astype(np.uint16)                                  # sums of two 8-bit values; results stay <= 255
    if hpairs is not None:
        i0, i1 = hpairs
        flat = out.reshape(h, w * 3)
        ch = np.arange(3)
        out = ((flat.take((i0[:, None] * 3 + ch).ravel(), axis=1) + flat.take((i1[:, None] * 3 + ch).ravel(), axis=1)
                + 1) >> 1).reshape(h, out_w, 3)
    if vpairs is not None:
        i0, i1 = vpairs
        out = (out.take(i0, axis=0) + out.take(i1, axis=0) + 1) >> 1
    return out.astype(np.uint8)


def _window_sums(a, h, w):
    """Sum over every h x w window ('valid'), from an integral image: fftconvolve(a, ones((h, w)), 'valid') up to
    rounding, without two FFTs."""
    c = np.zeros((a.shape[0] + 1, a.shape[1] + 1))
    c[1:, 1:] = a.cumsum(0).cumsum(1)
    return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]


def robust_correlation_at(mask, blurred_template, sigma):
    """`robust_correlation` (the same score, to ~1e-14) AND where its best window is -> (score, y, x) in `mask`
    coordinates. Built for scanning a whole region quickly: only the part of the mask with ink plus a template-sized margin
    is correlated (every window that can touch ink stays inside it), and the window sums come from integral images."""
    from scipy.ndimage import gaussian_filter
    ys, xs = np.nonzero(mask)
    th, tw = blurred_template.shape
    if not len(ys):
        return 0.0, 0, 0
    y0, x0 = max(0, ys.min() - th - 4), max(0, xs.min() - tw - 4)
    img = gaussian_filter(mask[y0:ys.max() + th + 5, x0:xs.max() + tw + 5].astype(np.float64), sigma)
    if img.shape[0] < th or img.shape[1] < tw:
        return 0.0, 0, 0
    tpl = blurred_template
    n = tpl.size
    ts = tpl.sum()
    tvar = (tpl * tpl).sum() - ts * ts / n
    ws = _window_sums(img, th, tw)
    ws2 = _window_sums(img * img, th, tw)
    num = fftconvolve(img, tpl[::-1, ::-1], mode="valid") - ws * ts / n
    wvar = ws2 - ws * ws / n
    ok = wvar > 5e-4 * n
    corr = np.where(ok, num / np.sqrt(np.maximum(tvar * wvar, 1e-9)), 0.0)
    if not corr.size:
        return 0.0, 0, 0
    y, x = np.unravel_index(np.argmax(corr), corr.shape)
    return float(min(corr[y, x], 1.0)), int(y0 + y), int(x0 + x)


def blur_template(template, sigma):
    """Template prepared for `robust_correlation`: padded so the blur can spread, then Gaussian-blurred."""
    from scipy.ndimage import gaussian_filter
    return gaussian_filter(np.pad(template.astype(np.float64), 2), sigma)


def robust_correlation(mask, blurred_template, sigma):
    """
    Compression-tolerant glyph score: the same zero-mean normalized correlation as `best_template_correlation`, but
    computed between the Gaussian-blurred mask and the equally blurred template, so a glyph edge that compression or
    resampling moved by a fraction of a pixel still overlaps. General NCC (valid for non-binary values). -> best score.
    """
    from scipy.ndimage import gaussian_filter
    img = gaussian_filter(mask.astype(np.float64), sigma)
    tpl = blurred_template
    n = tpl.size
    ts = tpl.sum()
    tvar = (tpl * tpl).sum() - ts * ts / n
    ones = np.ones_like(tpl)
    ws = fftconvolve(img, ones, mode="valid")
    ws2 = fftconvolve(img * img, ones, mode="valid")
    num = fftconvolve(img, tpl[::-1, ::-1], mode="valid") - ws * ts / n
    wvar = ws2 - ws * ws / n
    ok = wvar > 5e-4 * n                       # flat windows cannot contain a glyph (and are numerically unstable)
    corr = np.where(ok, num / np.sqrt(np.maximum(tvar * wvar, 1e-9)), 0.0)
    return float(min(corr.max(), 1.0)) if corr.size else 0.0


# ==================================================================
# SCORING THE WHOLE VIDEO
# ==================================================================

def params_signature(templates):
    return repr((3, ROI_X, ROI_Y, ROI_W, ROI_H, MASK_MIN_RED_BLUE, MASK_GREEN_BELOW,
                 SCORE_EVERY_N_FRAMES, [(t["id"], t["array"].shape, float(t["array"].sum())) for t in templates],
                 VIDEO_FILE.stat().st_size))


def probe_fps(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
         "-of", "default=nw=1:nk=1", str(video)], capture_output=True, text=True, check=True).stdout.strip()
    num, den = out.split("/")
    return float(num) / float(den)


def score_video(templates, rescan=False):
    """
    Per-frame best chest-word score for the whole video.
    -> (times, scores, ys, xs, which): `which` indexes `templates` (the winner),
    ys/xs are the centre of the winning match inside the ROI.
    """
    signature = params_signature(templates)
    if SCORE_CACHE.exists() and not rescan:
        cached = np.load(SCORE_CACHE, allow_pickle=False)
        if str(cached["signature"]) == signature:
            print(f"Using cached visual scores ({SCORE_CACHE}); --rescan to recompute.")
            return cached["times"], cached["scores"], cached["ys"], cached["xs"], cached["which"]

    fps = probe_fps(VIDEO_FILE)
    print(f"Scoring video frames ({fps:.2f} fps, every {SCORE_EVERY_N_FRAMES}nd frame, "
          f"{len(templates)} templates) - a few minutes...")
    process = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(VIDEO_FILE), "-vf",
         f"{scale_filter()},crop={ROI_W}:{ROI_H}:{ROI_X}:{ROI_Y}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, bufsize=10 ** 8)
    frame_bytes = ROI_W * ROI_H * 3
    scores, ys, xs, which, index = [], [], [], [], 0
    while True:
        raw = process.stdout.read(frame_bytes)
        if len(raw) < frame_bytes:
            break
        if index % SCORE_EVERY_N_FRAMES == 0:
            mask = lavender_mask(np.frombuffer(raw, np.uint8).reshape(ROI_H, ROI_W, 3))
            best = (0.0, 0, 0, 0)
            # nearly empty mask -> nothing text-like on screen, skip the FFT
            if mask.sum() >= 20:
                for k, t in enumerate(templates):
                    score, y, x = best_template_correlation(mask, t["array"])
                    if score > best[0]:
                        h, w = t["array"].shape
                        best = (score, y + h // 2, x + w // 2, k)
            scores.append(best[0]); ys.append(best[1]); xs.append(best[2]); which.append(best[3])
        index += 1
    times = np.arange(len(scores)) * SCORE_EVERY_N_FRAMES / fps
    scores = np.array(scores, dtype=np.float32)
    ys, xs, which = np.array(ys), np.array(xs), np.array(which)
    OUTPUT_DIR.mkdir(exist_ok=True)
    np.savez(SCORE_CACHE, times=times, scores=scores, ys=ys, xs=xs, which=which, signature=signature)
    return times, scores, ys, xs, which


def detect_events(times, scores, ys, xs, high=EVENT_HIGH_THRESHOLD, low=EVENT_LOW_THRESHOLD):
    """
    Fires on a new popup, which is either:
      * the score rising above `high` while armed (re-armed after the score has
        stayed below `low` for EVENT_REARM_HOLD_SECONDS), or
      * the matched word jumping EVENT_POSITION_JUMP_PIXELS or more while the
        score stays high (a second popup replacing the first).
    Never fires twice within EVENT_MIN_SEPARATION_SECONDS.
    """
    events, armed, low_since, last_high_pos = [], True, None, None
    for t, s, y, x in zip(times, scores, ys, xs):
        spaced = not events or t - events[-1] >= EVENT_MIN_SEPARATION_SECONDS
        if s >= high:
            jumped = (last_high_pos is not None
                      and max(abs(y - last_high_pos[0]), abs(x - last_high_pos[1])) >= EVENT_POSITION_JUMP_PIXELS)
            if spaced and (armed or jumped):
                events.append(t)
                armed = False
            last_high_pos = (y, x)
            low_since = None
        elif s < low:
            low_since = t if low_since is None else low_since
            if not armed and t - low_since >= EVENT_REARM_HOLD_SECONDS:
                armed, last_high_pos = True, None
        else:
            low_since = None
    return np.array(events)


def popup_event_details(times, scores, ys, xs, which, templates, events):
    """Raw evidence for each popup event: peak score within 1 s of the onset, which template, where."""
    details = []
    for t in events:
        window = (times >= t) & (times <= t + 1.0)
        k = int(np.argmax(np.where(window, scores, -1)))
        details.append({"time": float(t), "score": float(scores[k]), "y": int(ys[k]), "x": int(xs[k]),
                        "template": templates[int(which[k])]["id"]})
    return details


# ==================================================================
# PLOT
# ==================================================================

def plot_scores(path, times, scores, truth, events, matches, tolerance, zoom, show):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(16, 5))
    for i, t in enumerate(truth):
        ax.axvspan(t - tolerance, t + tolerance, color="tab:green", alpha=0.18,
                   label="ground-truth chest +/- tolerance" if i == 0 else None)
        ax.axvline(t, color="tab:green", linewidth=1.0)
    ax.plot(times, scores, linewidth=0.7, color="tab:blue", label="best chest-word template score")
    ax.axhline(EVENT_HIGH_THRESHOLD, linestyle="--", color="tab:red", label="fire threshold")
    ax.axhline(EVENT_LOW_THRESHOLD, linestyle=":", color="tab:gray", label="re-arm threshold")
    matched = {m[1] for m in matches}
    for i, t in enumerate(events):
        ok = i in matched
        ax.scatter(t, 1.03, marker="v" if ok else "x", s=60, color="tab:green" if ok else "tab:red", zorder=4,
                   label=("event: matched chest" if ok else "event: false positive")
                   if i == next((j for j in range(len(events)) if (j in matched) == ok), -1) else None)
    ax.set_ylim(-0.05, 1.1)
    ax.set_xlabel("Time (seconds)")
    ax.set_ylabel("correlation with chest-word glyphs")
    ax.set_title("Visual detector: chest word in the XP popup (HD video)")
    ax.legend(loc="upper right", fontsize=8)
    if zoom:
        ax.set_xlim(*zoom)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    print(f"Saved {path}")
    if show:
        plt.show()
    plt.close(fig)


# ==================================================================
# OPTIONAL: AUDIO / VIDEO AGREEMENT CHECK
# ==================================================================

def agreement_check(truth, visual_events, tolerance):
    """Compare audio and visual false positives (no fusion rule is applied)."""
    from fortnite_chest_counter.detection import audio as detector

    audio_events, cand_times, _, _ = detector.audio_chest_events(truth)

    a_match, _, a_fp = match_events(truth, audio_events, tolerance)
    v_match, _, v_fp = match_events(truth, visual_events, tolerance)
    a_fp_times = audio_events[a_fp]
    v_fp_times = visual_events[v_fp]
    window = 1.0   # how close counts as "the same moment" for the agreement check

    def near(t, others):
        return len(others) > 0 and np.min(np.abs(others - t)) <= window

    print("\n=== AUDIO vs VISUAL AGREEMENT (no fusion rule; information only) ===")
    print(f"audio: {len(a_match)}/{len(truth)} chests, {len(a_fp)} false positives")
    print(f"video: {len(v_match)}/{len(truth)} chests, {len(v_fp)} false positives")
    both_fp = sum(near(t, v_fp_times) for t in a_fp_times)
    print(f"audio false positives that ALSO have a visual false positive within {window}s: {both_fp} of {len(a_fp)}")
    audio_support = sum(near(t, audio_events) for t in visual_events)
    print(f"visual events with an accepted audio event within {window}s: {audio_support} of {len(visual_events)}")
    print(f"audio events with a visual event within {window}s: "
          f"{sum(near(t, visual_events) for t in audio_events)} of {len(audio_events)}")
    # "agreement": any audio event and visual event within +/- window
    agreed = [t for t in visual_events if near(t, audio_events)]
    m, missed, fp = match_events(truth, np.array(agreed), tolerance)
    print(f"AUDIO AND VIDEO AGREE (visual events with an audio event within {window}s): "
          f"{len(m)}/{len(truth)} chests, {len(fp)} false positives")
    # looser: use ALL audio burst candidates (not only accepted ones) as the audio evidence
    agreed_any = [t for t in visual_events if np.min(np.abs(cand_times - t)) <= window]
    m2, _, fp2 = match_events(truth, np.array(agreed_any), tolerance)
    print(f"visual events with ANY audio 4-11 kHz burst candidate within {window}s: "
          f"{len(m2)}/{len(truth)} chests, {len(fp2)} false positives")


# ==================================================================
# MAIN
# ==================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rescan", action="store_true", help="ignore cached scores and re-read the video")
    parser.add_argument("--tolerance", type=float, default=EVALUATION_TOLERANCE_SECONDS)
    parser.add_argument("--zoom", type=float, nargs=2, metavar=("START", "END"))
    parser.add_argument("--fusion", action="store_true", help="also run the audio detector and compare")
    parser.add_argument("--no-show", action="store_true")
    args = parser.parse_args()
    if args.no_show:
        import matplotlib
        matplotlib.use("Agg")

    if not VIDEO_FILE.exists():
        raise FileNotFoundError(f"HD video not found: {VIDEO_FILE.resolve()}")
    truth = load_ground_truth(GROUND_TRUTH_FILE)
    print(f"Ground truth ({len(truth)} chests): " + ", ".join(f"{t:.2f}" for t in truth))

    templates = load_templates()
    for t in templates:
        print(f"Template {t['id']}: {t['array'].shape[1]}x{t['array'].shape[0]} px, {int(t['array'].sum())} lit pixels")
    times, scores, ys, xs, which = score_video(templates, args.rescan)

    print("\nPeak template score within [-0.75, +1.5] s of each labelled chest:")
    for i, t in enumerate(truth):
        window = (times >= t - 0.75) & (times <= t + 1.5)
        k = int(np.argmax(np.where(window, scores, -1)))
        english = CHEST_TEMPLATES[0]
        note = "  (in-sample: English template was cut from this popup)" \
            if abs(t - english["source_time"]) < 1.0 and english["source_video"] == VIDEO_FILE else ""
        print(f"  chest {t:7.2f}s  peak score {scores[k]:.2f}  template {templates[which[k]]['id']}{note}")
    print("Score percentiles over the whole video (50/90/99/99.9): "
          + " / ".join(f"{p:.2f}" for p in np.percentile(scores, [50, 90, 99, 99.9])))

    events = detect_events(times, scores, ys, xs)
    matches, _, _ = report(
        f"VISUAL DETECTOR (fire >= {EVENT_HIGH_THRESHOLD}, re-arm < {EVENT_LOW_THRESHOLD})",
        truth, events, args.tolerance)

    print("\nThreshold sweep (re-arm = fire - 0.25):")
    print("  fire   events  TP  FP")
    for high in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
        ev = detect_events(times, scores, ys, xs, high, high - 0.25)
        m, _, fp = match_events(truth, ev, args.tolerance)
        print(f"  {high:.2f}   {len(ev):5d}  {len(m):2d}  {len(fp):2d}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    plot_scores(OUTPUT_DIR / "visual_timeline_full.png", times, scores, truth, events, matches, args.tolerance,
                None, show=False)
    if args.zoom:
        plot_scores(OUTPUT_DIR / "visual_timeline_zoom.png", times, scores, truth, events, matches,
                    args.tolerance, tuple(args.zoom), show=not args.no_show)
    elif not args.no_show:
        plot_scores(OUTPUT_DIR / "visual_timeline_full.png", times, scores, truth, events, matches,
                    args.tolerance, None, show=True)

    if args.fusion:
        agreement_check(truth, events, args.tolerance)


if __name__ == "__main__":
    main()
