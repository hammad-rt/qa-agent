"""Detect CRM/ERP platform and version from URL + page markup (read-only).

Results belong on the discovery / env fingerprint, never as lasting facts.
Unknown or custom is a normal outcome.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass
class Detection:
    platform: str | None = None          # suitecrm7 | suitecrm8 | sugarcrm | None
    platform_confidence: float = 0.0
    platform_version: str | None = None
    version_confidence: float = 0.0
    signals: list[str] | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = list(self.signals or [])
        return d


# URL / route patterns
_SUITE7_URL = re.compile(r"index\.php\?([^#]*&)?module=", re.I)
_SUITE8_HASH = re.compile(r"/#/(?:home|login|accounts|contacts|leads|opportunities|calendar|admin)", re.I)
_SUITE8_ANGULAR = re.compile(r"suitecrm.?8|suite8|/api/v8/|ng-version|angular", re.I)
_SUGAR_SIDECAR = re.compile(r"#(Accounts|Contacts|Leads|Opportunities|Home|RevenueLineItems)\b")
_SUGAR_MARK = re.compile(r"sugarcrm|sidecar|sugar_version|SugarApplication", re.I)
_SUITE_MARK = re.compile(r"suitecrm|SuiteP|themes/SuiteP|SuiteCRM", re.I)
_VERSION_META = re.compile(
    r'(?:suitecrm|sugar(?:crm)?)\s*(?:version)?[^0-9]{0,12}(\d+(?:\.\d+){1,3})',
    re.I,
)
_META_GENERATOR = re.compile(
    r'<meta[^>]+(?:name|content)=["\'][^"\']*(?:suitecrm|sugarcrm)[^"\']*["\'][^>]*>',
    re.I,
)
_VERSION_FILE = re.compile(r"Version\s+(\d+(?:\.\d+){1,3})", re.I)


def detect(url: str = "", html: str = "", title: str = "") -> Detection:
    """Return best-effort platform id + confidence from URL and markup."""
    url = url or ""
    html = html or ""
    title = title or ""
    blob = f"{url}\n{title}\n{html[:200_000]}"
    signals: list[str] = []
    scores: dict[str, float] = {"suitecrm7": 0.0, "suitecrm8": 0.0, "sugarcrm": 0.0}

    if _SUITE7_URL.search(url) or _SUITE7_URL.search(html):
        scores["suitecrm7"] += 0.55
        signals.append("url:index.php?module=")
    if re.search(r"entryPoint=|action=Login|module=Users", blob, re.I):
        scores["suitecrm7"] += 0.15
        signals.append("legacy:entryPoint/action")
    if _SUITE_MARK.search(blob) and not _SUITE8_ANGULAR.search(blob):
        scores["suitecrm7"] += 0.25
        signals.append("markup:suitecrm-legacy")
    if re.search(r"themes/SuiteP|sugar_version\.js|jssource/src_files", blob, re.I):
        scores["suitecrm7"] += 0.2
        signals.append("assets:SuiteP")

    if _SUITE8_HASH.search(url) or _SUITE8_HASH.search(html):
        scores["suitecrm8"] += 0.5
        signals.append("route:/#/")
    if _SUITE8_ANGULAR.search(blob):
        scores["suitecrm8"] += 0.35
        signals.append("markup:angular/suite8")
    if re.search(r"SuiteCRM\s*8|suitecrm8", blob, re.I):
        scores["suitecrm8"] += 0.25
        signals.append("label:SuiteCRM8")

    if _SUGAR_SIDECAR.search(url) or _SUGAR_SIDECAR.search(html):
        scores["sugarcrm"] += 0.5
        signals.append("route:#Module")
    if _SUGAR_MARK.search(blob) and "suitecrm" not in blob.lower():
        scores["sugarcrm"] += 0.35
        signals.append("markup:sugar/sidecar")
    elif re.search(r"\bsidecar\b", blob, re.I) and re.search(r"\bsugar\b", blob, re.I):
        scores["sugarcrm"] += 0.25
        signals.append("markup:sidecar+sugar")

    best, conf = max(scores.items(), key=lambda kv: kv[1])
    if conf < 0.35:
        return Detection(platform=None, platform_confidence=round(conf, 2), signals=signals)

    version, vconf = _detect_version(blob, best)
    return Detection(
        platform=best,
        platform_confidence=min(0.99, round(conf, 2)),
        platform_version=version,
        version_confidence=vconf,
        signals=signals,
    )


def _detect_version(blob: str, platform: str) -> tuple[str | None, float]:
    m = _VERSION_META.search(blob)
    if m:
        return m.group(1), 0.7
    if _META_GENERATOR.search(blob):
        m2 = _VERSION_FILE.search(blob)
        if m2:
            return m2.group(1), 0.55
    # Weak defaults from family only — low confidence.
    if platform == "suitecrm7":
        return None, 0.0
    if platform == "suitecrm8":
        return None, 0.0
    return None, 0.0


def fingerprint_patch(detection: Detection) -> dict:
    """Fragment to merge into discovery / env fingerprint (not lasting facts)."""
    return {
        "platform": detection.platform,
        "platform_confidence": detection.platform_confidence,
        "platform_version": detection.platform_version,
        "version_confidence": detection.version_confidence,
        "platform_signals": list(detection.signals or []),
    }
