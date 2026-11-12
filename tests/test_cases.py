"""
Real-world detection cases (tests/cases/cases.json, docs/reference/INVESTIGATION_2026-11-08.md) as permanent regression tests.

Each case's pop-up box masks (tests/fixtures/popups/<case>.npz, made by research/cases.py fixtures from the user's
diagnostic captures) are replayed through the real ChestEstimator. The pop-up channels work on that mask only, so the
pop-up decisions are the ones the full footage gives (research/cases.py run checks the full frames when the footage is
present). The other clues (prompt, sprite tag, ammo) see an empty picture here.

Every confirmed failure becomes a case: add it to cases.json, run `research/cases.py fetch` + `fixtures`, add a check.
Run:   python tests/test_cases.py     (or pytest)
"""

import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "src"))   # run without installing

import json
from pathlib import Path

import numpy as np

from fortnite_chest_counter import estimator as E
from fortnite_chest_counter import settings
from fortnite_chest_counter.detection import visual as V

HERE = Path(__file__).resolve().parent
CASES = json.loads((HERE / "cases" / "cases.json").read_text(encoding="utf-8"))["cases"]
BEFORE, AFTER = 0.5, 2.5


def frames(name):
    d = np.load(HERE / "fixtures" / "popups" / f"{name}.npz")
    h, w = (int(v) for v in d["shape"])
    frame = np.zeros((V.NORMALIZED_HEIGHT, V.NORMALIZED_WIDTH, 3), np.uint8)
    for t, packed in zip(d["times"], d["masks"]):
        mask = np.unpackbits(packed)[:h * w].reshape(h, w).astype(bool)
        roi = np.where(mask[..., None], np.array([232, 196, 250], np.uint8), np.array([40, 36, 60], np.uint8))
        frame[V.ROI_Y:V.ROI_Y + h, V.ROI_X:V.ROI_X + w] = roi
        yield float(t), frame


def replay(name, **channels):
    E.configure(settings.DEFAULTS["detector"])
    est = E.ChestEstimator(**channels)
    events = []
    for t, f in frames(name):
        events += est.process_frame(t, f)
    events += est.flush()
    return [e for e in events if e["approx_counted"]]


def score(name, counted):
    """-> (found, missed, duplicates, false) against the case's truth."""
    chests = [dict(r, hits=0) for r in CASES[name]["truth"] if r["what"] != "not_chest"]
    dup = false = 0
    for e in sorted(counted, key=lambda e: e["time"]):
        near = [c for c in chests if c["t"] - BEFORE <= e["time"] <= c["t"] + AFTER]
        free = [c for c in near if not c["hits"]]
        if free:
            min(free, key=lambda c: abs(c["t"] - e["time"]))["hits"] += 1
        elif near:
            dup += 1
        else:
            false += 1
    found = sum(1 for c in chests if c["hits"])
    return found, len(chests) - found, dup, false


def check(name, expect_found=None, **channels):
    counted = replay(name, **channels)
    found, missed, dup, false = score(name, counted)
    real = len([r for r in CASES[name]["truth"] if r["what"] != "not_chest"])
    want = real if expect_found is None else expect_found
    assert (found, dup, false) == (want, 0, 0), f"{name}: found {found}/{real}, missed {missed}, duplicates {dup}, false {false}"
    return counted


def test_fixtures_and_truth_exist_for_every_case():
    for name, case in CASES.items():
        if case.get("known_limitation"):
            continue
        assert (HERE / "fixtures" / "popups" / f"{name}.npz").exists(), name
        assert case["truth"] and all(r["what"] in ("new_popup", "new_line", "multiplier", "not_chest") for r in case["truth"])


def test_thiefs_small_hud_all_five_chests_and_only_strong_new_pop_ups_certain():
    counted = check("thiefs_small_hud")
    assert len(counted) == 5
    assert [e.get("increment", {}).get("kind") for e in counted] == [None, None, "new_line", "multiplier", "multiplier"]
    # M9H: a new small-HUD pop-up whose two small-word matches both reach SMALL_HIGH_* is CERTAIN (here 1.00 / 1.00, the pop-up the
    # template was cut from, and 0.90 / 0.98); stacked steps stay APPROX only, as before
    assert [e["certain_counted"] for e in counted] == [True, True, False, False, False]
    assert all(e["approx_counted"] for e in counted)
    assert all(e["scaled_popup"]["scale"] == E.SMALL_SCALE for e in counted)
    assert not replay("thiefs_small_hud", small_hud=False, stacked=False)                # v0.2.1: nothing at all


def _with(**constants):
    """Run with some estimator constants changed, then put them back."""
    class Patch:
        def __enter__(self):
            self.old = {k: getattr(E, k) for k in constants}
            for k, v in constants.items():
                setattr(E, k, v)

        def __exit__(self, *exc):
            for k, v in self.old.items():
                setattr(E, k, v)
    return Patch()


