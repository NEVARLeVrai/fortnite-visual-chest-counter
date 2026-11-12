# Diagnostic capture and review

Goal: turn every live miss (or wrong count) into a **repeatable, labelled test case**, so detector changes - and later
Sprite Chests, rewards and match tracking - are measured on real footage instead of tuned on single examples.

```text
live session (app or console) --[capture on]--> captures/<session>/  video + every decision + marks
                                                      |
                     a person fills labels.csv ("truth") while watching the clips
                                                      |
             `review` replays every clip through the CURRENT detector -> review.txt / review.json
             (live vs replay vs labels: found, missed, wrongly counted)
```

## Using it

Session logs, rejected-candidate logs, popup snapshots and diagnostic video recording are **off by default**.
Enable only the diagnostics you need in Settings before reproducing a problem. Counts, saved state and OBS output
continue updating with diagnostics off. Existing saved preferences are kept. Selecting **Off (stop diagnostic files)** on the Dashboard immediately
turns off video recording, session journals, candidate logs and popup snapshots. The running counter reloads the
logging switches; saved diagnostic files remain available. Text logging can be enabled separately in Settings.

* App: Dashboard -> **Diagnostic recording** (Off / only around my marks / around chest decisions and marks / whole
  session) - switchable while counting: the worker gets `capture <mode>` on its control pipe, the old recording finishes
  in a background thread (its encoder flush never holds up counting) and the new one starts in its own folder. Press
  **Ctrl+Alt+M** (global hotkey, works while Fortnite has the focus) or **Report missed chest** right after a miss; both are
  available only while a recording runs (without one there is no video to keep, so the app says so instead).
  Settings -> **Review captures...** runs `review --gui`: capture N of M, video done / total, time left from measured
  throughput (`progress.py`: no estimate until a few seconds and a few percent are measured), Stop, failed/finished state.
* Console: `[capture] mode = "marks" | "events" | "session"` in config.toml, key **M** to mark,
  `python -m fortnite_chest_counter review [folder] [--current-settings]`. A console mark without a recording is still
  written to the session log.

## What is recorded

Exactly the frames the detector analysed: the normalized 1280x720 RGB picture at the detector cadence (15 fps), after
counting, through a bounded queue to a separate FFmpeg encoder. Counting never waits for the recording; a full queue
skips the frame for the recording only (`frames_not_recorded` in capture.json). Disk: refuses to start below 2 GB free,
stops recording below 1 GB. One folder per session (layout in `capture.py`'s docstring): `capture.json`, `video/`
(10-s segments, `clips.json`, an `.ffconcat` playlist per clip), `timeline.csv` (recording frame -> live time),
`events.jsonl`, `marks.jsonl`, `labels.csv`, `README.txt`.

Console marks use `mark_before_seconds` (30) and `mark_after_seconds` (10) by default. The app’s missed-chest report uses six seconds before and four seconds after.
Events mode also keeps, per chest decision, `before_seconds` (10) / `after_seconds` (10). In both, segments nothing claims
are deleted about 40 s after they were recorded (a rolling buffer of ~50 s on disk: ~140 MB standard, ~300 MB lossless).

## Format choice (measured)

Measured 2026-11-07: the detector's own frames (English clip, full 8.5 min; PS5 clip 55-200 s) were recorded with each
candidate, replayed with a fresh estimator, and every decision compared with direct analysis of the same frames
(match = same source, tier and counts within 0.6 s).

| Format (`[capture] quality`) | English clip | PS5 clip | max pop-up score change | Size |
|---|---|---|---|---|
| FFV1 lossless RGB (`lossless`) | **6/6** identical | **6/6** identical | 0.000 | ~22 GB/hour |
| MJPEG 4:4:4 q2 (`standard`) | 5/6 (one HIGH -> CERTAIN) | 5/6 + 1 extra (one MEDIUM approx lost) | 0.032 | ~10 GB/hour |
| MJPEG 4:4:4 q4 | 4/6 | 5/6 + 1 extra | 0.108 | ~6.4 GB/hour |
| MJPEG 4:2:0 q2 | 4/6 | 5/6 + 1 extra | 0.075 | ~7.2 GB/hour |
| H.264 (OpenH264, 8 Mbps, 4:2:0) | not measured: the development FFmpeg has no OpenH264 | | | |

The 7-px pop-up word is so small that every lossy format moves its match score a little, which flips decisions sitting
within a few hundredths of a tier threshold; chroma subsampling (4:2:0) hurts more than JPEG quantisation. Hence:
`standard` (default) for everyday use - the chest is clearly visible and replays agree on everything not at a
threshold - and `lossless` when the replay must reproduce the live decision exactly. Whatever the format, replaying the
SAME capture is deterministic, so comparing detector versions on a capture is always exact. Intra-only codecs also cut
into 10-s segments at any frame. Encoder load: about 0.1 core (MJPEG) / 0.3 core (FFV1) at 15 fps 720p.

Events mode does not keep video around generic button prompts (pickups, ammo boxes ...: frequent, not chest decisions);
they are still listed in events.jsonl.

## Labels and review

`labels.csv` (UTF-8 with BOM, opens in Excel): one row per decision and mark; fill `truth` with `chest`, `not_chest`,
`missed_chest`, `sprite_chest`, `reward`, `other`. For a mark, move `live_time` to the moment the chest really opened;
add `kind=missed` rows for chests nobody marked. `review` matches decisions within 0.6 s (as `research/regress.py`) and
reports, for the live run and for the replay: real chests found as CERTAIN / counted at all, and labelled not-a-chest
items that were counted. By default the detector settings recorded with the capture are used, so a review isolates
code changes; `--current-settings` uses config.toml instead.

Each clip is replayed from its first frame with a fresh estimator; events-mode clips start >= 10 s before a decision, so
the evidence windows (prompt history 2.5 s, popup look-ahead 1 s, prompt finish 5.6 s) are complete.

## Next: a dataset

Collect sessions from different sources (OBS feed, capture card, YouTube / Twitch VODs, low bitrates), label them, and keep
them outside Git (they are large). A future `research/` harness can then run `review` over the whole set and diff against
a stored result, the same way `regress.py` guards the two original recordings today.


### Report a missed chest

In Dashboard → Capture settings, select **Only around my marks**. While counting, use **Report missed chest** in Capture health or press **Ctrl+Alt+M** just after the opening. This marks six seconds before and four seconds after the report. Clips retain the intersecting video segments, so the exported clip can be longer. Each report stores the HUD scale detected by the worker, the target and latest measured analysis FPS, and the skipped-frame count in `marks.jsonl`. The rolling recorder requires explicit activation and stays off by default.

After stopping the recording, use **Dashboard → Capture health → Export chest diagnostic**. Select the finished capture folder and the report to create a ZIP containing its video segments, the corresponding detection decisions and a small report with the app version and capture measurements. The video can show in-game names or chat. The ZIP excludes your app configuration and session logs.
