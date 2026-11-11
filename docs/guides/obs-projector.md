# OBS projector window (capture card and Virtual Camera already in use)

For streamers whose setup is already "full": **OBS owns the capture card**, and **OBS Virtual Camera is used by another
program** (or shows something other than your gameplay). The counter then reads your gameplay from an **OBS projector
window** of the gameplay source - no second opening of the capture card, no Virtual Camera, no plugin, and your stream
stays exactly as it is.

> **Status (v0.2.1):** the window capture is tested end to end with a stand-in window on our development PC (15 pictures
> per second, nothing dropped, the same chests counted as from the video file). It has **not yet been tested with a real
> OBS projector and a physical capture card** - please [tell us](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/issues)
> how it works for you, with the app's diagnostics report.

```text
Capture card --> OBS (your scenes and stream, unchanged)
                  └─ "Windowed Projector (Source)" of the gameplay source  (a window on your PC)
                        └─ Fortnite Visual Chest Counter reads that window  -->  OBS Text source on your stream
```

## Set it up (once)

1. In OBS, right-click your **gameplay source** (for example the capture card's Video Capture Device) - in the Sources
   list or in the preview - and choose **Windowed Projector (Source)**. A window opens showing only that source.
2. Make that window **at least 1280x720** (bigger is fine; any shape works - black bars are cut off automatically).
3. Recommended: OBS > **Settings > General > Projectors** > tick **Save projectors on exit**, so the projector reopens
   every time OBS starts.
4. Open **Fortnite Visual Chest Counter.exe**, click **Refresh**, and pick **OBS projector: Windowed Projector (Source) -
   *your source*** in the **Video source** list.
5. Click **Start counting**. Turn on **Live preview** once to check that the whole picture is your gameplay with the HUD.
6. Show the numbers on stream: [OBS overlay guide](obs-overlay.md).

## While you stream

- The projector window **may stay behind other windows** (behind OBS, your browser, ...). It must **not be minimized**:
  a minimized window has no picture. If that happens the app warns you and counts again as soon as the window is back.
- **Closing the projector** (or OBS) stops counting, with a message. Open the projector again and press **Start counting**.
- The projector shows the source itself, not your live scene: switching scenes, a "Be right back" screen or overlays on
  your stream do not change what the counter sees.
- If you rename the gameplay source, its projector gets a new title: pick it again in the Video source list.

## Why this and not ...

| Alternative | Why not here |
|---|---|
| OBS Virtual Camera | Already used by another program (if it already shows your gameplay, try the normal Virtual Camera setup first - several programs can often read it at once). |
| Opening the capture card in the app | Most capture cards allow only one program at a time, and OBS already has it. |
| Screen capture of a fullscreen projector | Works too ([single-PC guide](obs-single-pc.md#virtual-camera-already-used-by-another-program)), but needs a spare monitor kept free of other windows. |
| OBS WebSocket screenshots | Investigated: it would make OBS render, copy back and compress every analysed picture on request. Not offered until it is measured on a live stream. |

Problems: [troubleshooting](troubleshooting.md#the-counter-does-not-get-a-picture).
