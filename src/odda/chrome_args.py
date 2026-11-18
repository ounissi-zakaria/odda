from __future__ import annotations

import os

from odda.vdisplay import SCREEN_HEIGHT, SCREEN_WIDTH

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
    if os.environ.get("PLAYWRIGHT_LEGACY_SCREENSHOT"):
        return ""
    return "--enable-features=CDPScreenshotNewSurface"


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

# See ADR-0040.


def build_chrome_args(
    user_data_dir: str,
    proxy_url: str | None = None,
) -> list[str]:
    # See ADR-0031.
    switches = [*_CHROMIUM_SWITCHES, f"--disable-features={_disable_features_value()}"]
    enabled = _enable_features_value()
    if enabled:
        switches.append(enabled)
    switches.append("--window-position=0,0")
    switches.append(f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}")
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
