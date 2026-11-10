# Headed launches size the page to the window; headless keeps the fixed 1280×720 viewport

`launch_persistent_context` was called with no `viewport=`, so patchright
applied its default — a fixed 1280×720 viewport enforced through a CDP
device-metrics override — in both modes. Headless has no window to diverge
from the override, so nothing shows it. Headed mode does: Chrome's window is
freely resizable and maximizable, but the page stays pinned to an
override-sized viewport anchored to the top-left of the content area, and
the remainder of the window never paints. A human watching the window (the
entire point of `headless=False`, ADR-0016) saw the page stranded in a
1280×720 corner with blank white margins, while the agent — whose
screenshots capture only the emulated viewport — saw nothing wrong.

Headed launches now pass `no_viewport=True`: the page sizes to the window
and reflows on resize/maximize, and `screen.*` reports the real display
geometry instead of the override's 1280×720. Headless launches are
untouched: the fixed 1280×720 viewport stays, keeping headless runs and the
e2e suite deterministic.

The 1280×720 was inherited from the driver's default, never a chosen odda
constant — but the headless half of that default is load-bearing (stable
geometry for agent-driven automation, constant screenshot dimensions), so it
stays exactly as patchright ships it. Only the headed half changes.
`browser_open(headless=False)` remains the opt-in for a human watching; that
axis is unchanged.

## Considered Options

- Keep the override for headed launches and add `--window-size` so the
  window launches fitting the viewport — rejected: cosmetic. The window is
  still resizable, so the first maximize recreates the stranded corner.
- Keep the override and re-set device metrics on every window resize —
  rejected: reimplements `no_viewport` with a listener, churning the
  geometry the agent reads mid-task.
- `no_viewport=True` for both modes — rejected: headless has no window to
  follow; the fixed 1280×720 is what makes headless screenshots and
  coordinate targeting deterministic.
- Document "don't resize the odda window" — rejected: the headed mode is
  for the human watching it; the failure was one maximize away, always.

## Consequences

- Headed screenshots rasterize at whatever the window's size is (often
  larger than 1280×720); the JPEG path and quality are unchanged.
- Coordinate targeting in headed mode is as fresh as the geometry read it
  was taken from: a user resizing the window between a snapshot and a
  coordinate click stretches the cached boxes. Ref targeting is unaffected.
  This staleness window pre-exists any window resize; it is inherent to a
  live window and not new.
- Without the override, headed mode reports the real `devicePixelRatio` and
  `screen.*`, which the annotate path's DSF scaling already reads per
  capture — no special handling.
- ADR-0016's headless default and its one-boolean-one-axis rule stand; this
  changes viewport emulation only on the headed side of that same boolean.
