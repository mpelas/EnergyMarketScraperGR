"""Normalization of raw ADMIE / HEnEx / ENTSO-E payloads into the unified
hourly record schema, plus merging and the grid-stress indicator.

Pure functions only (no I/O) so they can be unit-tested directly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

GRID_STRESS_HIGH = "HIGH"
GRID_STRESS_MODERATE = "MODERATE"
GRID_STRESS_LOW = "LOW"

# Thresholds used by the default grid-stress indicator
DEFAULT_HIGH_CURTAILMENT_MW = 300.0
DEFAULT_MODERATE_CURTAILMENT_MW = 50.0
DEFAULT_NEGATIVE_PRICE = 0.0
DEFAULT_LOW_PRICE_EUR = 20.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_date(value: Any, fallback: date) -> date:
    """Resolve an input date string: YYYY-MM-DD, 'today', 'yesterday', or fallback."""
    if not value:
        return fallback
    s = str(value).strip().lower()
    today = datetime.now(timezone.utc).date()
    if s in ("today", "current_date"):
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(s)
    except ValueError:
        return fallback


def hour_timestamp(day: str, hour: int) -> str:
    """Build an ISO-8601 UTC hour-start timestamp. hour is 1..24 (market convention)."""
    h = (hour - 1) % 24
    return f"{day}T{h:02d}:00:00Z"


def new_record(timestamp: str, market_area: str = "GR", resolution: str = "1H") -> Dict[str, Any]:
    return {
        "timestamp": timestamp,
        "marketArea": market_area,
        "resolution": resolution,
        "load": {"forecastMW": None, "actualMW": None},
        "renewables": {
            "solarForecastMW": None,
            "solarActualMW": None,
            "windForecastMW": None,
            "windActualMW": None,
            "estimatedCurtailmentMW": None,
        },
        "pricing": {"henexMCP_EUR_MWh": None, "systemMarginalPrice_EUR_MWh": None},
        "crossBorderFlowsMW": {},
        "gridStressIndicator": GRID_STRESS_LOW,
        "sources": [],
        "ingestedAt": utc_now_iso(),
    }


def _set_once(target: Dict[str, Any], key: str, value: Any) -> None:
    """Set a value only if it is not None and the target is still empty."""
    if value is None:
        return
    if target.get(key) is None:
        target[key] = value


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def ingest_admie_payload(record: Dict[str, Any], payload: Any, category: str, source_url: str) -> None:
    """Best-effort extraction of grid metrics from an ADMIE file payload.

    ADMIE files are JSON/CSV/XML with varying schemas per FileCategory, so we
    scan dicts/lists for known key names instead of hard-coding one layout.
    """
    keys = _flatten_keys(payload)
    load, ren, pricing, grid = record["load"], record["renewables"], record["pricing"], None

    def find(*names: str) -> Optional[float]:
        lowered = {k.lower(): k for k in keys}
        for n in names:
            k = lowered.get(n.lower())
            if k is not None:
                v = _as_float(keys[k])
                if v is not None:
                    return v
        return None

    _set_once(load, "actualMW", find("systemload", "actualload", "loadmw", "totalload"))
    _set_once(load, "forecastMW", find("forecastload", "loadforecast", "forecast"))
    _set_once(ren, "solarActualMW", find("solaractual", "solargeneration", "solar"))
    _set_once(ren, "solarForecastMW", find("solarforecast", "solarfc"))
    _set_once(ren, "windActualMW", find("windactual", "windgeneration", "wind"))
    _set_once(ren, "windForecastMW", find("windforecast", "windfc"))
    _set_once(pricing, "systemMarginalPrice_EUR_MWh", find("smp", "systemmarginalprice", "marginalprice"))
    curt = find("curtailmentmw", "curtailment", "downwardcurtailment")
    if curt is not None:
        _set_once(ren, "estimatedCurtailmentMW", curt)

    # timestamp if the payload carries one
    ts = None
    for k in keys:
        if k.lower() in ("timestamp", "datetime", "date_time", "date"):
            ts = _parse_ts(keys[k])
            if ts:
                break
    if ts:
        record["timestamp"] = ts
    if "ADMIE" not in record["sources"]:
        record["sources"].append("ADMIE")
    record.setdefault("sourceUrls", {})
    record["sourceUrls"][category] = source_url


def ingest_henex_row(record: Dict[str, Any], row: Dict[str, Any], day: str, source_url: str) -> None:
    """Map one row of the HEnEx DAM results sheet onto the unified record."""
    lowered = {str(k).strip().lower(): v for k, v in row.items()}

    def find(*names: str) -> Any:
        for n in names:
            if n in lowered:
                return lowered[n]
        return None

    hour = _as_float(find("hour", "period", "timeblock", "block"))
    if hour is not None:
        record["timestamp"] = hour_timestamp(day, int(hour))
    _set_once(record["load"], "actualMW", _as_float(find("total system load (mw)", "system load (mw)", "load (mw)", "totalload")))
    mcp = _as_float(find("mcp (eur/mwh)", "mcp (€/mwh)", "mcp", "market clearing price", "price (eur/mwh)", "clearing price (eur/mwh)"))
    _set_once(record["pricing"], "henexMCP_EUR_MWh", mcp)
    volume = _as_float(find("volume (mwh)", "total volume (mwh)", "volume"))
    if volume is not None and record.get("henexVolumeMWh") is None:
        record["henexVolumeMWh"] = volume
    if "HEnEx" not in record["sources"]:
        record["sources"].append("HEnEx")
    record.setdefault("sourceUrls", {})
    record["sourceUrls"]["HEnEx_DAM_Results"] = source_url


def ingest_entsoe_flow(record: Dict[str, Any], border: str, mw: float) -> None:
    record["crossBorderFlowsMW"][border] = mw
    if "ENTSO-E" not in record["sources"]:
        record["sources"].append("ENTSO-E")


def _flatten_keys(obj: Any, out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Flatten nested dict/list structures into {key: value} for key scanning."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _flatten_keys(v, out)
            else:
                out[str(k)] = v
    elif isinstance(obj, list):
        for item in obj:
            _flatten_keys(item, out)
    return out


def _parse_ts(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+00:00"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def merge_hourly_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge all records that share the same timestamp into one unified record."""
    merged: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        key = rec.get("timestamp")
        if not key:
            continue
        if key not in merged:
            merged[key] = new_record(key, rec.get("marketArea", "GR"), rec.get("resolution", "1H"))
        target = merged[key]
        for section in ("load", "renewables", "pricing"):
            for k, v in rec.get(section, {}).items():
                _set_once(target[section], k, v)
        for border, mw in rec.get("crossBorderFlowsMW", {}).items():
            if mw is not None and border not in target["crossBorderFlowsMW"]:
                target["crossBorderFlowsMW"][border] = mw
        for src in rec.get("sources", []):
            if src not in target["sources"]:
                target["sources"].append(src)
        for cat, url in rec.get("sourceUrls", {}).items():
            target.setdefault("sourceUrls", {})[cat] = url
        if rec.get("henexVolumeMWh") is not None and target.get("henexVolumeMWh") is None:
            target["henexVolumeMWh"] = rec["henexVolumeMWh"]
    return [merged[k] for k in sorted(merged)]


