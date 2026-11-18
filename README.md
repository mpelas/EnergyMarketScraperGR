# Hellenic & Balkan Energy Market Tracker (Imbalance & RES Curtailment)

Apify Actor that unifies fragmented Greek / Southeastern European energy market
data into one clean, hourly, analysis-ready JSON dataset.

## Data pillars

| Pillar | Source | What is extracted |
|---|---|---|
| Grid & curtailments | ADMIE (IPTO) Operation & Market Files API (`getOperationMarketFilewRange`) | system load, RES generation vs forecast, curtailment MW, System Marginal Price |
| Spot pricing | HEnEx day-ahead market publications (`YYYYMMDD_EL-DAM_Results_EN_v##.xlsx`) | hourly Market Clearing Price (EUR/MWh), volumes |
| Cross-border flows | ENTSO-E Transparency Platform (optional, needs free API token) | physical flows on GR borders: IT, BG, MK, AL, TR |

## Output

One dataset item per hour (merged across sources):

```json
{
  "timestamp": "2026-11-17T13:00:00Z",
  "marketArea": "GR",
  "resolution": "1H",
  "load": { "forecastMW": 6850.5, "actualMW": 6720.0 },
  "renewables": { "solarForecastMW": 3200.0, "solarActualMW": 2890.0, "windForecastMW": 1400.0, "windActualMW": 950.0, "estimatedCurtailmentMW": 450.0 },
  "pricing": { "henexMCP_EUR_MWh": 42.5, "systemMarginalPrice_EUR_MWh": 55.2 },
  "crossBorderFlowsMW": { "BG_GR": 300.0, "IT_GR": -150.0 },
  "gridStressIndicator": "HIGH",
  "sources": ["ADMIE", "HEnEx"],
  "ingestedAt": "2026-11-18T14:30:00Z"
}
```

`gridStressIndicator` is `HIGH` on curtailment >= 300 MW or negative prices,
`MODERATE` on any curtailment >= 50 MW or prices below 20 EUR/MWh, else `LOW`.

Run statistics go to the key-value store under `run_summary`; triggered
threshold alerts under `alerts` and are optionally POSTed to your webhook URL.

## Input

See `.actor/input_schema.json`. Main fields: `dateFrom` / `dateTo`
(YYYY-MM-DD, `today`, `yesterday`), `includeAdmieData`, `admieCategories`,
`includeHEnExPrices`, `includeEntsoeFlows` + `entsoeApiKey`,
`alertWebhookUrl`, `alertThresholds`, `proxyConfiguration`.

## Monetization

Pay-per-event: `$1.00` one-time per run (`run_started`) plus `$0.005` per
pushed hourly record (`energy_record_pushed`, primary event).

## Local development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v   # unit tests
apify run                                # local run (uses storage/ + INPUT)
```
