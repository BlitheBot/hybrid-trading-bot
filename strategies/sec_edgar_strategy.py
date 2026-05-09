import asyncio
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import urllib.request
import pytz

from config import Config
from data.sp500_tickers import SP500_TICKERS
from strategies.base_strategy import BaseStrategy

_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&dateb=&owner=include&count=40&output=atom"
)
_NS_ATOM  = "http://www.w3.org/2005/Atom"
_NS_EDGAR = "https://www.sec.gov/"

# SEC requires descriptive User-Agent so they can contact you if needed:
# https://www.sec.gov/os/accessing-edgar-data
_HEADERS  = {"User-Agent": "HybridTradingBot/1.0 gamerdiamondknight@gmail.com"}

_SP500_SET = set(SP500_TICKERS)


def _http_get(url: str, timeout: int = 12) -> bytes | None:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        print(f"[EDGAR] GET failed ({url[:80]}): {e}")
        return None


def _cik_from_accession(accession: str) -> str:
    """'0000320193-24-000001' → '320193'"""
    try:
        return str(int(accession.split("-")[0]))
    except (ValueError, IndexError):
        return ""


def _parse_feed(xml_bytes: bytes) -> list[dict]:
    """Return list of {accession, cik_candidates, href} for each feed entry."""
    results = []
    try:
        root = ET.fromstring(xml_bytes)
        for entry in root.findall(f"{{{_NS_ATOM}}}entry"):
            # Try EDGAR-namespace elements first (present when feed declares xmlns:edgar)
            accession = (entry.findtext(f"{{{_NS_EDGAR}}}accession-number") or "").strip()
            cik_edgar = (entry.findtext(f"{{{_NS_EDGAR}}}CIK") or "").strip()
            href      = (entry.findtext(f"{{{_NS_EDGAR}}}filing-href") or "").strip()

            # Fallback: extract accession from <id> text
            if not accession:
                id_el = entry.find(f"{{{_NS_ATOM}}}id")
                if id_el is not None and id_el.text and "accession-number:" in id_el.text:
                    accession = id_el.text.split("accession-number:")[-1].strip()

            # Fallback: href from <link>
            if not href:
                link_el = entry.find(f"{{{_NS_ATOM}}}link")
                if link_el is not None:
                    href = link_el.get("href", "")

            if not accession:
                continue

            # Build ordered-unique list of CIK candidates to try
            cik_acc  = _cik_from_accession(accession)
            cik_href = ""
            if "/Archives/edgar/data/" in href:
                cik_href = href.split("/Archives/edgar/data/")[1].split("/")[0]
            cik_ns = str(int(cik_edgar)) if cik_edgar.isdigit() else ""

            seen: set[str] = set()
            candidates: list[str] = []
            for c in [cik_ns, cik_acc, cik_href]:
                if c and c not in seen:
                    seen.add(c)
                    candidates.append(c)

            results.append({"accession": accession, "cik_candidates": candidates, "href": href})
    except Exception as e:
        print(f"[EDGAR] Feed parse error: {e}")
    return results


def _find_form4_xml_url(cik_candidates: list[str], accession: str) -> str | None:
    """
    Fetch the filing index JSON for the first working CIK and return the Form 4 XML URL.
    Falls back to the conventional accession-number.xml filename if all index fetches fail.
    """
    accno_nodash = accession.replace("-", "")
    for cik in cik_candidates:
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{accno_nodash}/index.json"
        )
        data = _http_get(index_url)
        time.sleep(0.12)
        if not data:
            continue
        try:
            items = json.loads(data).get("directory", {}).get("item", [])
            # Prefer items explicitly typed "4"
            for item in items:
                if item.get("type") == "4" and item.get("name", "").endswith(".xml"):
                    return (
                        f"https://www.sec.gov/Archives/edgar/data/{cik}"
                        f"/{accno_nodash}/{item['name']}"
                    )
            # Any .xml that isn't an index file
            for item in items:
                name = item.get("name", "")
                if name.endswith(".xml") and "index" not in name.lower():
                    return (
                        f"https://www.sec.gov/Archives/edgar/data/{cik}"
                        f"/{accno_nodash}/{name}"
                    )
        except Exception as e:
            print(f"[EDGAR] Index parse error ({cik}/{accno_nodash}): {e}")

    # Last resort: standard EDGAR accession-number.xml convention
    if cik_candidates:
        cik = cik_candidates[0]
        return (
            f"https://www.sec.gov/Archives/edgar/data/{cik}"
            f"/{accno_nodash}/{accno_nodash}.xml"
        )
    return None