def test_small_hud_chests_on_a_720p_stream_are_found():
    """M9H: on a 720p stream the small word's exact match stayed at 0.68-0.73, under rc5's 0.75 trigger; the blurred match did not."""
    counted = check("thiefs_720p_stream_chests")
    assert not any(e["certain_counted"] for e in counted)                              # a 720p small word never reaches the CERTAIN bar
    with _with(SMALL_TRIGGER=0.75):
        assert not replay("thiefs_720p_stream_chests")                                 # rc5: all three missed


def test_a_pop_up_hidden_for_a_moment_by_the_map_counts_once():
    assert len(check("thiefs_720p_map_flash")) == 1
    with _with(SMALL_SAME_POPUP_SECONDS=0):
        assert len(replay("thiefs_720p_map_flash")) == 2                                # without the rule, the same pop-up twice


def test_strong_1080p_small_hud_pop_ups_are_certain_and_steps_stay_approx():
    counted = check("thiefs_1080p_small_hud")
    assert [e["certain_counted"] for e in counted] == [True, True, False, False, False, False, False]
    with _with(SMALL_HIGH_EXACT=2.0):
        assert not any(e["certain_counted"] for e in replay("thiefs_1080p_small_hud"))  # rc5: all APPROX


def test_small_hud_channel_alone_finds_the_two_new_pop_ups():
    counted = replay("thiefs_small_hud", stacked=False)
    assert len(counted) == 2 and score("thiefs_small_hud", counted)[2:] == (0, 0)       # stacked steps need the tracker


def test_normal_hud_x2_and_x3_steps_are_counted_once_each():
    counted = check("replays_stack_x2_x3")
    steps = [e for e in counted if e.get("increment")]
    assert [s["increment"]["kind"] for s in steps] == ["multiplier", "multiplier"]
    assert not any(s["certain_counted"] for s in steps)                                  # multiplier steps: APPROX only
    assert len(replay("replays_stack_x2_x3", stacked=False)) == 1                       # v0.2.1: the first chest only


def test_a_new_chest_line_keeps_the_normal_rules():
    counted = check("replays_new_chest_line")
    line = [e for e in counted if e.get("increment")]
    assert len(line) == 1 and line[0]["increment"]["kind"] == "new_line"
    assert line[0]["certain_counted"] == counted[0]["certain_counted"]                   # tiers as for a new pop-up


def test_a_non_chest_line_added_to_the_pop_up_does_not_count_again():
    check("replays_sprite_level_line")
    old = replay("replays_sprite_level_line", stacked=False)                             # v0.2.1's position re-fire
    assert score("replays_sprite_level_line", old)[2] == 1                               # counted the same chest twice


def test_e0015_case_and_storm_phase_and_weakpoint_lines_do_not_count():
    check("replays_e0015_duplicate")
    check("replays_six_chests")


def test_old_position_re_fire_double_counts_the_e0015_pop_up_under_noise_and_the_tracker_does_not():
    """Live E0015 (not reproducible from the MJPEG recording): the same pop-up counted twice when the score hovered at
    0.75 and the scale-in animation had moved the word by 8 px. Same trace + small noise, both rules."""
    d = np.load(HERE / "fixtures" / "popups" / "replays_e0015_duplicate.npz")
    h, w = (int(v) for v in d["shape"])
    tpl = V.load_templates()[0]["array"]
    trace = []
    for t, packed in zip(d["times"], d["masks"]):
        m = np.unpackbits(packed)[:h * w].reshape(h, w).astype(np.float32)
        s, y, x = V.best_template_correlation(m, tpl) if m.sum() >= 20 else (0.0, 0, 0)
        trace.append((float(t), s, y, x))
    rng = np.random.default_rng(7)
    twice = {True: 0, False: 0}
    for _ in range(600):
        noise = rng.normal(0, 0.015, len(trace))
        for jump in (True, False):
            od = E.OnsetDetector(V.EVENT_HIGH_THRESHOLD, V.EVENT_LOW_THRESHOLD, True)
            fires = [t for (t, s, y, x), n in zip(trace, noise) if od.update(t, s + n, y, x, allow_jump=jump)]
            twice[jump] += sum(1 for f in fires if 185.9 <= f <= 188.3) > 1
    assert twice[True] > 0 and twice[False] == 0, twice


def test_every_case_finds_every_chest_once_and_nothing_else():
    for name, case in CASES.items():
        if not case.get("known_limitation"):
            check(name)


