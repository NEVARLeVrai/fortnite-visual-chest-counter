# Personal stats

Open **Stats** in the navigation rail. Choose a period, a game mode and a match quality filter to filter the totals, record, graphs and mode table. The 7-day and 30-day filters include today.

The quality filter offers **Many unknown chests** (at least 3 unknown chests and 25% of the original live total), **No verified replay**, and **Large replay difference** (at least 5 more or fewer openings than the original live count). Verified matches use the stored live breakdown for the unknown filter. For newly verified results, the replay difference includes destroyed chests. Choosing a quality filter opens the per-match chart so you can select a result directly.

- **Overview** shows saved matches, chests, confirmed and estimated counts, average, record and how many matches were replay verified.
- **Chests over time** groups by date or displays the last 30 matches within the current filters. In the per-match view, the green line starts after five matches and averages the current match and the four previous matches in the filtered history. Hover to read a bar. Click a match bar or use the arrow keys and Enter to open its result and the live-to-replay difference and chest-type changes when available.
- **Chest types** shows counts and shares. Older records with missing type data contribute to **Not classified**.
- **By game mode** shows matches, total chests, average and best match for each mode in the selection.

The estimated Pirate Token chance combines the saved counts using each match's boost. A verified replay replaces that match's live counts. The page uses local saved matches. On Discord, [`/stats`](matches-and-discord.md#commands) uses finished results shared to the group.

![Stats with the per-match chart, five-match average and chest-type breakdown](../previews/stats.png)

**By capture configuration** groups matches by the capture source, HUD scale and target analysis rate recorded by the worker. It shows match count and the proportion of unknown chest types among live openings with a recorded type classification. The comparison uses original live breakdowns for verified matches. Older matches without capture metadata appear as **Unknown configuration**. Use the capture filter to inspect one setup alongside the period, mode and quality filters.
