# Hellenic & Balkan Energy Market Tracker (Imbalance & RES Curtailment)

An [Apify Actor](https://apify.com) that collects fragmented Greek and Southeastern European electricity-market data from **three official public sources**, merges them into **one clean, hourly, analysis-ready JSON dataset**, and flags "grid stress" events such as renewable-energy curtailment or negative electricity prices.

Think of it as a translator: Greece's energy data lives on several different websites, in different formats (Excel files, XML, web pages). This Actor visits all of them, reads them, and puts everything into one uniform table you can open in Excel, Google Sheets, Power BI, or feed into your own app.

---

## What problem does it solve?

If you follow the Greek energy market — as a trader, analyst, renewable developer, journalist, or curious citizen — you currently have to:

1. Go to **ADMIE (IPTO)** — the Greek grid operator — and manually download Excel files for electricity demand, solar/wind generation, and grid balancing.
2. Go to **HEnEx** — the Greek energy exchange — and download another Excel file for day-ahead electricity prices.
3. Optionally go to **ENTSO-E** — the European network of grid operators — and query their API for cross-border electricity flows.

Then you have to clean, align, and merge all of that by hand, hour by hour.

**This Actor does all of that automatically, every run.**

---

## The three data sources explained

### 1. ADMIE / IPTO — the Greek grid operator

- **Website:** [https://www.admie.gr](https://www.admie.gr)
- **API used:** `getOperationMarketFilewRange` — a public endpoint that lists operational and market files (Excel/CSV/ZIP) by date range and category.
- **What we get:**
  - **System load** — how much electricity Greece actually consumed vs. what was forecast (in MW, per hour).
  - **Renewable generation (RES)** — actual solar + wind output vs. forecast (in MW, per hour).
  - **Imports/Exports** — electricity flowing in/out of Greece through its interconnectors with neighbouring countries.
  - **Imbalance pricing / System Marginal Price (SMP)** — the price the grid operator charges when supply and demand don't match.
- **Why it matters:** When solar and wind produce more than the grid can absorb, the operator must "curtail" (switch off) some renewable plants. ADMIE publishes how many MW were curtailed. This Actor estimates curtailment as `RES forecast − RES actual` and exposes it as `estimatedCurtailmentMW`.

### 2. HEnEx — the Hellenic Energy Exchange

- **Website:** [https://www.enexgroup.gr](https://www.enexgroup.gr)
- **Page scraped:** [Day-Ahead Market results](https://www.enexgroup.gr/markets-publications-el-day-ahead-market)
- **What we get:** The official day-ahead market results file (`YYYYMMDD_EL-DAM_Results_EN_v##.xlsx`), which contains 15-minute (MTU) clearing prices. The Actor aggregates these into **hourly Market Clearing Prices (MCP, in EUR/MWh)** and total traded volume.
- **Why it matters:** The day-ahead price is the benchmark wholesale electricity price in Greece. Negative or very low prices signal oversupply (often from solar at midday); high prices signal scarcity.

### 3. ENTSO-E Transparency Platform — the European grid data hub (optional)

- **URL:** [https://transparency.entsoe.eu](https://transparency.entsoe.eu)
- **API endpoint:** `https://transparency.entsoe.eu/api`
- **Registration:** Free — you create an account on the portal, go to **Account Settings**, and generate a **security token** (an API key). Paste that token into the Actor input field `entsoeApiKey`.
- **What we get:** **Physical cross-border electricity flows** (in MW, per hour) on Greece's borders with Italy (IT), Bulgaria (BG), North Macedonia (MK), Albania (AL), and Turkey (TR). The Actor queries document type `A11` (physical flows) with process type `A16` (realised/actual), using the standard EIC bidding-zone codes (e.g. Greece = `10YGR-HTSO------Y`).
- **Why it matters:** It tells you whether Greece was importing or exporting power at each hour, and from/to which neighbour. Combined with prices, you can see if Greece imports cheap power at night and exports expensive power during peak.

> **ENTSO-E in one sentence:** it is the official EU-wide portal where all national grid operators publish their data in a standard format, so you don't have to scrape 40 different national websites.

---

## How the Actor combines the three sources

The Actor works in four steps:

```
Step 1: FETCH          Step 2: PARSE           Step 3: MERGE            Step 4: ENRICH
┌─────────────┐       ┌─────────────┐        ┌──────────────────┐     ┌──────────────────┐
│ ADMIE files │──────▶│ Excel/CSV   │───────▶│                  │     │                  │
│ HEnEx xlsx  │──────▶│ XML parsing │───────▶│  One record per  │────▶│ gridStress       │
│ ENTSO-E API │──────▶│ XML parsing │───────▶│  hour (timestamp)│     │ indicator +      │
└─────────────┘       └─────────────┘        │  (ADMIE+HEnEx    │     │ alerts           │
                                              │  +ENTSO-E merged)│     │                  │
                                              └──────────────────┘     └──────────────────┘
```

**In plain English:**

1. **Fetch** — For the date range you choose, the Actor downloads ADMIE files (one API call covers the whole range), scrapes the HEnEx day-ahead page for each day, and (if enabled) calls the ENTSO-E API once per day per border.
2. **Parse** — Excel files are read into tables; ENTSO-E XML is parsed to extract hourly flow values; HEnEx 15-minute prices are aggregated to hourly averages.
3. **Merge** — Everything is keyed by **hour timestamp** (UTC). If ADMIE says "at 13:00 load was 6,720 MW" and HEnEx says "at 13:00 the price was €42.50/MWh" and ENTSO-E says "at 13:00 300 MW flowed from Bulgaria to Greece", all three land in the **same record** for 13:00.
4. **Enrich** — The Actor computes a **grid stress indicator** and fires **alerts**:
   - `HIGH` — curtailment ≥ 300 MW, or negative prices (you are being paid to consume electricity!).
   - `MODERATE` — curtailment ≥ 50 MW, or prices below €20/MWh.
   - `LOW` — everything else.

If a source fails (e.g. ADMIE is down), the Actor logs a warning and continues with the other sources — one broken portal never kills the whole run.

---

## Output: one dataset item per hour

Each item in the Apify dataset looks like this:

```json
{
  "timestamp": "2026-11-17T13:00:00Z",
  "marketArea": "GR",
  "resolution": "1H",
  "load": { "forecastMW": 6850.5, "actualMW": 6720.0 },
  "renewables": {
    "solarForecastMW": 3200.0,
    "solarActualMW": 2890.0,
    "windForecastMW": 1400.0,
    "windActualMW": 950.0,
    "resTotalForecastMW": 4600.0,
    "resTotalActualMW": 3840.0,
    "estimatedCurtailmentMW": 450.0
  },
  "pricing": {
    "henexMCP_EUR_MWh": 42.5,
    "systemMarginalPrice_EUR_MWh": 55.2
  },
  "crossBorderFlowsMW": {
    "BG_GR": 300.0,
    "IT_GR": -150.0,
    "MK_GR": 50.0,
    "AL_GR": 0.0,
    "TR_GR": 0.0
  },
  "henexVolumeMWh": 6720.0,
  "gridStressIndicator": "HIGH",
  "sources": ["ADMIE", "HEnEx", "ENTSO-E"],
  "ingestedAt": "2026-11-18T14:30:00Z"
}
```

### Field-by-field glossary (no jargon)

| Field | What it means in plain English |
|---|---|
| `timestamp` | The hour this record describes, in UTC (e.g. `2026-11-17T13:00:00Z` = 13:00–14:00 UTC). |
| `marketArea` | Always `GR` (Greece). |
| `resolution` | Always `1H` — one record per hour. |
| `load.forecastMW` | How much electricity ADMIE predicted Greece would use that hour (megawatts). |
| `load.actualMW` | How much Greece actually used that hour. |
| `renewables.solarForecastMW` / `solarActualMW` | Predicted vs. actual solar generation. |
| `renewables.windForecastMW` / `windActualMW` | Predicted vs. actual wind generation. |
| `renewables.resTotalForecastMW` / `resTotalActualMW` | Total renewables (solar + wind + others), predicted vs. actual. |
| `renewables.estimatedCurtailmentMW` | How many MW of renewable energy had to be thrown away (switched off) because the grid couldn't take it. Roughly `forecast − actual`. |
| `pricing.henexMCP_EUR_MWh` | The average day-ahead market clearing price for that hour (EUR per megawatt-hour). This is the wholesale price of electricity. |
| `pricing.systemMarginalPrice_EUR_MWh` | The price set by the grid operator for imbalances (when supply ≠ demand). Usually higher than the market price. |
| `crossBorderFlowsMW` | Electricity physically flowing across each border. Positive = flowing INTO Greece, negative = flowing OUT. Keys: `BG_GR` (Bulgaria→Greece), `IT_GR` (Italy→Greece), `MK_GR`, `AL_GR`, `TR_GR`. |
| `henexVolumeMWh` | Total electricity traded on the day-ahead market that hour. |
| `gridStressIndicator` | `LOW` / `MODERATE` / `HIGH` — see rules above. A quick health check of the grid. |
| `sources` | Which of the three sources contributed data to this record. |
| `ingestedAt` | When the Actor created this record. |

---

## Alerts and run summary

- **Run summary** is saved to the key-value store under the key `run_summary` — it tells you how many records were pushed, which sources were used, and any errors.
- **Alerts** are saved under `alerts` and optionally sent to your **webhook URL** (e.g. a Slack incoming webhook, Discord, or your own endpoint) whenever a threshold is breached. Default thresholds: curtailment > 300 MW, or MCP price < 0 €/MWh (negative-price alert).

---

## Input parameters

See `.actor/input_schema.json` for the interactive form. Main fields:

| Field | What to enter |
|---|---|
| `dateFrom` | Start date. `YYYY-MM-DD`, or the keywords `today` / `yesterday`. Defaults to yesterday. **Required.** |
| `dateTo` | End date (inclusive). Defaults to `dateFrom`. Range is capped at **31 days**. |
| `includeAdmieData` | `true` to fetch ADMIE grid files (load, RES, imbalances). Default `true`. |
| `admieCategories` | Which ADMIE file categories to request (advanced). Defaults cover load, RES, imports/exports, forecasts, and imbalance pricing. |
| `includeHEnExPrices` | `true` to fetch HEnEx day-ahead prices. Default `true`. |
| `includeEntsoeFlows` | `true` to fetch ENTSO-E cross-border flows. Default `false` (because it needs an API key). |
| `entsoeApiKey` | Your **free ENTSO-E security token** from [transparency.entsoe.eu](https://transparency.entsoe.eu) → Account Settings. Required only if `includeEntsoeFlows` is `true`. |
| `alertWebhookUrl` | Optional HTTP endpoint that receives JSON alerts. |
| `alertThresholds` | `{ "maxCurtailmentMW": 300.0, "minMCPPrice": 0.0 }` — tweak to change when alerts fire. |
| `proxyConfiguration` | Optional Apify proxy settings (some operator portals rate-limit datacenter IPs). |

---

## How to get a free ENTSO-E API token (step by step)

1. Go to [https://transparency.entsoe.eu](https://transparency.entsoe.eu) and click **Sign in / Register**.
2. Create a free account (email + password). Confirm your email.
3. Log in, click your name (top right) → **Account Settings**.
4. Open the **Security Token** tab and click **Generate** — copy the token.
5. Paste it into the Actor input field **`entsoeApiKey`** and set **`includeEntsoeFlows`** to `true`.

That's it. The token is free and does not expire unless you regenerate it.

---

## Monetization (pricing)

This Actor uses **pay-per-event** pricing on Apify:

- **$1.00** one-time per run (`run_started` event).
- **$0.005** per pushed hourly record (`energy_record_pushed` — the primary event).

Example: a 7-day run produces ~168 hourly records → $1.00 + 168 × $0.005 = **$1.84**.

---

## Local development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v   # run unit tests
apify run                                # local run (uses storage/ + INPUT file)
```

---

## FAQ

**Q: Do I need an ENTSO-E account?**
No. ENTSO-E is optional. If you leave `includeEntsoeFlows` = `false`, the Actor still gives you ADMIE + HEnEx data (load, renewables, curtailment, prices, SMP). Enable ENTSO-E only if you also want cross-border flows.

**Q: What does "curtailment" mean?**
When there is more renewable energy (sun + wind) than the grid can use at that moment, the grid operator tells some solar/wind farms to switch off. That wasted energy is "curtailed". High curtailment = a sign the grid needs more storage or more interconnectors.

**Q: Why are prices sometimes negative?**
When there is a huge surplus of solar/wind and demand is low (e.g. Sunday midday), producers may pay to keep their plants running (because shutting down and restarting is expensive), so the market price goes below zero. Negative price hours are a strong "grid stress" signal.

**Q: What timezone are the timestamps?**
All timestamps are **UTC** (the `Z` suffix). Greece is UTC+2 (winter) / UTC+3 (summer), so 13:00 UTC = 15:00 Athens time in winter.

**Q: Can I get more than 31 days?**
The input caps a single run at 31 days. Just run the Actor multiple times with different date ranges, or schedule it daily/weekly via an Apify Schedule.

**Q: The data looks empty / has a "note" field.**
That means none of the sources returned data for your date range (e.g. you picked a future date, or a source was temporarily down). Check the `run_summary` in the key-value store — it lists any errors per source.

---

## License & disclaimer

Data is sourced from public APIs and publications of ADMIE (IPTO), HEnEx, and ENTSO-E. The Actor only redistributes publicly available data; always check the terms of use of each source. This Actor is for informational/educational purposes and is not investment advice.
