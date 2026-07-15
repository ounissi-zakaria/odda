# Dynamic analysis records are wiped on navigation, not preserved across page loads

Both Wrap and Logpoint wipe their recorded events when the page navigates. This matches
Wrap's userscript mechanism (the userscript re-initializes its record array on each
`document_start`) and is applied to Logpoint for consistency, even though Logpoint's CDP
breakpoint survives navigation and re-binds to the new script.

The rejected alternative is DevTools-style preservation: keep records across navigations,
stamped with a navigation/page-load identifier so the agent can slice by load. This was
rejected for simplicity — the agent's workflow is wrap → trigger → dump within a single
page load, so cross-navigation accumulation adds state (and a slicing API) for a use case
that doesn't exist yet. The cost is that the agent must dump before navigating again, or
the records are lost.

This applies to records only. Logpoint *installations* persist across navigation (the CDP
breakpoint re-binds to the re-loaded script); only the recorded events are wiped. Logpoint
installations do not survive tab close — they are per-tab-session, not durable.