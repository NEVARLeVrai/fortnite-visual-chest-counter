# Change and validation notes

The **Setup** page guides capture, automatic match detection, overlay and Discord configuration. Replay verification is enabled by default and can be turned off in Settings. See the [setup guides](guides/index.md).

This is the living implementation and review document for the desktop pull request. Update this page when the branch
changes; the pull request links here. Versioned release notes describe published releases.

## Changes

- Add the Stats page with date and mode filters, chest-type and mode breakdowns, a five-match moving average and clickable per-match details. Add Discord `/stats` for finished group or player results and make `/match` complete its initial interaction directly.
- Adapt the upstream English small-HUD detector improvements for compressed 720p footage and deduplicate pop-ups briefly obscured by the map.

- Show the verified replay count as confirmed in the Dashboard and personal overlay at the lobby. Keep the original live count in Compare replay and leave the next match’s counter independent.

- Show synchronized chests, matches and Pirate Token odds above the Discord ranking. Count one result per
  player and match ID in the ranking, and retain older unlinked results. Add
  `/match` for paginated shared history and per-match summaries. Chest types remain available in `/chests`.
- Share the Pirate Token calculation across Dashboard, overlays and Discord. Keep the saved cumulative totals
  stable during OBS updates and synchronize verified match corrections to Guests without duplicating a result.

- Local screen and window capture read Fortnite's HUD scale at the start of each session and try the nearest
  calibrated text size first. Image matching still chooses the winning size. The session log records both scales
  when available. Recorded video, streams and capture devices use the image alone.

- Try one extra reduction for larger XP notification text, with the usual sizes tried first. The French
  qualified-line reader also uses it and keeps deduplication coordinates in the original capture.
  Brazilian Portuguese enlarged readings require a sharp glyph and a chest-object prompt.
  Detection uses the captured text so the same logic works on local recordings and capture cards.
  Weak matches at the widest scale leave the usual French XP reading in place.

- Recover faint French chest popups from three aligned `COFFRE` and XP readings, as estimated openings.
  Also read a bare `COFFRE` below another reward such as `MUNITIONS AU MAX` when the XP header and chest line align across three frames. Reuse the existing overlap check before adding it as an estimated opening.
  Read the actual digit on qualified rare rows through X4 so a temporary unreadable multiplier does not recount the stack.

- Display each player’s cumulative Pirate Token estimate on the public Discord board and in `/chests`. Detailed chest breakdowns are in `/chests`. The Group leaderboard ranks chests from matches shared to Discord. Distinguish it from the Match history + live overlay total.
- Recover French chest types from three consistent readings around a counted popup, including weaker frames and
  neighbouring HUD scales. These readings affect the type only. The opening threshold and confidence rules stay the same.

- Start each overlay section with its chest total, followed by details and Pirate Token odds. The Team section
  includes each member’s count through `{member_totals}`. Section templates support inline `**bold**` in the
  graphical overlay and blank lines in both the graphical and OBS outputs. Previous default templates upgrade automatically.

- Add French HUD detection and multi-frame classification for normal, Rare and Sprite chests. Deduplicate
  overlapping readings and retain an unknown type for ambiguous openings.
- Follow French qualified lines when notifications reflow around ammo/upgrade messages. Keep the XP gate and three
  stable readings, retain quantities across capture pauses, and rearm only after an observed disappearance.
  Keep a qualified chest's known type independent of another generic line as the stack moves.
- Keep one capture per app. Improve HDR-to-SDR handling and preview delivery, keep the original SDR colour path,
  and cap live preview at 5 FPS independently of the detector's 5/10/15 FPS settings.
- Close video pipes and reap stopped FFmpeg encoders and decoders, including command-wrapper shutdown, so recording
  and review cleanup does not race Windows file locks. Keep the console stop path nonblocking and bound stop waits.
- Replay diagnostics using the original recorded frame intervals. Keep video playback positions separate from the
  detector clock, so irregular live capture does not change temporal confidence or deduplication rules on replay.
- Canonicalize process paths during update safety checks, so Windows short-name and junction aliases cannot hide
  a running program inside the runtime or staged package.
- Separate confirmed openings, estimated-only openings and Total in the Dashboard. Use Total and the manual
  multiplier for the Pirate Token estimate.
- Use a Material 3-inspired Qt interface with Google Sans, a left navigation rail, connected actions, themed
  dialogs and animated chest variants. Configure overlay wording, chest types, drop chance, saved match count and
  screen-share visibility.
- Add local match history, batch deletion, local log-based start and finish, and automatic mode detection with
  a manual selector.
- Put sharing and bot setup together in Group. Add Host and Guest roles, one published leaderboard, Discord
  match reports and team matching by verified session fingerprint.
- Stagger Group clients and share a webhook cooldown across submissions and shared-board requests. Respect Discord retry/reset delays, retain queued results and avoid blocking the UI.
- Share coalesced current-match updates, recover edited live feed messages after reconnect and keep final saved results authoritative. Add matching team participants and totals to local history.
- Show personal, verified team and cumulative match totals with boost-aware Pirate Token estimates. Add overlay transparency and custom headings and multiline templates for Personal, Team and All matches, including cumulative match count.
- Use one section editor and renderer for the floating overlay, Dashboard preview and managed OBS text file. Remove
  the old layout radios. Apply global optional-statistic checkboxes consistently and reconcile worker counts with
  the latest shared context without duplicating a saved/current match.
