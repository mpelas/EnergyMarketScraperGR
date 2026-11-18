import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.normalize import (  # noqa: E402
    compute_grid_stress,
    finalize,
    hour_timestamp,
    ingest_admie_wide,
    ingest_entsoe_flow,
    henex_hourly_aggregate,
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

    def test_henex_hourly_aggregate(self):
        # 15-minute MTU rows like the real EL-DAM_Results sheet
        rows = [
            {"DELIVERY_MTU": "2026-11-17 13:00:00", "MCP": 40.0, "TOTAL_TRADES": 100.0},
            {"DELIVERY_MTU": "2026-11-17 13:15:00", "MCP": 44.0, "TOTAL_TRADES": 120.0},
            {"DELIVERY_MTU": "2026-11-17 14:00:00", "MCP": 50.0, "TOTAL_TRADES": 90.0},
        ]
        recs = henex_hourly_aggregate(rows, "2026-11-17", "https://www.enexgroup.gr/x.xlsx")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["timestamp"], "2026-11-17T13:00:00Z")
        self.assertAlmostEqual(recs[0]["pricing"]["henexMCP_EUR_MWh"], 42.0)
        self.assertAlmostEqual(recs[0]["henexVolumeMWh"], 220.0)
        self.assertIn("HEnEx", recs[0]["sources"])

    def test_admie_wide_ingestion(self):
        # ADMIE wide .xls layout: label row, 'Date' header row with hours 1..24, data rows
        rows = [
            [None, "NET LOAD WITHOUT CRETE (MWh)"] + [None] * 23,
            ["Date"] + list(range(1, 25)),
            ["17-11-2026", 4196, 3887] + [None] * 22,
        ]
        records = {}
        n = ingest_admie_wide(rows, "RealTimeSCADASystemLoad", "https://www.admie.gr/f.xls", records)
        self.assertEqual(n, 2)
        rec = records["2026-11-17T00:00:00Z"]
        self.assertEqual(rec["load"]["actualMW"], 4196.0)
        self.assertEqual(records["2026-11-17T01:00:00Z"]["load"]["actualMW"], 3887.0)
        self.assertIn("ADMIE", rec["sources"])

    def test_admie_wide_imports_exports(self):
        # stacked per-border blocks: label row, 'Date' row, one data row
        rows = [
            [None, "ALBANIA REALTIME NET (MWh)"] + [None] * 23,
            ["Date"] + list(range(1, 25)),
            ["17-11-2026", -180] + [None] * 23,
            [None, "BULGARIA REALTIME NET (MWh)"] + [None] * 23,
            ["Date"] + list(range(1, 25)),
            ["17-11-2026", 456] + [None] * 23,
        ]
        records = {}
        n = ingest_admie_wide(rows, "RealTimeSCADAImportsExports", "https://www.admie.gr/g.xls", records)
        self.assertEqual(n, 2)
        rec = records["2026-11-17T00:00:00Z"]
        self.assertEqual(rec["crossBorderFlowsMW"]["AL_GR"], -180.0)
        self.assertEqual(rec["crossBorderFlowsMW"]["BG_GR"], 456.0)

    def test_entsoe_flow(self):
        rec = new_record("2026-11-17T10:00:00Z")
        ingest_entsoe_flow(rec, "BG_GR", 300.0)
        self.assertEqual(rec["crossBorderFlowsMW"]["BG_GR"], 300.0)
        self.assertIn("ENTSO-E", rec["sources"])

    def test_merge_same_hour(self):
        a = new_record("2026-11-17T10:00:00Z")
        a["load"]["actualMW"] = 6000.0
        a["sources"].append("ADMIE")
        b = new_record("2026-11-17T10:00:00Z")
        b["pricing"]["henexMCP_EUR_MWh"] = 30.0
        b["sources"].append("HEnEx")
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