def test_french_chest_popups_count_once_including_the_storm():
    # ARCHIS gameplay, Nov 7 at 49–55s (chest) and 33–39s (storm phase),
    # then Nov 9 at 294–300s (chest under a sprite XP line in the storm).
    defaults = settings.DEFAULTS["detector"]
    E.configure(dict(defaults, languages=["fr"]))
    try:
        estimator = E.ChestEstimator()
        events = []
        for t, frame in frames("french_chest"):
            events.extend(estimator.process_frame(t, frame))
        events.extend(estimator.flush())
    finally:
        E.configure(defaults)
    counted = [event for event in events if event["approx_counted"]]
    assert len(counted) == 2
    assert all(event["popup_template"] == "fr:COFFRE" for event in counted)
    assert counted[0]["time"] < 6 and counted[1]["time"] >= 12


def test_reduced_french_notifications_recover_both_chests_without_storm_false_positive():
    defaults = settings.DEFAULTS["detector"]
    E.configure(dict(defaults, languages=["fr"]))
    try:
        estimator = E.ChestEstimator(hud_scale=0.75)
        events = []
        for t, frame in frames("french_chest"):
            roi = frame[V.ROI_Y:V.ROI_Y + V.ROI_H, V.ROI_X:V.ROI_X + V.ROI_W]
            reduced = V.resize_box(roi, round(V.ROI_W * 0.8), round(V.ROI_H * 0.8))
            roi[:] = 0
            roi[:reduced.shape[0], :reduced.shape[1]] = reduced
            events.extend(estimator.process_frame(t, frame))
        events.extend(estimator.flush())
    finally:
        E.configure(defaults)
    counted = [event for event in events if event["approx_counted"]]
    assert len(counted) == 2
    assert counted[0]["time"] < 6 and counted[1]["time"] >= 12
    assert all(event["chest_type"] == "chest" for event in counted)
    assert all(event["scaled_popup"]["scale"] == E.LOCALIZED_SMALL_HUD_SCALE for event in counted)


def test_french_chest_types_are_confirmed_by_multiple_line_readings():
    from fortnite_chest_counter.detection import chest_types

    defaults = settings.DEFAULTS["detector"]
    E.configure(dict(defaults, languages=["fr"]))
    try:
        suffixes = chest_types.load_french_suffixes()
        for expected in (chest_types.NORMAL, chest_types.RARE, chest_types.SPRITE):
            estimator = E.ChestEstimator()
            th, tw = estimator.french_template.shape
            mask = np.zeros((V.ROI_H, V.ROI_W), dtype=np.float32)
            y, x = 80, 120
            mask[y:y + th, x:x + tw] = estimator.french_template
            if expected in suffixes:
                suffix = suffixes[expected]
                start = x + tw + (8 if expected == chest_types.SPRITE else 3)
                top = y - 2 if expected == chest_types.SPRITE else y
                mask[top:top + suffix.shape[0], start:start + suffix.shape[1]] = suffix
            frame = np.zeros((V.NORMALIZED_HEIGHT, V.NORMALIZED_WIDTH, 3), dtype=np.uint8)
            roi = frame[V.ROI_Y:V.ROI_Y + V.ROI_H, V.ROI_X:V.ROI_X + V.ROI_W]
            roi[mask > 0] = (232, 196, 250)

            estimator.process_frame(0, frame)
            assert estimator._label(("normal", estimator.tracked_run, "first"))[0] == chest_types.UNKNOWN
            events = []
            for i in range(1, 20):
                events.extend(estimator.process_frame(i / 15, frame))
            events.extend(estimator.flush())
            counted = [event for event in events if event["approx_counted"]]
            assert len(counted) == 1
            assert counted[0]["chest_type"] == expected
            assert counted[0]["type_evidence"]["votes"][expected] >= chest_types.VOTES_MIN
    finally:
        E.configure(defaults)


def test_french_rare_x2_counts_each_stable_opening_once():
    from collections import Counter

    from fortnite_chest_counter import paths
    from fortnite_chest_counter.detection import chest_types

    defaults = settings.DEFAULTS["detector"]
    E.configure(dict(defaults, languages=["fr"]))
    try:
        estimator = E.ChestEstimator()
        folder = paths.TEMPLATES_DIR / "chest/fr"
        templates = {name: np.load(folder / f"stack_{name}.npy") for name in ("exp", "rare", "x2", "sprite")}
        # A fallback reading of the first rare line can land on the qualifier.
        estimator.pending_scaled.append({"onset": -0.1, "y": 74, "x": 193, "template": "fr:COFFRE",
                                         "blur": 0.8, "scale": 1.0, "confirmed": True,
                                         "french_type": chest_types.RARE})
        layouts = [
            [("rare", 71, 152)],
            [("x2", 70, 146)],
            [("x2", 65, 144), ("sprite", 78, 144)],
            [("x2", 58, 144), ("sprite", 71, 144), ("rare", 85, 144)],
            [("x2", 58, 144), ("sprite", 71, 144), ("x2", 85, 144)],
        ]
        events = []
        for block, lines in enumerate(layouts):
            frame = np.zeros((V.NORMALIZED_HEIGHT, V.NORMALIZED_WIDTH, 3), dtype=np.uint8)
            for name, y, x in [("exp", lines[0][1] - 13, 170), *lines]:
                mask = templates[name]
                h, w = mask.shape
                frame[V.ROI_Y + y:V.ROI_Y + y + h, V.ROI_X + x:V.ROI_X + x + w][mask > 0] = (225, 190, 245)
            for i in range(8):
                events.extend(estimator.process_frame((block * 8 + i) / 15, frame))
        events.extend(estimator.flush())
        counted = [event for event in events if event["approx_counted"]]
        assert Counter(event["chest_type"] for event in counted) == {chest_types.RARE: 4, chest_types.SPRITE: 1}
        assert all(event["certain_counted"] for event in counted)
    finally:
        E.configure(defaults)


