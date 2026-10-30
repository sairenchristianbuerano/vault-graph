# Accessibility

vault-graph's UI is a canvas star map with HTML panels around it. Here is what works today, and what does not.

## What works

- **Keyboard:** `Esc` closes the task dialog, the details panel and the legend filter. `f` fits the whole graph, `h` zooms to the highlight, `+` and `-` zoom.
- **Real controls:** the zoom buttons, the note lists in the details panel, and the task dialog's actions are HTML buttons, so they can be reached and read by assistive technology.
- **Task dialog:** uses `role="dialog"` with `aria-modal="true"` and a label, moves focus to its main action when it opens, and the note field has a visible label.
- **Reduced motion:** with the system "reduce motion" setting, stars stop twinkling, the camera jumps instead of flying, and the "NEED YOU" glow becomes a static outline.
- **Text alternatives:** everything the graph shows is also available as text. The task board panel lists every task with its priority and progress. The command line answers the same questions: `vault.py search`, `note`, `related`, `path`, `entity`, `report` and `task list`. `graph/GRAPH_REPORT.md` lists the most connected notes, hubs, bridges and orphans.

## Known limits

- **The stars on the canvas are not navigable with a screen reader or the keyboard.** Use the board, the legend's panel lists (click a legend entry), or the CLI instead.
- Some legend entries and board items are clickable elements that are not yet buttons, so keyboard focus does not reach all of them.
- Colors carry meaning (kind of note, priority). Priorities also have a text label (`85% CRITICAL`), but note kinds in the graph are shown by color only.
- The theme is dark only.

## Report a barrier

Open an issue with the **accessibility** label. Say what you were trying to do, what you used (browser, screen reader, keyboard only, zoom level), and what got in the way. Barriers are treated as bugs.