- Publish the same detailed leaderboard from the app's channel dropdown or Discord's `/leaderboard publish:true`. Preserve a separate shared data message for Guest sync.
- Add a README banner and badges, preview assets and explicit component acknowledgements. Configure bot and webhook branding separately and rotate the bot presence through live team and saved group statistics.
- Apply bot username, avatar and banner explicitly. Keep the avatar chest centred and the banner text aligned;
  branding previews are labelled local simulations. Add Windows and Linux test jobs on pushes and pull requests.
- Add tray options, diagnostic cleanup and a Settings reset. Keep a console debug batch file and a buildable
  Windows development launcher.

## Defaults

The portable defaults mirror the verified setup in `config.example.toml`. Match automation and Auto mode are enabled,
the app opens normally and closes to the tray, and the on-screen overlay is visible in screen sharing. Group sharing remains
off. Automatic match reports also require a configured bot and enabled sharing.
Session logs, candidate logs, detection snapshots and diagnostic video are all off by default and require opt-in.
The example configuration is checked against the code defaults. Portable builds include no saved configuration,
credentials, player names, counters, diagnostic recordings or sample match data.

## French HUD scaling

The French fallback now follows enlarged HUD text in a fixed coordinate plane at 0.9 and 0.85, instead of detecting
an initial blurred word and then trying to track it with native-size glyphs. Scale-specific votes remain separate;
normal, Rare and Sprite confidence checks are retained. On Lionel's 40 saved images held for repeated observations,
35 become typed Chest and five remain Unknown; these static probes cannot prove the number of live openings or
stack deduplication. No confidence threshold was lowered to promote these images to Certain.

## Diagnostics and HDR scheduling

Choosing Diagnostic recording **Off** also immediately disables session journals, candidate logs and PNG snapshots.
Existing diagnostic files are retained for investigation; Off stops new writes rather than deleting evidence.
The running worker reloads logging switches, and the Settings checkboxes reflect the saved choice.
HDR preparation runs in one additional worker thread, overlapping tone mapping with detection. Only the latest raw
capture and latest converted result are retained; old frames are skipped rather than queued. The source timestamps
and end-to-end drop counters are preserved. Preview-only capture does not start this converter, and SDR keeps its
existing path. A bounded 45-frame synthetic 720p benchmark with the real estimator measured about 16–22 FPS
sequentially and 25–28 FPS pipelined; this is processing capacity, not a live-capture rate guarantee.

The Windows counting worker uses the default process priority to limit contention with the game. Diagnostic encoding
runs below normal priority.

The updater now waits for a short stable ready state before accepting a new version, closing a race where an app
could report readiness and immediately exit before rollback was armed.

## French chest type refinement

- Use repeated high-contrast readings only to label an already-counted French notification. Leave qualified or adjacent ambiguous openings to the existing detector.
- Replayed the four retained HDR excerpts that previously showed an unknown type. All four classify as normal with the same count, onset and confidence tier.

## Validation

- Replayed all 13,248 recorded frames of a local French HDR session with original frame timings. The earlier
  detector found 31 openings (18 normal, 7 unknown, 5 rare, 1 Sprite). The corrected detector found 32
  (26 normal, 5 rare, 1 Sprite). Visual inspection verified the added normal opening and the X1–X4 burst
  followed by one Sprite chest. These replay results do not establish the live detection rate.

- Detector development replayed 11 diagnostic recordings comprising 20,455 analysed frames. A rare-chest replay
  recovered four Rare openings and one Sprite opening. A separate replay counted a normal opening once after
  deduplication. A normal French X2 pop-up first seen with its multiplier already present can still count as one.
- Two local Fortnite logs from the same pair of Duo matches yielded two matching session fingerprints and detected
  Duo on both PCs. Automated tests exercised local history, mode selection, Discord publication and settings sync
  using simulated Discord responses.
- App previews capture the current interface with sample data. The Discord leaderboard and profile previews are locally rendered simulations, with the leaderboard using the actual embed payload.
- A further local HDR recording contains 9,168 frames. Both the unchanged baseline and the corrected full replay
  count 31 openings; the visually checked burst is exactly four Rare chests and one Sprite chest, with no extra
  count from ammo, fading notifications or inventory. One opening logged live is absent in both replays near a
  weak glyph threshold in the standard MJPEG recording; this is not counted as a new detector regression or hidden
  by lowering confidence thresholds. Static saved images additionally exercise the previously clipped X2 layouts. The final
  enlarged-HUD change was also checked on the 300-frame Rare/Sprite burst: exactly four Rare plus one Sprite,
  all Certain.
- Before release, repeat tests with an installed Windows build, HDR and SDR capture, Discord screen sharing and
  a live Host/Guest setup. Live performance and Discord delivery need separate verification.

The initial work used Codex with GPT-6.1 Sol at medium reasoning effort. Maintainers should independently replay
their own recordings before merging detector changes.

## Acknowledgements

See [project, artwork and dependency credits](ACKNOWLEDGEMENTS.md).

## References

- [Material Design 3](https://m3.material.io/)
- [Chest Tracker](https://github.com/Xxoris190/chest-tracker)
- [Google Sans](https://github.com/googlefonts/googlesans)
- [Material Icons](https://github.com/google/material-design-icons)
- [Independent Pirate Token calculator](https://piratetoken.quest/drop-rate/)
