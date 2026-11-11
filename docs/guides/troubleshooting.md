# Troubleshooting

First step for anything unclear: in the app open **Help & diagnostics** -> **Run diagnostics** -> **Copy report** (also
saved to `logs\diagnostics.txt`). Include it, and any `logs\crash_*.txt`, when you
[open an issue](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/issues).

Setup-specific problems: [dual-PC SRT](obs-dual-pc-srt.md#troubleshooting) · [capture card](capture-card.md#if-the-picture-is-black).

## The counter does not get a picture

| Message or symptom | Fix |
|---|---|
| `OBS Virtual Camera was not found` | Open OBS Studio 28+, click **Start Virtual Camera**, then **Start counting** again. |
| `exists but is not sending video` | Click **Start Virtual Camera** again; check that `ChestDetectorFeed` is not black. |
| Live preview black or showing the wrong scene | Virtual Camera settings (gear): **Output Type** = **Scene**, **Output Selection** = **ChestDetectorFeed**. With Studio Mode, make sure the scene holding your gameplay source is live in **Program**. |
| Virtual Camera already used by something else | Use an **OBS projector window** of your gameplay source ([guide](obs-projector.md)), or the capture card directly ([capture-card guide](capture-card.md#advanced-the-capture-card-directly-without-obs)). |
| `The OBS projector window you picked is not open` | In OBS right-click the gameplay source > **Windowed Projector (Source)**, then **Refresh** and pick it. Tip: OBS > Settings > General > Projectors > **Save projectors on exit**. |
| `projector window is minimized` / black preview | Restore the projector window. It may be behind other windows, but not minimized. |
| `the projector window was closed, so counting stopped` | Reopen it in OBS and press **Start counting**. |
| `Could not open the video device` | The card is probably in use by OBS: close OBS or remove the card from it - or use the Virtual Camera or a projector. |
| `already running` / `already open` | Only one counter runs at a time; use the open one (taskbar). |
| `Could not open the livestream` | The stream must be live and public. Use the full link (`https://www.twitch.tv/CHANNEL`, a YouTube `watch?v=` link or `https://www.youtube.com/@CHANNEL/live`). |
| Livestream: *buffering* / *reconnecting* | Slow connection, an ad break or the stream restarted. The counter waits and continues by itself; counts are kept. |

## The picture is there, but counting looks wrong

| Symptom | Fix |
|---|---|
| Number never changes | Turn on **Live preview**: 16:9, full picture, HUD visible? Does the chest pop-up ("CHEST" / "BAÚ") appear in `ChestDetectorFeed`? Is **Settings -> Fortnite language** right for your game? |
| Many chests only in APPROX | Normal for compressed or larger-HUD video. Better picture quality (higher bitrate, clean feed) helps; Dashboard Approx contains estimated-only openings. The OBS APPROX figure includes confirmed openings. |
| Missed or wrong chests | Dashboard -> **Diagnostic recording** -> "Only around my marks", then press **Ctrl+Alt+M** right after each miss. Share that session's `captures\` folder in an issue ([details](diagnostic-capture.md)). |
| Count should start from an earlier number | Dashboard -> **Edit counts...**: Enter the chests already opened as estimated openings and leave confirmed openings at 0. **Reset...** sets both to 0. Both work while counting. |

## The number on stream is wrong or not updating

| Symptom | Fix |
|---|---|
| OBS text blank or not updating | The Text source must have **Read from file** ticked and point at the file shown on the app's **Overlay** page. |
| Text shows something different from the app | That Text source reads another file (for example an old copy of the app). Re-point it ([overlay guide](obs-overlay.md)). |
| OBS WebSocket: *Waiting for OBS* | OBS is closed, or **Tools > WebSocket Server Settings > Enable WebSocket server** is off, or the port differs (default 4455). |
| OBS WebSocket: *Password problem* | Copy the password again from OBS (**Show Connect Info**), paste it on the Overlay page, click **Connect**. |
| OBS WebSocket: *the source reads a file* | That Text source has **Read from file** ticked, so OBS ignores direct updates: click **Use direct updates**, or pick another source. |
| OBS WebSocket: text flickers between two values | Another tool also updates that Text source. Give each tool its own source. |
| Numbers wrong after moving the app to a new folder | Copy `state\` and `config.toml` from the old folder and re-point the OBS Text source. |

## Still stuck

Give the [README](../../README.md) and the guide for your setup to an AI assistant and ask it to walk you through it one
step at a time - the README's "Getting setup help from AI" section lists the facts it needs. Or open an issue with the
diagnostics report.
