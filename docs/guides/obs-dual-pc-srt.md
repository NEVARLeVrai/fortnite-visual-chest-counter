# Dual-PC setup over your local network (SRT)

Run the counter on your **streaming PC**, with **no capture card**. The gaming PC's OBS sends its picture over your local
network with SRT (built into OBS), the streaming PC's OBS receives it, and the counter reads it through OBS Virtual
Camera. **Nothing from this project runs on the gaming PC.**

> **Tested on 2026-11-07** with the public v0.2.0 download: many chests counted on the streaming PC, no noticeable lag or
> other problems. That result is for our hardware and network ([tested setup](#tested-setup)); other networks and
> settings can behave differently.

```text
GAMING PC                                          STREAMING PC
Fortnite                                           OBS: Media Source "SRT from gaming PC"
  -> OBS Game Capture                                   (SRT listener on UDP port 9000, MPEG-TS)
  -> OBS "Start Streaming" (Custom service,          -> scene "ChestDetectorFeed"
     SRT caller to the streaming PC)  --- LAN --->   -> OBS Virtual Camera
                                                     -> Fortnite Visual Chest Counter -> OBS Text source on your stream
```

**Contents:** [What you need](#what-you-need) · [1. IP address](#1-find-the-streaming-pcs-ip-address) ·
[2. Receiver](#2-streaming-pc-receive-the-picture-in-obs) · [3. Sender](#3-gaming-pc-send-the-picture-with-srt) ·
[4. Program and Virtual Camera](#4-streaming-pc-program-scene-and-virtual-camera) · [5. Count](#5-start-counting) ·
[Every session](#every-session) · [What to expect](#what-to-expect) · [Troubleshooting](#troubleshooting) ·
[Tested setup](#tested-setup)

## What you need

- Two Windows PCs on the **same local network** (connected to the same router). A wired connection is the most dependable;
  Wi-Fi can work. Guest networks often stop devices from reaching each other - use your main network.
- **OBS Studio** on both PCs (use a current version).
- **Fortnite Visual Chest Counter** on the streaming PC only ([download](../../README.md#get-started)).
- About ten minutes. The steps use **example** addresses such as `192.168.1.50` - yours will be different.

## 1. Find the streaming PC's IP address

On the **streaming PC**:

1. Press **Start**, type `cmd`, open **Command Prompt**.
2. Type `ipconfig` and press Enter.
3. Under the adapter you use (**Ethernet adapter ...** or **Wireless LAN adapter Wi-Fi**), note the **IPv4 Address**,
   for example `192.168.1.50`. It usually starts with `192.168.`, `10.` or `172.`.

The router can give the PC a different address later (for example after a restart). If the connection stops working one
day, check the address again - or reserve it in your router's settings ("DHCP reservation").

## 2. Streaming PC: receive the picture in OBS

Set up the receiver first, so it is already listening when the gaming PC starts sending.

1. In OBS, **Scenes** box: click **+** and name the scene `ChestDetectorFeed`.
2. **Sources** box: **+** -> **Media Source** -> name it `SRT from gaming PC` -> OK.
3. In its properties:
   - **Untick "Local File".**
   - **Input:** `srt://0.0.0.0:9000?mode=listener`
   - **Input Format:** `mpegts`
   - Optional: tick **Use hardware decoding when available**. Leave the other options as they are.
   - Click **OK**.
4. If Windows asks whether OBS may communicate on networks, allow it on **Private networks**.

The source stays empty until the gaming PC sends (step 3). Once the picture appears, select the source and press
`Ctrl+F` so it fills the scene. Put nothing else in `ChestDetectorFeed` (no webcam, alerts, chat or the counter's text).

**Also showing this picture on your stream?** Create the Media Source only once. In your stream scene use **Sources** ->
**+** -> **Media Source** -> **Add Existing** -> `SRT from gaming PC`. A second Media Source on the same port cannot receive
the feed. *(Standard OBS behaviour; our test used the receiving scene directly.)*

## 3. Gaming PC: send the picture with SRT

On the **gaming PC**, in OBS:

1. Have a scene with Fortnite in it (**Game Capture**, filling the scene). Send **clean gameplay**: add webcam, alerts and
   chat on the streaming PC instead, so the counter does not see them.
2. **Settings -> Stream:**
   - **Service:** `Custom...`
   - **Server:** `srt://192.168.1.50:9000?mode=caller` - replace `192.168.1.50` with the address from step 1.
   - **Stream Key:** leave empty.
3. **Settings -> Output** (Output Mode **Advanced**, **Streaming** tab) - suggested starting point:
   - **Video Encoder:** NVIDIA NVENC H.264
   - **Rate Control:** CBR, **Bitrate:** `4000` Kbps
   - **Keyframe Interval:** `2` s
   - In Output Mode **Simple**: Video Bitrate `4000`, Encoder **Hardware (NVENC, H.264)**.
   - No NVIDIA GPU? Your AMD or Intel hardware encoder, or x264, should also work (not part of our test).
4. **Settings -> Video:** **Output (Scaled) Resolution** `1280x720`, **Common FPS Values** `30`. These settings also apply
   to recordings made with OBS on this PC.
5. Click **OK**, then **Start Streaming**.

"Start Streaming" here sends the picture **only to your streaming PC** - not to Twitch or YouTube (go live from the
streaming PC as usual). Within a few seconds the picture appears in the Media Source on the streaming PC.

Why 720p30: the counter analyses the picture at 1280x720, 15 pictures per second, so more does not improve counting. If
your stream needs more (for example 1080p60 and a higher bitrate), that works too, as long as your network keeps up.

## 4. Streaming PC: Program scene and Virtual Camera

1. **Keep the scene with the Media Source live in Program while you count.** If **Studio Mode** is on, the scene you click
   only appears in **Preview** (left); select it and click **Transition** so it is in **Program** (right). Preview is not
   what OBS outputs. On a PC that also streams, your stream scene in Program counts, if it contains the Media Source (Add
   Existing, step 2).
2. In the **Controls** box click the **gear** next to **Start Virtual Camera**: **Output Type** = **Scene**,
   **Output Selection** = **ChestDetectorFeed**. Close the dialog.
3. Click **Start Virtual Camera**.

## 5. Start counting

1. On the streaming PC open **Fortnite Visual Chest Counter.exe**.
2. Keep **OBS Virtual Camera** as the video source and click **Start counting**. The line under the source shows the
   picture size and how many pictures per second are analysed (about 15).
3. Turn on **Live preview** once to check: the whole picture is the 16:9 gameplay, with the HUD visible.
4. Show the numbers on your stream: [OBS overlay guide](obs-overlay.md).

## Every session

1. Streaming PC: open OBS (the Media Source starts listening).
2. Gaming PC: OBS -> **Start Streaming**.
3. Streaming PC: picture in the Media Source? Scene live in Program, **Start Virtual Camera**.
4. Streaming PC: the app -> **Start counting**. Counts are saved after every chest and kept between sessions.

## What to expect

- **Counting works as on one PC.** The counter sees only the picture it receives, so the usual rules apply: 16:9 picture,
  HUD visible, English or Portuguese (Brazil) pop-ups, CERTAIN and APPROX ([OBS counter definitions](obs-overlay.md)).
- **A short delay.** The picture reaches the streaming PC slightly after it happens, so each chest is counted that much
  later too. In our test there was no noticeable lag.
- **Picture quality matters.** A very low bitrate or an overloaded network blurs or breaks up the small pop-up text.
  Chests shown while the picture is frozen or broken can be missed, and more chests end up in APPROX only. If you see
  that: use a wired connection, or try a slightly different bitrate.
- **Load on the streaming PC:** OBS decodes the feed (hardware decoding helps) and the counter analyses 15 pictures per
  second. Our streaming PC (Intel Core i5 13th gen, RTX 4070) did this without noticeable problems.
- **On the gaming PC only OBS runs** - it captures and sends video. Fair play notes: [README](../../README.md#compatibility-and-data).

## Troubleshooting

| Problem | What to check |
|---|---|
| Media Source stays black | 1. The gaming PC is streaming (OBS status bar shows a bitrate). 2. The Server address is the streaming PC's **current** IPv4 address (step 1). 3. Same port on both sides; `mode=caller` on the gaming PC, `mode=listener` on the streaming PC. 4. **Local File** unticked, Input Format `mpegts`. 5. [Windows Firewall](#windows-firewall-udp-port-9000) on the streaming PC. 6. Both PCs on the same network: not a guest network, no VPN. |
| Gaming PC's OBS cannot connect or keeps reconnecting | The streaming PC must be listening first: open OBS there, then click **Start Streaming** again. Then check the address and the firewall. |
| Worked before, black now | The streaming PC probably got a new address: run `ipconfig` again and update the Server on the gaming PC. |
| Picture freezes, stutters or breaks up | Network cannot keep up: try a wired connection or a slightly lower bitrate; close large downloads. |
| No picture after the gaming PC restarted streaming | Wait a few seconds. If it does not come back, open the Media Source's properties on the streaming PC and click **OK** (this restarts it). |
| Media Source shows the game, the counter does not | Virtual Camera started? Output Type **Scene** -> **ChestDetectorFeed**? Scene with the Media Source live in **Program** (Studio Mode)? Check the app's **Live preview**. |
| Picture arrives but nothing is counted | Live preview: full 16:9 picture, HUD visible, no overlays sent from the gaming PC? Then see [general troubleshooting](troubleshooting.md). |

### Windows Firewall (UDP port 9000)

The streaming PC receives on **UDP port 9000**. If you dismissed Windows' question or nothing arrives:

1. Set the network to **Private**: **Settings -> Network & internet -> Wi-Fi** (or **Ethernet**) -> your network ->
   **Network profile type: Private network**. Do this on both PCs.
2. Allow the port: right-click **Start** -> **Terminal (Admin)** (or Windows PowerShell (Admin)) and run:

   ```powershell
   New-NetFirewallRule -DisplayName "OBS SRT (UDP 9000)" -Direction Inbound -Protocol UDP -LocalPort 9000 -Action Allow -Profile Private
   ```

   To remove it later: `Remove-NetFirewallRule -DisplayName "OBS SRT (UDP 9000)"`.
3. Using another security program with its own firewall? Allow OBS or UDP port 9000 there as well.

Port 9000 is only a choice: any free port works if it is the same in both URLs (and in the firewall rule).

### Can the PCs reach each other?

On the gaming PC open Command Prompt and run `ping 192.168.1.50` (the streaming PC's address). Replies mean the network
path works. "Request timed out" does not prove a problem - Windows blocks ping by default - so also check the firewall
and the addresses above.

## Tested setup

| | Gaming PC (sender) | Streaming PC (receiver) |
|---|---|---|
| Hardware | Intel Core i9 13th gen, NVIDIA RTX 4080 | Intel Core i5 13th gen, NVIDIA RTX 4070 |
| Software | OBS Studio: SRT output (Custom service, caller) | OBS Studio: Media Source (SRT listener, MPEG-TS), OBS Virtual Camera; Fortnite Visual Chest Counter v0.2.0 (public download) |

- **Video:** a Fortnite stream playing on the gaming PC, captured by its OBS - rather than a match played live. The video
  path (OBS -> SRT -> OBS -> Virtual Camera -> counter) is the same either way.
- **Result (2026-11-07):** numerous chests detected and counted on the streaming PC; no noticeable lag or other
  significant problems; the counter did not run on the gaming PC.
- **What it shows:** SRT is a viable way to get the picture to the counter on this setup. It does not guarantee the same
  result on every network, PC or OBS configuration. If your setup behaves differently, please
  [open an issue](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/issues) with the app's diagnostics report.