def compute_grid_stress(
    record: Dict[str, Any],
    high_curtailment_mw: float = DEFAULT_HIGH_CURTAILMENT_MW,
    moderate_curtailment_mw: float = DEFAULT_MODERATE_CURTAILMENT_MW,
    low_price_eur: float = DEFAULT_LOW_PRICE_EUR,
) -> str:
    """Classify grid stress for one hourly record.

    HIGH: curtailment >= high threshold, or negative MCP/SMP.
    MODERATE: any curtailment, or price below the low-price band.
    LOW: otherwise.
    """
    ren = record.get("renewables", {})
    pricing = record.get("pricing", {})
    curt = _as_float(ren.get("estimatedCurtailmentMW")) or 0.0
    mcp = _as_float(pricing.get("henexMCP_EUR_MWh"))
    smp = _as_float(pricing.get("systemMarginalPrice_EUR_MWh"))

    if curt >= high_curtailment_mw:
        return GRID_STRESS_HIGH
    if (mcp is not None and mcp < 0) or (smp is not None and smp < 0):
        return GRID_STRESS_HIGH
    if curt >= moderate_curtailment_mw:
        return GRID_STRESS_MODERATE
    if (mcp is not None and mcp < low_price_eur) or (smp is not None and smp < low_price_eur):
        return GRID_STRESS_MODERATE
    return GRID_STRESS_LOW


def finalize(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort, compute the stress indicator and drop internal helper keys."""
    out = []
    for rec in merge_hourly_records(records):
        rec["gridStressIndicator"] = compute_grid_stress(rec)
        rec["ingestedAt"] = utc_now_iso()
        rec.pop("sourceUrls", None)
        out.append(rec)
    return out
