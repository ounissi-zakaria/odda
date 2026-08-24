"""Redeclared Chrome launch flags for odda's per-browser profiles.

Under ``ignore_default_args=True`` Chrome receives *only* these args, so this
module redeclares patchright's ``chromiumSwitches`` set (plus what
``_innerDefaultArgs`` adds) with two deliberate omissions and one addition.
The source of truth is patchright's ``chromiumSwitches`` block in the
installed driver's ``coreBundle.js``; see ``AGENTS.md`` for the drift audit.
"""

from __future__ import annotations

import os

# patchright's ``disabledFeatures`` array, verbatim (16 names).
_DISABLED_FEATURES: tuple[str, ...] = (
    "AvoidUnnecessaryBeforeUnloadCheckSync",
    "BoundaryEventDispatchTracksNodeRemoval",
    "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider",
    "GlobalMediaControls",
    "HttpsUpgrades",
    "LensOverlay",
    "MediaRouter",
    "PaintHolding",
    "ThirdPartyStoragePartitioning",
    "BlockOriginHeaderModificationOnRedirect",
    "Translate",
    "AutoDeElevate",
    "OptimizationHints",
    "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion",
)

# Added on top of patchright's set: the m150 feature names that suppress the
# on-device GenAI model store (``optimization_guide_model_store``, ~48MB per
# profile). patchright's ``OptimizationHints`` disables hints downloads, not
# the model store. Not in patchright — the whole point of the redeclare.
_MODEL_STORE_FEATURES: tuple[str, ...] = (
    "OptimizationGuide",
    "OptimizationGuideOnDeviceModel",
    "OptimizationGuideModelExecution",
    "OptimizationGuideModelsManifest",
    "OptimizationGuidePrediction",
)


def _disable_features_value() -> str:
    return ",".join((*_DISABLED_FEATURES, *_MODEL_STORE_FEATURES))


def _enable_features_value() -> str:
    # Reproduce patchright's PLAYWRIGHT_LEGACY_SCREENSHOT ternary.
    if os.environ.get("PLAYWRIGHT_LEGACY_SCREENSHOT"):
        return ""
    return "--enable-features=CDPScreenshotNewSurface"


# patchright's desktop ``chromiumSwitches()`` literals (the driver's
# ``_innerDefaultArgs`` calls it with no options, so ``--disable-sync`` IS
# present — its ``android: true`` branch only fires on the Android path).
#
# Deliberately OMITTED (odda-specific deviations — do not add back):
#   --password-store=basic, --use-mock-keychain
#       odda wants the real system keychain so seeded cookies persist
#       (the point of init-chrome-profile).
_CHROMIUM_SWITCHES: tuple[str, ...] = (
    "--disable-field-trial-config",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-edgeupdater",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-updater-scheduler",
    "--force-color-profile=srgb",
    "--no-first-run",
    "--no-service-autorun",
    "--export-tagged-pdf",
    "--disable-search-engine-choice-screen",
    "--edge-skip-compat-layer-relaunch",
    "--disable-infobars",
    "--disable-blink-features=AutomationControlled",
    "--disable-sync",
)


def build_chrome_args(
    user_data_dir: str,
    proxy_url: str | None = None,
    *,
    headless: bool = True,
) -> list[str]:
    """Assemble the complete Chrome ``args=`` list for a persistent context.

    ``about:blank`` is the positional URL and **must be last** (Chrome treats
    the first non-``--`` token as the URL; anything after it is ignored).

    With a proxy, ``--proxy-bypass-list=<-loopback>`` *forces* loopback
    (127.0.0.1) traffic through the proxy — load-bearing for odda's e2e
    fixture/dyn servers, whose flows are captured through the proxy. Dropping
    it silently breaks loopback flow capture.

    Args:
        user_data_dir: Chrome ``--user-data-dir`` target (temp profile).
        proxy_url: Optional proxy server URL. When ``None`` no proxy flags
            are emitted.
        headless: When true (default), add the headless block; odda defaults to
            headless and ``--headed`` is the explicit override.

    Returns:
        The complete ``args`` list, with ``about:blank`` last.
    """
    switches = [*_CHROMIUM_SWITCHES, f"--disable-features={_disable_features_value()}"]
    enabled = _enable_features_value()
    if enabled:
        switches.append(enabled)
    if headless:
        switches.append("--headless")
        switches.append("--hide-scrollbars")
        switches.append("--mute-audio")
        switches.append(
            "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
            "primaryPointerType=4,availablePointerTypes=4"
        )
    # Required in the Docker test container (non-root, restricted namespaces).
    switches.append("--no-sandbox")
    proxy_flags = [f"--proxy-server={proxy_url}"] if proxy_url else []
    if proxy_url:
        proxy_flags.append("--proxy-bypass-list=<-loopback>")
    return [
        *switches,
        f"--user-data-dir={user_data_dir}",
        *proxy_flags,
        "--remote-debugging-pipe",
        "--enable-unsafe-extension-debugging",
        "about:blank",
    ]
