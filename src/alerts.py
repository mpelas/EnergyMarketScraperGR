"""Threshold evaluation and webhook alert dispatch."""

from __future__ import annotations

from typing import Any, Dict, List


def evaluate_record(record: Dict[str, Any], thresholds: Dict[str, float]) -> List[Dict[str, Any]]:
    """Return a list of alert dicts for one hourly record (possibly empty)."""
    alerts: List[Dict[str, Any]] = []
    max_curt = float(thresholds.get("maxCurtailmentMW", 300.0))
    min_mcp = float(thresholds.get("minMCPPrice", 0.0))

    curt = record.get("renewables", {}).get("estimatedCurtailmentMW")
    if curt is not None and float(curt) >= max_curt:
        alerts.append(
            {
                "type": "HIGH_CURTAILMENT",
                "severity": "WARNING",
                "timestamp": record.get("timestamp"),
                "message": f"RES curtailment of {curt} MW at {record.get('timestamp')} (threshold {max_curt} MW).",
                "value": curt,
            }
        )
    mcp = record.get("pricing", {}).get("henexMCP_EUR_MWh")
    if mcp is not None and float(mcp) <= min_mcp:
        alerts.append(
            {
                "type": "NEGATIVE_OR_ZERO_PRICE",
                "severity": "CRITICAL" if float(mcp) < 0 else "WARNING",
                "timestamp": record.get("timestamp"),
                "message": f"HEnEx day-ahead MCP at {mcp} EUR/MWh at {record.get('timestamp')} (threshold {min_mcp}).",
                "value": mcp,
            }
        )
    return alerts


async def send_webhook(webhook_url: str, alert: Dict[str, Any]) -> bool:
    payload = {
        "text": f"Grid alert [{alert['severity']}]: {alert['type']} at {alert.get('timestamp')}",
        "alert": alert,
        "source": "mpelas/energy-market-scraper-gr",
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(webhook_url, json=payload)
            return resp.status_code < 400
    except Exception:
        return False
