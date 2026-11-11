## Fortnite Visual Chest Counter v0.3.0 — Public Beta

First public beta of the fork. [Fortnite-Visual-Chest-Counter-v0.3.0.zip](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/releases/download/v0.3.0/Fortnite-Visual-Chest-Counter-v0.3.0.zip) · [Setup and features](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/blob/v0.3.0/README.md) · [Report an issue](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/issues)

## Changes since upstream v0.2.3

- **Capture and detection.** Single local capture per player, with HDR-to-SDR conversion and lighter preview updates. French chest detection follows HUD scaling and stacked XP notifications. The detector distinguishes normal, rare and Sprite chests when the text supports a type, and can retain an unknown type when the evidence is ambiguous.
- **Match tracking and verification.** Start and finish local Battle Royale matches from Fortnite’s log, detect the playlist in Auto mode, or use the manual controls. The optional Fortnite replay reader can reconcile counts and types after a match and keep destroyed chests separate. Local match history can show a replay comparison and teammates from the same session.
- **Desktop and overlays.** Redesigned Material-inspired interface, setup guide, animated chest artwork, separate confirmed and estimated counts, and configurable Personal, Team and Match history + live sections for the floating overlay and OBS.
- **Discord.** Host and Guest roles share results from their own captures. Linked sessions are deduplicated, and replay corrections update the same result. The published ranking shows synchronized totals and token odds. Use `/match` for shared history and a match summary, and `/chests` for detailed player counts and chest types.
- **Pirate Token.** The estimates use the counted chests and keep the boost saved with each result, including when a cumulative total spans different boosts.
- **Support.** Diagnostics default to off. The README, setup guides, screenshots and Discord previews cover the current fork.

See the [full change and validation notes](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/blob/v0.3.0/docs/CHANGES.md).

## Install on Windows

Extract the entire ZIP and launch **Fortnite Visual Chest Counter.exe**. To migrate an existing installation, close it and copy your own `config.toml` and `state/` to the newly extracted folder. Discord credentials remain in Windows Credential Manager on the same Windows account. Replay verification requires the .NET 10 and ASP.NET Core 10 x64 runtimes.

The first installation from this fork is manual. It establishes trust in the fork’s signing keys. Later compatible signed releases can be offered by the experimental in-app updater. The launcher and the updater’s manifest use separate signing systems. The manifest is signed with the fork’s Ed25519 key.

Based on Fortnite Visual Chest Counter by Gabriel Gomez. See [credits and licenses](https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/blob/v0.3.0/docs/ACKNOWLEDGEMENTS.md).
