import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.normalize import (  # noqa: E402
    compute_grid_stress,
    finalize,
    hour_timestamp,
    ingest_admie_payload,
    ingest_entsoe_flow,
    ingest_henex_row,
    merge_hourly_records,
    new_record,
    resolve_date,
)
from src.alerts import evaluate_record  # noqa: E402
from datetime import date, timedelta  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_hour_timestamp(self):
        self.assertEqual(hour_timestamp("2026-11-18", 1), "2026-11-18T00:00:00Z")
        self.assertEqual(hour_timestamp("2026-11-18", 24), "2026-11-18T23:00:00Z")

    def test_henex_row_ingestion(self):
        rec = new_record(hour_timestamp("2026-11-17", 1))
        ingest_henex_row(
            rec,
            {"Hour": 5, "MCP (EUR/MWh)": "42.5", "Total System Load (MW)": 6720.0},
            "2026-11-17",
            "https://www.enexgroup.gr/x.xlsx",
        )
        self.assertEqual(rec["timestamp"], "2026-11-17T04:00:00Z")
        self.assertEqual(rec["pricing"]["henexMCP_EUR_MWh"], 42.5)
        self.assertEqual(rec["load"]["actualMW"], 6720.0)
        self.assertIn("HEnEx", rec["sources"])

    def test_admie_payload_ingestion(self):
        rec = new_record("2026-11-17T00:00:00Z")
        ingest_admie_payload(
            rec,
            {"SystemLoad": 6100.0, "CurtailmentMW": 450.0, "SMP": 55.2, "WindActual": 900.0},
            "RealTimeSCADASystemLoad",
            "https://www.admie.gr/f.json",
        )
        self.assertEqual(rec["load"]["actualMW"], 6100.0)
        self.assertEqual(rec["renewables"]["estimatedCurtailmentMW"], 450.0)
        self.assertEqual(rec["pricing"]["systemMarginalPrice_EUR_MWh"], 55.2)
        self.assertIn("ADMIE", rec["sources"])

    def test_entsoe_flow(self):
        rec = new_record("2026-11-17T10:00:00Z")
        ingest_entsoe_flow(rec, "BG_GR", 300.0)
        self.assertEqual(rec["crossBorderFlowsMW"]["BG_GR"], 300.0)
        self.assertIn("ENTSO-E", rec["sources"])

    def test_merge_same_hour(self):
        a = new_record("2026-11-17T10:00:00Z")
        ingest_admie_payload(a, {"SystemLoad": 6000.0}, "cat", "u1")
        b = new_record("2026-11-17T10:00:00Z")
        ingest_henex_row(b, {"Hour": 11, "MCP (EUR/MWh)": 30.0}, "2026-11-17", "u2")
        merged = merge_hourly_records([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["load"]["actualMW"], 6000.0)
        self.assertEqual(merged[0]["pricing"]["henexMCP_EUR_MWh"], 30.0)
        self.assertEqual(sorted(merged[0]["sources"]), ["ADMIE", "HEnEx"])

    def test_grid_stress(self):
        rec = new_record("2026-11-17T10:00:00Z")
        self.assertEqual(compute_grid_stress(rec), "LOW")
        rec["renewables"]["estimatedCurtailmentMW"] = 500.0
        self.assertEqual(compute_grid_stress(rec), "HIGH")
        rec["renewables"]["estimatedCurtailmentMW"] = 100.0
        self.assertEqual(compute_grid_stress(rec), "MODERATE")
        rec["renewables"]["estimatedCurtailmentMW"] = None
        rec["pricing"]["henexMCP_EUR_MWh"] = -5.0
        self.assertEqual(compute_grid_stress(rec), "HIGH")

    def test_finalize_sorts_and_sets_indicator(self):
        recs = [new_record("2026-11-17T12:00:00Z"), new_record("2026-11-17T01:00:00Z")]
        out = finalize(recs)
        self.assertEqual([r["timestamp"] for r in out], ["2026-11-17T01:00:00Z", "2026-11-17T12:00:00Z"])
        self.assertTrue(all(r["gridStressIndicator"] in ("LOW", "MODERATE", "HIGH") for r in out))

    def test_alerts(self):
        rec = new_record("2026-11-17T10:00:00Z")
        rec["pricing"]["henexMCP_EUR_MWh"] = -1.0
        alerts = evaluate_record(rec, {"maxCurtailmentMW": 300.0, "minMCPPrice": 0.0})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "CRITICAL")

    def test_resolve_date(self):
        today = date.today()
        self.assertEqual(resolve_date("today", today), today)
        self.assertEqual(resolve_date("yesterday", today), today - timedelta(days=1))
        self.assertEqual(resolve_date("2026-11-17", today), date(2026, 11, 17))
        self.assertEqual(resolve_date(None, today), today)
        self.assertEqual(resolve_date("bogus", date(2026, 1, 1)), date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
