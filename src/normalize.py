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
DEFAULT_LOW_PRICE_EUR = 20.0

# ADMIE ImportsExports column label -> our border key
BORDER_LABELS = {
    "ALBANIA": "AL_GR",
    "BULGARIA": "BG_GR",
    "ITALY": "IT_GR",
    "MACEDONIA": "MK_GR",
    "FYROM": "MK_GR",
    "TURKEY": "TR_GR",
}


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
    """ISO-8601 UTC hour-start timestamp. hour is 1..24 (market convention)."""
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
            "resTotalForecastMW": None,
            "resTotalActualMW": None,
            "estimatedCurtailmentMW": None,
        },
        "pricing": {"henexMCP_EUR_MWh": None, "systemMarginalPrice_EUR_MWh": None},
        "crossBorderFlowsMW": {},
        "gridStressIndicator": GRID_STRESS_LOW,
        "sources": [],
        "ingestedAt": utc_now_iso(),
    }


def _set_once(target: Dict[str, Any], key: str, value: Any) -> None:
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


def _parse_day(value: Any) -> Optional[str]:
    """Parse '17-11-2026', '2026-11-17' or a datetime into YYYY-MM-DD."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    s = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def ingest_admie_wide(
    rows: List[List[Any]],
    category: str,
    source_url: str,
    records: Dict[str, Dict[str, Any]],
) -> int:
    """Parse an ADMIE wide .xls matrix (hour columns 1..24, one row per day)
    into unified hourly records. Returns the number of hour-cells ingested."""
    if not rows or len(rows) < 2:
        return 0
    # ADMIE files contain one or more stacked blocks: a label row, a 'Date'
    # header row with hour numbers 1..24 to its right, then one data row per day.
    cat = category.upper()
    count = 0
    i = 0
    while i < len(rows):
        row = rows[i]
        if not row or str(row[0]).strip().lower() != "date":
            i += 1
            continue
        hours = [_as_float(v) for v in row[1:25]]
        label_row = rows[i - 1] if i > 0 else []
        labels = [str(v).upper() if v is not None else "" for v in label_row[1:25]]
        # a block usually carries one label for all of its hour columns
        block_label = next((l for l in labels if l), "")
        # data rows follow until the next block header or the end
        j = i + 1
        while j < len(rows):
            drow = rows[j]
            if drow and str(drow[0]).strip().lower() == "date":
                break
            day = _parse_day(drow[0]) if drow else None
            if day:
                for idx, h in enumerate(hours):
                    if h is None:
                        continue
                    col = idx + 1
                    value = _as_float(drow[col]) if col < len(drow) else None
                    if value is None:
                        continue
                    ts = hour_timestamp(day, int(h))
                    rec = records.setdefault(ts, new_record(ts))
                    label = labels[idx] if idx < len(labels) and labels[idx] else block_label
                    _apply_admie_value(rec, cat, label, value)
                    if "ADMIE" not in rec["sources"]:
                        rec["sources"].append("ADMIE")
                    rec.setdefault("sourceUrls", {})[category] = source_url
                    count += 1
            j += 1
        i = j
    return count


def _apply_admie_value(rec: Dict[str, Any], cat: str, label: str, value: float) -> None:
    load, ren, pricing = rec["load"], rec["renewables"], rec["pricing"]
    if "IMPORTSEXPORT" in cat or "INTERCONNECTION" in cat:
        for key, border in BORDER_LABELS.items():
            if key in label:
                rec["crossBorderFlowsMW"].setdefault(border, value)
                return
        return
    if "SYSTEMLOAD" in cat and "FORECAST" not in cat:
        _set_once(load, "actualMW", value)
    elif "LOADFORECAST" in cat:
        _set_once(load, "forecastMW", value)
    elif "SCADARES" in cat or ("RES" in cat and "FORECAST" not in cat and "REALTIME" in cat):
        _set_once(ren, "resTotalActualMW", value)
    elif "RESFORECAST" in cat:
        _set_once(ren, "resTotalForecastMW", value)
    elif "CURTAIL" in cat:
        _set_once(ren, "estimatedCurtailmentMW", value)
    elif "IMBALANCE" in cat or "SMP" in cat or "MARGINAL" in cat:
        _set_once(pricing, "systemMarginalPrice_EUR_MWh", value)


def _parse_any_ts(value: Any) -> Optional[str]:
    """Parse a datetime-ish value (datetime, 'YYYY-MM-DD HH:MM:SS', ISO) to hour-start UTC ISO."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.date().isoformat()}T{dt.hour:02d}:00:00Z"


def henex_hourly_aggregate(rows: List[Dict[str, Any]], day: str, source_url: str) -> List[Dict[str, Any]]:
    """Aggregate HEnEx DAM result rows (15-minute MTUs) into hourly records.

    Sheet columns: DELIVERY_MTU, MCP, TOTAL_TRADES, ... We average MCP and sum
    traded volume per delivery hour.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        lowered = {str(k).strip().lower(): v for k, v in row.items()}

        def find(*names: str) -> Any:
            for n in names:
                if n in lowered:
                    return lowered[n]
            return None

        ts = _parse_any_ts(find("delivery_mtu", "delivery start", "datetime", "timestamp"))
        if ts is None:
            hour = _as_float(find("hour", "period", "sort", "block"))
            ts = hour_timestamp(day, int(hour)) if hour is not None else hour_timestamp(day, 1)
        b = buckets.setdefault(ts, {"mcp": [], "volume": [], "load": []})
        mcp = _as_float(find("mcp", "mcp (eur/mwh)", "mcp (€/mwh)", "price (eur/mwh)"))
        if mcp is not None:
            b["mcp"].append(mcp)
        vol = _as_float(find("total_trades", "volume (mwh)", "total volume (mwh)", "volume"))
        if vol is not None:
            b["volume"].append(vol)
        load = _as_float(find("total system load (mw)", "system load (mw)", "load (mw)", "totalload"))
        if load is not None:
            b["load"].append(load)

    out: List[Dict[str, Any]] = []
    for ts in sorted(buckets):
        b = buckets[ts]
        rec = new_record(ts)
        if b["mcp"]:
            rec["pricing"]["henexMCP_EUR_MWh"] = sum(b["mcp"]) / len(b["mcp"])
        if b["volume"]:
            rec["henexVolumeMWh"] = sum(b["volume"])
        if b["load"]:
            rec["load"]["actualMW"] = sum(b["load"]) / len(b["load"])
        rec["sources"].append("HEnEx")
        rec["sourceUrls"] = {"HEnEx_DAM_Results": source_url}
        out.append(rec)
    return out


def ingest_entsoe_flow(record: Dict[str, Any], border: str, mw: float) -> None:
    record["crossBorderFlowsMW"][border] = mw
    if "ENTSO-E" not in record["sources"]:
        record["sources"].append("ENTSO-E")


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
    """HIGH on curtailment >= high threshold or negative price; MODERATE on
    curtailment >= moderate threshold or price below the low band; else LOW."""
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
    """Merge, sort, compute the stress indicator and drop internal helper keys."""
    out = []
    for rec in merge_hourly_records(records):
        rec["gridStressIndicator"] = compute_grid_stress(rec)
        rec["ingestedAt"] = utc_now_iso()
        rec.pop("sourceUrls", None)
        out.append(rec)
    return out
