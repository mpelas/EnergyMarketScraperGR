"""HTTP clients for the three data pillars:

1. ADMIE (IPTO) Operation & Market Files API
   https://www.admie.gr/getOperationMarketFilewRange?dateStart=..&dateEnd=..&FileCategory=..
   Returns JSON metadata with `file_path`; files are .xls/.xlsx/.csv/.zip.
2. HEnEx day-ahead market results (xlsx via Liferay document library links)
   https://www.enexgroup.gr/markets-publications-el-day-ahead-market
3. ENTSO-E Transparency Platform REST API (optional, needs a security token)
   https://transparency.entsoe.eu/api

Everything is defensive: any failure logs a warning and returns an empty list,
so one broken portal never kills the whole run.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

ADMIE_BASE = "https://www.admie.gr"
ADMIE_FILE_RANGE = f"{ADMIE_BASE}/getOperationMarketFilewRange"
HENEX_BASE = "https://www.enexgroup.gr"
HENEX_DAM_PAGE = f"{HENEX_BASE}/markets-publications-el-day-ahead-market"
ENTSOE_API = "https://transparency.entsoe.eu/api"

# Bidding-zone EIC codes for Greece's borders (ENTSO-E)
GR_BORDERS = {
    "IT_GR": ("10YIT-GRTN-----Y", "10YGR-HTSO------Y"),
    "BG_GR": ("10YBG-----------P", "10YGR-HTSO------Y"),
    "MK_GR": ("10YMK-MEPS----O", "10YGR-HTSO------Y"),
    "AL_GR": ("10YAL-KESH-----5", "10YGR-HTSO------Y"),
    "TR_GR": ("10YTR-TEIAS----W", "10YGR-HTSO------Y"),
}

USER_AGENT = "Mozilla/5.0 (compatible; EnergyMarketScraperGR/1.0; +https://apify.com)"

DEFAULT_ADMIE_CATEGORIES = [
    "RealTimeSCADASystemLoad",
    "RealTimeSCADARES",
    "RealTimeSCADAImportsExports",
    "DayAheadLoadForecast",
    "DayAheadRESForecast",
    "ISP1DayAheadLoadForecast",
    "ISP1DayAheadRESForecast",
    "ExPostImbalancePricingResults",
    "SystemRealizationSCADA",
]


async def _get(client: httpx.AsyncClient, url: str, params: Optional[dict] = None, retries: int = 3) -> Optional[httpx.Response]:
    """GET with simple exponential backoff. Returns None on final failure."""
    import asyncio

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
            await asyncio.sleep(delay)
            delay *= 2
    return None


class EnergySources:
    def __init__(self, proxy_url: Optional[str] = None):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html, text/csv, application/vnd.ms-excel, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/zip, */*",
        }
        self.client = httpx.AsyncClient(headers=headers, proxy=proxy_url, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ ADMIE
    async def fetch_admie_files(self, date_from: date, date_to: date, categories: List[str]) -> List[Dict[str, Any]]:
        """Return [{'category', 'sourceUrl', 'rows'}] where rows is a raw table (list of lists)."""
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
                path = entry.get("file_path") or entry.get("url") or entry.get("path") or entry.get("fileUrl")
                if not path:
                    continue
                url = path if path.startswith("http") else f"{ADMIE_BASE}{path}"
                file_resp = await _get(self.client, url)
                if file_resp is None:
                    continue
                rows = _read_table(file_resp.content, url)
                if rows:
                    out.append({"category": category, "sourceUrl": url, "rows": rows})
        return out

    # ------------------------------------------------------------------ HEnEx
    async def fetch_henex_dam(self, day: date) -> List[Dict[str, Any]]:
        """Find the YYYYMMDD_EL-DAM_Results_EN file on the HEnEx DAM page and parse it.

        Links are Liferay document-library URLs (/c/document_library/get_file?uuid=...)
        whose anchor text is the file name, e.g. 20261117_EL-DAM_Results_EN_v01.
        Returns [{'category', 'sourceUrl', 'rows', 'day'}] with header-based rows.
        """
        page = await _get(self.client, HENEX_DAM_PAGE)
        if page is None:
            return []
        soup = BeautifulSoup(page.text, "lxml")
        compact = day.strftime("%Y%m%d")
        needle = f"{compact}_EL-DAM_Results_EN"
        candidates: List[str] = []
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"]
            if needle in text and "ResultsSummary" not in text and "PrelimResults" not in text:
                candidates.append(href if href.startswith("http") else f"{HENEX_BASE}{href}")
        if not candidates:  # fallback: href itself carries the file name
            for a in soup.find_all("a", href=True):
                href = a["href"]
                name = href.rsplit("/", 1)[-1]
                if needle in name and "ResultsSummary" not in name:
                    candidates.append(href if href.startswith("http") else f"{HENEX_BASE}{href}")
        out: List[Dict[str, Any]] = []
        for url in candidates[:2]:
            resp = await _get(self.client, url)
            if resp is None:
                continue
            rows = _read_table(resp.content, url, header=True)
            if rows:
                out.append({"category": "HEnEx_DAM_Results", "sourceUrl": url, "rows": rows, "day": day.isoformat()})
        return out

    # ------------------------------------------------------------------ ENTSO-E
    async def fetch_entsoe_flows(self, day: date, api_key: str) -> List[Dict[str, Any]]:
        """Physical cross-border flows per GR border, hourly. Returns [{'border', 'timestamp', 'mw'}]."""
        import xml.etree.ElementTree as ET

        out: List[Dict[str, Any]] = []
        start = f"{day.strftime('%Y%m%d')}0000"
        end = f"{(day + timedelta(days=1)).strftime('%Y%m%d')}0000"
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
            for ts in root.iter():
                if not ts.tag.endswith("timeSeries"):
                    continue
                for period in ts:
                    if not period.tag.endswith("Period"):
                        continue
                    pstart = None
                    for child in period.iter():
                        if child.tag.endswith("start"):
                            pstart = child.text
                            break
                    for point in period:
                        if not point.tag.endswith("Point"):
                            continue
                        pos = val = None
                        for child in point:
                            if child.tag.endswith("position"):
                                pos = child.text
                            elif child.tag.endswith("quantity"):
                                val = child.text
                        if pstart and pos and val:
                            try:
                                mw = float(val)
                                hour = _hour_from_entsoe(pstart, int(pos))
                            except (TypeError, ValueError):
                                continue
                            out.append({"border": border, "timestamp": hour, "mw": mw})
        return out


def _hour_from_entsoe(start_iso: str, position: int) -> str:
    """ENTSO-E period start like 2026-11-17T23:00Z with 1-based point positions."""
    from datetime import datetime, timezone

    s = start_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    hour = (dt.hour + (position - 1)) % 24
    return f"{dt.date().isoformat()}T{hour:02d}:00:00Z"


def _read_table(content: bytes, url: str, header: bool = False) -> List[Any]:
    """Read xls/xlsx/csv/zip into rows.

    header=False -> raw matrix (list of lists), used for ADMIE wide files.
    header=True  -> list of row dicts keyed by the first row, used for HEnEx.
    """
    import pandas as pd

    try:
        if url.lower().endswith(".zip") or content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                for name in z.namelist():
                    if name.lower().endswith((".xlsx", ".xls")):
                        content = z.read(name)
                        url = name
                        break
                    if name.lower().endswith(".csv"):
                        return _csv_rows(z.read(name).decode("utf-8", errors="replace"), header)
        if url.lower().endswith(".csv"):
            return _csv_rows(content.decode("utf-8", errors="replace"), header)
        if url.lower().endswith((".xls", ".xlsx")) or content[:2] in (b"\xd0\xcf", b"PK"):
            df = pd.read_excel(io.BytesIO(content), header=0 if header else None)
            if header:
                df = df.dropna(how="all")
                return [{str(c): (None if pd.isna(v) else v) for c, v in row.items()} for _, row in df.iterrows()]
            return [[None if pd.isna(v) else v for v in row] for row in df.values.tolist()]
    except Exception:
        return []
    return []


def _csv_rows(text: str, header: bool) -> List[Any]:
    import csv

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []
    if header:
        h = [str(x).strip() for x in rows[0]]
        return [dict(zip(h, r)) for r in rows[1:] if r]
    return rows