def test_french_recovery_and_late_blurred_match_share_an_opening():
    from unittest.mock import patch

    from fortnite_chest_counter.detection import chest_types

    defaults = settings.DEFAULTS["detector"]
    E.configure(dict(defaults, languages=["fr"]))
    try:
        est = E.ChestEstimator()
        roi = np.zeros((V.ROI_H, V.ROI_W, 3), dtype=np.uint8)
        mask = np.ones(roi.shape[:2], dtype=np.float32)
        with (patch.object(est, "_recover_french_line", side_effect=[
                  (73, 187, chest_types.NORMAL, True), None, None, None]),
              patch.object(est, "_scaled_best", side_effect=[
                  (0.70, 73, 187, 1.0, "fr:COFFRE", False),
                  (0.0, 0, 0, 1.0, None, False),
                  (0.0, 0, 0, 1.0, None, False),
                  (0.85, 73, 188, 0.9, "fr:COFFRE", False)])):
            for t in (0.0, 0.1, 0.7, 1.44):
                est._scaled_popup(t, roi, mask, False)
        counted = [event for event in est.flush() if event["approx_counted"]]
        assert len(counted) == 1
        assert counted[0]["certain_counted"]
    finally:
        E.configure(defaults)


def test_sharp_french_chest_can_be_confirmed_after_hud_rescaling():
    defaults = settings.DEFAULTS["detector"]
    E.configure(dict(defaults, languages=["fr"]))
    try:
        estimator = E.ChestEstimator()
        events = []
        for t, frame in frames("french_chest"):
            if t >= 12:
                break
            roi = frame[V.ROI_Y:V.ROI_Y + V.ROI_H, V.ROI_X:V.ROI_X + V.ROI_W]
            enlarged = V.resize_box(roi, round(V.ROI_W / 0.9), round(V.ROI_H / 0.9))
            roi[:] = enlarged[:V.ROI_H, :V.ROI_W]
            events.extend(estimator.process_frame(t, frame))
        events.extend(estimator.flush())
    finally:
        E.configure(defaults)
    counted = [event for event in events if event["approx_counted"]]
    assert len(counted) == 1 and counted[0]["certain_counted"]
    assert counted[0]["scaled_popup"]["confirmed"]


def test_five_chest_vault_counts_every_step_including_the_third_lines_x2():
    counted = check("replays_vault_five_chests")
    assert sorted((e.get("increment") or {}).get("kind") or "new" for e in counted) == \
        ["multiplier", "multiplier", "new", "new_line", "new_line"]


def test_a_late_exact_match_on_a_pop_up_already_counted_is_the_same_pop_up():
    counted = check("replays_chest_x2_late_exact")                     # scale-aware onset + X2, no third count
    assert len(counted) == 2
    old = replay("replays_chest_x2_late_exact", stacked=False)
    assert len(old) == 2 and not any(e.get("increment") for e in old)  # v0.2.1: right total, for the wrong reason


def test_non_chest_lines_and_an_unchanged_pop_up_are_not_counted_again():
    for name in ("replays_weakpoint_line", "replays_static_popup_refire"):
        assert len(check(name)) == 1
        _, _, dup, false = score(name, replay(name, stacked=False))
        assert dup + false == 1, name                                    # v0.2.1 counted the same chest twice


def test_portuguese_only_settings_keep_the_position_re_fire_and_no_new_channels():
    E.configure(dict(settings.DEFAULTS["detector"], languages=["pt"], medium_counts_alone_languages=[]))
    try:
        est = E.ChestEstimator()
        assert est.small is None and not est.stacked and est.popup.use_position
    finally:
        E.configure(settings.DEFAULTS["detector"])


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:
            failed += 1
            import traceback
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} case checks passed")
    sys.exit(1 if failed else 0)