def _parse_form4_xml(xml_bytes: bytes) -> dict | None:
    """
    Parse a Form 4 ownership document.
    Returns dict with issuer ticker, owner info, and aggregated buy/sell totals.
    Only counts nonDerivativeTransaction rows (open-market buys and sells).
    """
    try:
        root = ET.fromstring(xml_bytes)

        issuer_el = root.find("issuer")
        if issuer_el is None:
            return None
        ticker_el = issuer_el.find("issuerTradingSymbol")
        ticker = (ticker_el.text or "").strip().upper() if ticker_el is not None else ""

        owner_name  = ""
        owner_title = "Insider"
        owner_el = root.find("reportingOwner")
        if owner_el is not None:
            name_el  = owner_el.find(".//rptOwnerName")
            title_el = owner_el.find(".//officerTitle")
            if name_el is not None and name_el.text:
                owner_name = name_el.text.strip()
            if title_el is not None and title_el.text:
                owner_title = title_el.text.strip()

        buy_value = sell_value = 0.0
        buy_shares = sell_shares = 0.0

        for tx in root.findall(".//nonDerivativeTransaction"):
            shares_el = tx.find(".//transactionShares/value")
            price_el  = tx.find(".//transactionPricePerShare/value")
            code_el   = tx.find(".//transactionAcquiredDisposedCode/value")
            if not (shares_el is not None and price_el is not None and code_el is not None):
                continue
            try:
                shares = float(shares_el.text or 0)
                price  = float(price_el.text or 0)
                code   = (code_el.text or "").strip().upper()
            except ValueError:
                continue
            if price <= 0 or shares <= 0:
                continue
            value = shares * price
            if code == "A":
                buy_value  += value
                buy_shares += shares
            elif code == "D":
                sell_value  += value
                sell_shares += shares

        return {
            "ticker":         ticker,
            "owner_name":     owner_name,
            "owner_title":    owner_title,
            "total_buy":      buy_value,
            "total_sell":     sell_value,
            "buy_shares":     buy_shares,
            "sell_shares":    sell_shares,
            "avg_buy_price":  buy_value  / buy_shares  if buy_shares  > 0 else 0.0,
            "avg_sell_price": sell_value / sell_shares if sell_shares > 0 else 0.0,
        }
    except Exception as e:
        print(f"[EDGAR] Form 4 XML parse error: {e}")
        return None


