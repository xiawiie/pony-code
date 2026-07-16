from datetime import datetime, timezone
import json
from pathlib import Path


CONTEXT_ABLATION_FORMAT_VERSION = 1
MEMORY_ABLATION_FORMAT_VERSION = 1
RECOVERY_ABLATION_FORMAT_VERSION = 1
DEFAULT_HARNESS_REGRESSION_V2_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_CONTEXT_ABLATION_V2_PATH = Path("artifacts/context-ablation-v2.json")
DEFAULT_MEMORY_ABLATION_V2_PATH = Path("artifacts/memory-ablation-v2.json")
DEFAULT_RECOVERY_ABLATION_V2_PATH = Path("artifacts/recovery-ablation-v2.json")
DEFAULT_CORE_REPORT_PATH = Path("docs/metrics/pico-benchmark-core-report.md")


def _decode_json_object(text):
    def object_from_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=object_from_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid JSON artifact") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON artifact must be an object")
    return payload


def _validate_record_header(payload, record_type, format_version):
    if payload.get("record_type") != record_type:
        raise ValueError("unexpected record_type")
    if (
        type(payload.get("format_version")) is not int
        or payload["format_version"] != format_version
    ):
        raise ValueError("unsupported format_version")
    return payload


def _load_json_artifact(path, record_type, format_version):
    payload = _decode_json_object(Path(path).read_text(encoding="utf-8"))
    return _validate_record_header(payload, record_type, format_version)


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
