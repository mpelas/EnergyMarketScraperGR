"""HTTP clients for the three data pillars:

1. ADMIE (IPTO) Operation & Market Files API
   https://www.admie.gr/getOperationMarketFilewRange?dateStart=..&dateEnd=..&FileCategory=..
2. HEnEx day-ahead market results (xlsx packages linked from the DAM publications page)
   https://www.enexgroup.gr/markets-publications-el-day-ahead-market
3. ENTSO-E Transparency Platform REST API (optional, needs a security token)
   https://transparency.entsoe.eu/api

Everything is defensive: any failure logs a warning and returns an empty list,
so one broken portal never kills the whole run.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

ADMIE_BASE = "https://www.admie.gr"
ADMIE_FILE_RANGE = f"{ADMIE_BASE}/getOperationMarketFilewRange"
HENEX_DAM_PAGE = "https://www.enexgroup.gr/markets-publications-el-day-ahead-market"
ENTSOE_API = "https://transparency.entsoe.eu/api"

# Bidding-zone EIC codes for Greece's borders
GR_BORDERS = {
    "IT_GR": ("10YIT-GRTN-----Y", "10YGR-HTSO------Y"),  # Italy - Greece (HVDC + HVAC)
    "BG_GR": ("10YBG-----------P", "10YGR-HTSO------Y"),
    "MK_GR": ("10YMK-MEPS----O", "10YGR-HTSO------Y"),
    "AL_GR": ("10YAL-KESH-----5", "10YGR-HTSO------Y"),
    "TR_GR": ("10YTR-TEIAS----W", "10YGR-HTSO------Y"),
}
GR_ZONE = "10YGR-HTSO------Y"

USER_AGENT = "Mozilla/5.0 (compatible; EnergyMarketScraperGR/1.0; +https://apify.com)"

DEFAULT_ADMIE_CATEGORIES = [
    "RealTimeSCADASystemLoad",
    "ISPDayAheadLoadForecast",
    "ISPWeekAheadLoadForecast",
    "DayAheadSystemMarginalPrice",
    "DayAheadRESForecast",
]


async def _get(client: httpx.AsyncClient, url: str, params: Optional[dict] = None, retries: int = 3) -> Optional[httpx.Response]:
    """GET with simple exponential backoff. Returns None on final failure."""
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url, params=params, timeout=45.0)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(f"{resp.status_code}", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp
        except Exception:
            if attempt == retries:
                return None
            await _sleep(delay)
            delay *= 2
    return None


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class EnergySources:
    def __init__(self, proxy_url: Optional[str] = None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html, text/csv, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/zip, */*"}
        self.client = httpx.AsyncClient(headers=headers, proxy=proxy_url, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ ADMIE
    async def fetch_admie_files(self, date_from: date, date_to: date, categories: List[str]) -> List[Dict[str, Any]]:
        """Return [{'category', 'sourceUrl', 'payload'}] for each downloadable ADMIE file."""
        out: List[Dict[str, Any]] = []
        params_base = {"dateStart": date_from.isoformat(), "dateEnd": date_to.isoformat()}
        for category in categories:
            resp = await _get(self.client, ADMIE_FILE_RANGE, params={**params_base, "FileCategory": category})
            if resp is None:
                continue
            try:
                meta = resp.json()
            except ValueError:
                continue
            entries = meta if isinstance(meta, list) else meta.get("files") or meta.get("data") or [meta]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("url") or entry.get("path") or entry.get("fileUrl") or entry.get("Url")
                if not path:
                    continue
                url = path if path.startswith("http") else f"{ADMIE_BASE}{path}"
                file_resp = await _get(self.client, url)
                if file_resp is None:
                    continue
                payload: Any
                ctype = file_resp.headers.get("Content-Type", "")
                if url.endswith(".json") or "json" in ctype:
                    try:
                        payload = file_resp.json()
                    except ValueError:
                        payload = file_resp.text
                elif url.endswith(".zip") or "zip" in ctype:
                    payload = _read_zip(file_resp.content)
                elif url.endswith((".xlsx", ".xls")) or "spreadsheet" in ctype:
                    payload = _read_excel(file_resp.content)
                elif url.endswith(".csv") or "csv" in ctype:
                    payload = _read_csv_text(file_resp.text)
                else:
                    payload = file_resp.text
                out.append({"category": category, "sourceUrl": url, "payload": payload})
        return out

    # ------------------------------------------------------------------ HEnEx
    async def fetch_henex_dam(self, day: date) -> List[Dict[str, Any]]:
        """Find the YYYYMMDD_EL-DAM_Results_EN xlsx on the HEnEx DAM publications page and parse it."""
        page = await _get(self.client, HENEX_DAM_PAGE)
        if page is None:
            return []
        soup = BeautifulSoup(page.text, "lxml")
        compact = day.strftime("%Y%m%d")
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            name = href.rsplit("/", 1)[-1]
            if compact in href and "dam" in href.lower() and "results" in name.lower() and href.lower().endswith((".xlsx", ".zip")):
                candidates.append(href if href.startswith("http") else f"https://www.enexgroup.gr{href}")
        if not candidates:  # fallback: any file with the date and Results in the name
            for a in soup.find_all("a", href=True):
                href = a["href"]
                name = href.rsplit("/", 1)[-1]
                if compact in href and "results" in name.lower() and href.lower().endswith((".xlsx", ".zip")):
                    candidates.append(href if href.startswith("http") else f"https://www.enexgroup.gr{href}")
        out: List[Dict[str, Any]] = []
        for url in candidates[:2]:
            resp = await _get(self.client, url)
            if resp is None:
                continue
            rows = _read_excel(resp.content) if url.lower().endswith(".xlsx") or "spreadsheet" in resp.headers.get("Content-Type", "") else _read_zip(resp.content)
            if rows:
                out.append({"category": "HEnEx_DAM_Results", "sourceUrl": url, "rows": rows, "day": day.isoformat()})
        return out

    # ------------------------------------------------------------------ ENTSO-E
    async def fetch_entsoe_flows(self, day: date, api_key: str) -> List[Dict[str, Any]]:
        """Physical cross-border flows per GR border, hourly. Returns [{'border', 'timestamp', 'mw'}]."""
        import xml.etree.ElementTree as ET

        out: List[Dict[str, Any]] = []
        start = f"{day.strftime('%Y%m%d')}0000"
        end_dt = day
        from datetime import timedelta

        end = f"{(end_dt + timedelta(days=1)).strftime('%Y%m%d')}0000"
        for border, (out_zone, in_zone) in GR_BORDERS.items():
            params = {
                "securityToken": api_key,
                "documentType": "A11",  # physical cross-border flows
                "processType": "A16",  # realised
                "outBiddingZone_Domain": out_zone,
                "inBiddingZone_Domain": in_zone,
                "periodStart": start,
                "periodEnd": end,
            }
            resp = await _get(self.client, ENTSOE_API, params=params)
            if resp is None:
                continue
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError:
                continue
            ns = {"e": "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"}
            for ts in root.findall(".//e:timeSeries", ns) or root.findall(".//{*}timeSeries"):
                for period in ts.findall(".//{*}Period"):
                    pstart = period.find("{*}timeInterval/{*}start")
                    for point in period.findall("{*}Point"):
                        pos = point.find("{*}position")
                        val = point.find("{*}quantity")
                        if pstart is None or pos is None or val is None:
                            continue
                        try:
                            mw = float(val.text)
                        except (TypeError, ValueError):
                            continue
                        out.append(
                            {
                                "border": border,
                                "timestamp": _hour_from_entsoe(pstart.text, int(pos.text)),
                                "mw": mw,
                            }
                        )
        return out


def _hour_from_entsoe(start_iso: str, position: int) -> str:
    """ENTSO-E period start is like 2026-11-17T23:00Z with 1-based positions (15-min or hourly)."""
    from datetime import datetime, timezone

    s = start_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    # assume hourly resolution: position 1..24
    hour = (dt.hour + (position - 1)) % 24
    return f"{dt.date().isoformat()}T{hour:02d}:00:00Z"


def _read_excel(content: bytes) -> List[Dict[str, Any]]:
    """Read the first sheet of an xlsx into a list of row dicts."""
    import pandas as pd

    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name=0)
    except Exception:
        return []
    df = df.dropna(how="all")
    return [{str(c): (None if pd.isna(v) else v) for c, v in row.items()} for _, row in df.iterrows()]


def _read_zip(content: bytes) -> List[Dict[str, Any]]:
    """Read the first xlsx/csv inside a zip package."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xlsx"):
                    return _read_excel(z.read(name))
                if name.lower().endswith(".csv"):
                    return _read_csv_text(z.read(name).decode("utf-8", errors="replace"))
    except Exception:
        return []
    return []


def _read_csv_text(text: str) -> List[Dict[str, Any]]:
    import csv

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if r]