class SECEdgarStrategy(BaseStrategy):
    """
    Polls SEC EDGAR Form 4 filings (insider trades) every 30 minutes.
    Generates bullish signals on large insider buys and bearish signals on large insider sells.

    Strength tiers (aligned to SEC_EDGAR_AUTO_TRADE_THRESHOLD=13):
      Buy  $100k-500k → strength 7   (alert only)
      Buy  $500k-1M   → strength 9   (alert only)
      Buy  $1M+       → strength 14  (auto-trade)
      Sell $500k-1M   → strength 7   (alert only; sells never auto-trade)
      Sell $1M+       → strength 9   (alert only)
    """

    def __init__(self, name: str = "SEC EDGAR Insider"):
        super().__init__(name)
        self._seen_accessions: set[str]             = set()
        self._ticker_cooldowns: dict[str, datetime] = {}

    def _on_cooldown(self, ticker: str) -> bool:
        ts = self._ticker_cooldowns.get(ticker)
        return ts is not None and datetime.now(pytz.utc) - ts < timedelta(hours=4)

    def _mark_cooldown(self, ticker: str):
        self._ticker_cooldowns[ticker] = datetime.now(pytz.utc)

    @staticmethod
    def _score_buy(value: float) -> tuple[int, int, float]:
        if value >= 1_000_000:
            return 8, 9, 14.0
        elif value >= 500_000:
            return 7, 8, 9.0
        else:
            return 6, 8, 7.0

    @staticmethod
    def _score_sell(value: float) -> tuple[int, int, float]:
        if value >= 1_000_000:
            return 3, 7, 9.0
        else:
            return 4, 6, 7.0

    def _make_signals(self, form4: dict) -> list[dict]:
        ticker = form4["ticker"]
        if not ticker or ticker not in _SP500_SET:
            return []
        if self._on_cooldown(ticker):
            return []

        owner = form4["owner_name"]
        title = form4["owner_title"]
        signals: list[dict] = []

        if form4["total_buy"] >= Config.SEC_EDGAR_MIN_BUY_VALUE:
            val = form4["total_buy"]
            sh  = form4["buy_shares"]
            px  = form4["avg_buy_price"]
            score, conf, strength = self._score_buy(val)
            self._mark_cooldown(ticker)
            signals.append({
                "ticker":     ticker,
                "headline":   (
                    f"Insider BUY: {owner} ({title}) — "
                    f"{int(sh):,} shares @ ${px:.2f} (${val / 1e6:.2f}M)"
                ),
                "source":     "SEC EDGAR Form 4",
                "sentiment":  "bullish",
                "score":      score,
                "confidence": conf,
                "strength":   strength,
                "action":     "buy",
                "reasoning":  (
                    f"{title} {owner} purchased ${val / 1e6:.2f}M "
                    f"in open-market {ticker} shares"
                ),
                "auto_trade": strength >= Config.SEC_EDGAR_AUTO_TRADE_THRESHOLD,
            })

        if form4["total_sell"] >= Config.SEC_EDGAR_MIN_SELL_VALUE:
            val = form4["total_sell"]
            sh  = form4["sell_shares"]
            px  = form4["avg_sell_price"]
            score, conf, strength = self._score_sell(val)
            if not self._on_cooldown(ticker):
                self._mark_cooldown(ticker)
            signals.append({
                "ticker":     ticker,
                "headline":   (
                    f"Insider SELL: {owner} ({title}) — "
                    f"{int(sh):,} shares @ ${px:.2f} (${val / 1e6:.2f}M)"
                ),
                "source":     "SEC EDGAR Form 4",
                "sentiment":  "bearish",
                "score":      score,
                "confidence": conf,
                "strength":   strength,
                "action":     "sell",
                "reasoning":  (
                    f"{title} {owner} sold ${val / 1e6:.2f}M "
                    f"in open-market {ticker} shares"
                ),
                "auto_trade": False,  # insider sells never auto-trade
            })

        return signals

    def _scan_sync(self) -> list[dict]:
        signals: list[dict] = []

        feed_data = _http_get(_FEED_URL)
        if not feed_data:
            return signals

        entries     = _parse_feed(feed_data)
        new_entries = [e for e in entries if e["accession"] not in self._seen_accessions]
        print(f"[EDGAR] {len(entries)} entries in feed, {len(new_entries)} new")

        for entry in new_entries:
            self._seen_accessions.add(entry["accession"])

            xml_url = _find_form4_xml_url(entry["cik_candidates"], entry["accession"])
            if not xml_url:
                continue

            time.sleep(0.12)
            xml_data = _http_get(xml_url)
            if not xml_data:
                continue

            form4 = _parse_form4_xml(xml_data)
            if not form4:
                continue

            for sig in self._make_signals(form4):
                print(
                    f"[EDGAR] Signal: {sig['action'].upper()} {sig['ticker']} "
                    f"strength={sig['strength']} — {sig['headline']}"
                )
                signals.append(sig)

        return signals

    async def scan_once(self) -> list[dict]:
        return await asyncio.to_thread(self._scan_sync)

    # ── BaseStrategy stubs ─────────────────────────────────────────────────────

    def generate_signals(self, data, *args, **kwargs):
        return None

    def execute_trade(self, signal, trading_client, risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        pass
