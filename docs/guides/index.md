# Setup guides

Open [**Stats**](stats.md) to track progress, compare modes and inspect a saved match from the chart.

In the app, select **Setup** above **Dashboard** for shortcuts to each configuration page.

## First run

1. Open **Dashboard → Capture settings** and choose your Fortnite screen. Use **Live preview** to check the picture. If HDR looks washed out, try the **HDR → SDR** source.
2. Open **Settings**, choose the language used in Fortnite, and save. For automatic history, enable automatic match detection and turn on **Record Replays** inside Fortnite. Return to the lobby after the first test match and check **Group → Match history**.
3. Open **Overlay** to choose which counters, team stats and odds appear. To use OBS, add its text source and select the output file shown in the app.
4. For group play, each player sets their own name and uses the shared webhook in **Group**. The host configures the bot and selects where the leaderboard is published. See [Matches and Discord](matches-and-discord.md) for the host and guest walkthrough.

## Capture options

Every setup ends the same way: the app reads a video picture and keeps the count (and writes it to a text file that an OBS
Text source can show on stream). Most people need one of the first two guides - no OBS needed:

| What you want to count | Guide | Tested |
|---|---|---|
| **A Twitch or YouTube livestream** (yours or anyone's) - paste the link | [Livestream URL](livestream-url.md) | **Yes** - incl. a 3-hour session, 2026-11-08 |
| **Anything on a screen** - a stream or video playing fullscreen, or your own game | [Screen](screen-capture.md) | **Yes** - 2026-11-08 |
| Your own stream: Fortnite and OBS on one PC | [Single PC with OBS](obs-single-pc.md) | Yes |
| Gaming PC + streaming PC on the same network, **no capture card** | [Dual PC over the network (SRT)](obs-dual-pc-srt.md) | **Yes** - 2026-11-07, v0.2.0 |
| A video file | [Recorded video](recorded-video.md) | Yes |
| Console or gaming PC + capture card + streaming PC | [Capture card](capture-card.md) | *Less tested:* counter side yes; not yet with a physical card |
| Capture card **owned by OBS** and **Virtual Camera used by another program** | [OBS projector window](obs-projector.md) | *Less tested:* window capture yes (stand-in window); not yet with a real OBS projector |

Then see [overlay and OBS](obs-overlay.md), [matches and Discord](matches-and-discord.md) or [troubleshooting](troubleshooting.md).

Start with the [README](../../README.md) for installation and the chest counter definitions.
