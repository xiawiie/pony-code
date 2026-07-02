from datetime import datetime, timezone
from pathlib import Path


METRICS_SCHEMA_VERSION = 2
DEFAULT_HARNESS_REGRESSION_V2_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_CONTEXT_ABLATION_V2_PATH = Path("artifacts/context-ablation-v2.json")
DEFAULT_MEMORY_ABLATION_V2_PATH = Path("artifacts/memory-ablation-v2.json")
DEFAULT_RECOVERY_ABLATION_V2_PATH = Path("artifacts/recovery-ablation-v2.json")
DEFAULT_CORE_REPORT_PATH = Path("docs/metrics/pico-benchmark-core-report.md")


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_mean(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _safe_ratio(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator


def _parse_iso8601(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None
