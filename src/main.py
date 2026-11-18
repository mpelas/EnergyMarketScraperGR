"""Hellenic & Balkan Energy Market Tracker - Imbalance & RES Curtailment.

Unifies ADMIE (IPTO), HEnEx and (optionally) ENTSO-E data into one hourly
JSON dataset per run. Pay-per-event monetization: one-time `run_started`
charge plus `energy_record_pushed` per dataset record.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from apify import Actor

from .alerts import evaluate_record, send_webhook
from .normalize import (
    compute_grid_stress,
    finalize,
    hour_timestamp,
    ingest_admie_payload,
    ingest_entsoe_flow,
    ingest_henex_row,
    new_record,
    resolve_date,
)
from .sources import DEFAULT_ADMIE_CATEGORIES, EnergySources

PRIMARY_EVENT = "energy_record_pushed"
MAX_DAYS = 31


async def main() -> None:
    async with Actor:
        # One-time per-run charge (pay-per-event monetization; no-op locally)
        await Actor.charge("run_started")

        actor_input: Dict[str, Any] = await Actor.get_input() or {}
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)

        date_from = resolve_date(actor_input.get("dateFrom"), yesterday)
        date_to = resolve_date(actor_input.get("dateTo"), date_from)
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        if (date_to - date_from).days + 1 > MAX_DAYS:
            date_to = date_from + timedelta(days=MAX_DAYS - 1)

        include_admie = bool(actor_input.get("includeAdmieData", True))
        include_henex = bool(actor_input.get("includeHEnExPrices", True))
        include_entsoe = bool(actor_input.get("includeEntsoeFlows", False))
        entsoe_key = actor_input.get("entsoeApiKey") or None
        categories = actor_input.get("admieCategories") or DEFAULT_ADMIE_CATEGORIES
        webhook_url = actor_input.get("alertWebhookUrl") or None
        thresholds = actor_input.get("alertThresholds") or {"maxCurtailmentMW": 300.0, "minMCPPrice": 0.0}

        proxy_url = None
        proxy_input = actor_input.get("proxyConfiguration")
        if proxy_input:
            try:
                proxy_cfg = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
                if proxy_cfg:
                    proxy_url = await proxy_cfg.new_url()
                    Actor.log.info("Proxy configuration created from input.")
            except Exception as e:
                Actor.log.warning(f"Proxy configuration failed, continuing without proxy: {e}")

        Actor.log.info(
            f"Energy Market Tracker run: {date_from} .. {date_to} | "
            f"admie={include_admie} henex={include_henex} entsoe={include_entsoe}"
        )

        sources = EnergySources(proxy_url=proxy_url)
        raw_records: List[Dict[str, Any]] = []
        errors: List[str] = []

        try:
            # ---- ADMIE (range query covers all days at once) ----
            if include_admie:
                try:
                    files = await sources.fetch_admie_files(date_from, date_to, categories)
                    Actor.log.info(f"ADMIE: downloaded {len(files)} file(s)")
                    for f in files:
                        rec = new_record(f"{date_from.isoformat()}T00:00:00Z")
                        ingest_admie_payload(rec, f["payload"], f["category"], f["sourceUrl"])
                        raw_records.append(rec)
                except Exception as e:
                    errors.append(f"ADMIE: {e}")
                    Actor.log.warning(f"ADMIE fetch failed: {e}")

            # ---- HEnEx (per day) ----
            if include_henex:
                day = date_from
                while day <= date_to:
                    try:
                        packages = await sources.fetch_henex_dam(day)
                        if not packages:
                            errors.append(f"HEnEx: no results package found for {day}")
                        for pkg in packages:
                            for row in pkg["rows"]:
                                rec = new_record(hour_timestamp(pkg["day"], 1))
                                ingest_henex_row(rec, row, pkg["day"], pkg["sourceUrl"])
                                raw_records.append(rec)
                    except Exception as e:
                        errors.append(f"HEnEx {day}: {e}")
                        Actor.log.warning(f"HEnEx fetch failed for {day}: {e}")
                    day += timedelta(days=1)

            # ---- ENTSO-E cross-border flows (optional) ----
            if include_entsoe:
                if not entsoe_key:
                    Actor.log.warning("includeEntsoeFlows=true but no entsoeApiKey given; skipping ENTSO-E.")
                else:
                    try:
                        day = date_from
                        while day <= date_to:
                            flows = await sources.fetch_entsoe_flows(day, entsoe_key)
                            for f in flows:
                                rec = new_record(f["timestamp"])
                                ingest_entsoe_flow(rec, f["border"], f["mw"])
                                raw_records.append(rec)
                            day += timedelta(days=1)
                        Actor.log.info("ENTSO-E flows fetched.")
                    except Exception as e:
                        errors.append(f"ENTSO-E: {e}")
                        Actor.log.warning(f"ENTSO-E fetch failed: {e}")
        finally:
            await sources.close()

        records = finalize(raw_records)
        Actor.log.info(f"Normalized into {len(records)} hourly record(s)")

        # Store-test safety: never exit with an empty dataset
        if not records:
            fallback = new_record(f"{date_from.isoformat()}T00:00:00Z")
            fallback["gridStressIndicator"] = compute_grid_stress(fallback)
            fallback["note"] = "No source data available for the requested range; sources: " + "; ".join(errors or ["none returned data"])
            records = [fallback]

        alerts: List[Dict[str, Any]] = []
        pushed = 0
        for rec in records:
            alerts.extend(evaluate_record(rec, thresholds))
            await Actor.push_data(rec, charged_event_name=PRIMARY_EVENT)
            pushed += 1

        for alert in alerts:
            Actor.log.warning(f"ALERT {alert['type']}: {alert['message']}")
            if webhook_url:
                ok = await send_webhook(webhook_url, alert)
                if not ok:
                    Actor.log.warning(f"Webhook dispatch failed for {alert['type']}")

        summary = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "recordsPushed": pushed,
            "alertsTriggered": len(alerts),
            "sourcesUsed": sorted({s for r in records for s in r.get("sources", [])}),
            "errors": errors,
            "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        await Actor.set_value("run_summary", summary)
        if alerts:
            await Actor.set_value("alerts", alerts)

        Actor.log.info(f"Done. {pushed} records pushed, {len(alerts)} alert(s), summary saved to key-value store.")


if __name__ == "__main__":
    asyncio.run(main())
