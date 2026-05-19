"""
Short sale volume signal using FINRA consolidated market short-volume data.

Data source: https://cdn.finra.org/equity/regsho/daily/CNMSshvol{DATE}.txt
- Free, no auth required, covers all exchange-listed US equities
- Updated each trading day after market close
- Uses *short sale volume ratio* (ShortVolume / TotalVolume), NOT the
  traditional biweekly short interest report (short shares / float).
  Typical ratio: 45-55% is normal; >=65% = elevated short pressure.
"""

import time
import urllib.request
from datetime import date, timedelta


class ShortInterestSignal:
    _BASE_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
    _HEADERS = {"User-Agent": "curl/7.68.0", "Accept": "*/*"}

    def __init__(
        self,
        quiver_api_key=None,                         # not used; FINRA data is free
        high_short_interest_threshold: float = 0.65, # short vol ratio >= 65% = high pressure
        squeeze_price_change_threshold: float = 0.02,
        cache_ttl_hours: float = 12.0,
    ):
        self.high_short_interest_threshold = high_short_interest_threshold
        self.squeeze_price_change_threshold = squeeze_price_change_threshold
        self._cache_ttl = cache_ttl_hours * 3600
        self._daily_data: dict = {}    # {date: str, rows: {sym: (short_vol, total_vol)}}
        self._daily_data_time: float = 0.0

    def _find_latest_file(self):
        """Try the last 8 calendar days; return (date_str, content) for the most recent file."""
        for i in range(2, 9):
            d = date.today() - timedelta(days=i)
            url = self._BASE_URL.format(date=d.strftime("%Y%m%d"))
            try:
                req = urllib.request.Request(url, headers=self._HEADERS)
                r = urllib.request.urlopen(req, timeout=15)
                content = r.read().decode("utf-8")
                lines = content.strip().split("\n")
                if len(lines) > 2:
                    return d.strftime("%Y-%m-%d"), content
            except Exception:
                continue
        return None

    def _parse(self, content: str) -> dict:
        rows = {}
        for line in content.strip().split("\n"):
            line = line.strip("\r")
            if not line or line.startswith("Date"):
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            try:
                sym = parts[1].strip()
                short_vol = float(parts[2])
                total_vol = float(parts[4])
                if total_vol > 0:
                    rows[sym] = (short_vol, total_vol)
            except (ValueError, IndexError):
                continue
        return rows

    def _maybe_refresh(self):
        if (time.monotonic() - self._daily_data_time) < self._cache_ttl and self._daily_data:
            return
        found = self._find_latest_file()
        if found is None:
            print("[SI] FINRA file unavailable -- keeping stale data")
            return
        data_date, content = found
        self._daily_data = {"date": data_date, "rows": self._parse(content)}
        self._daily_data_time = time.monotonic()
        print(f"[SI] Loaded FINRA data: {data_date} ({len(self._daily_data['rows'])} symbols)")

    def get(self, symbol: str, current_price: float, prev_close: float) -> dict:
        def _neutral(note):
            return {
                "short_interest_pct": 0.0,
                "squeeze_setup": False,
                "squeeze_score": 0.0,
                "signal": 0,
                "note": note,
                "data_age_days": 0,
            }

        self._maybe_refresh()

        if not self._daily_data:
            return _neutral("SI data unavailable")

        rows = self._daily_data["rows"]
        data_date = self._daily_data["date"]

        if symbol not in rows:
            return _neutral(f"No FINRA short vol data for {symbol}")

        short_vol, total_vol = rows[symbol]
        ratio = short_vol / total_vol

        price_change_pct = (
            (current_price - prev_close) / prev_close
            if prev_close > 0 and prev_close != current_price
            else 0.0
        )

        si_component = min(ratio / self.high_short_interest_threshold, 1.0)
        price_component = (
            min(price_change_pct / self.squeeze_price_change_threshold, 1.0)
            if price_change_pct > 0 else 0.0
        )
        squeeze_score = round((si_component * 0.6) + (price_component * 0.4), 3)
        squeeze_setup = (
            ratio >= self.high_short_interest_threshold
            and price_change_pct >= self.squeeze_price_change_threshold
        )

        try:
            age_days = (date.today() - date.fromisoformat(data_date)).days
        except Exception:
            age_days = 0

        if squeeze_score > 0.7:
            sig = 1
            note = (
                f"Squeeze setup: short vol ratio={ratio:.1%} >= "
                f"{self.high_short_interest_threshold:.0%} + price "
                f"+{price_change_pct:.1%} | score={squeeze_score:.2f} | data={data_date}"
            )
        elif ratio > self.high_short_interest_threshold and price_change_pct < -0.01:
            sig = -1
            note = (
                f"Avoid: high short vol ratio={ratio:.1%} + price "
                f"{price_change_pct:.1%} (selling pressure) | data={data_date}"
            )
        else:
            sig = 0
            note = f"Short vol ratio={ratio:.1%} | score={squeeze_score:.2f} | data={data_date}"

        return {
            "short_interest_pct": round(ratio, 4),
            "squeeze_setup": squeeze_setup,
            "squeeze_score": squeeze_score,
            "signal": sig,
            "note": note,
            "data_age_days": age_days,
        }


if __name__ == "__main__":
    signal = ShortInterestSignal(
        high_short_interest_threshold=0.65,
        squeeze_price_change_threshold=0.02,
        cache_ttl_hours=12,
    )
    signal._daily_data = {
        "date": "2026-05-15",
        "rows": {
            "COST": (450_000, 600_000),   # 75% -- high SI, price rising
            "SPY":  (120_000, 300_000),   # 40% -- normal
            "AMZN": (200_000, 300_000),   # 66.7% -- high SI, price falling
        },
    }
    signal._daily_data_time = time.monotonic()

    print("--- Test 1: squeeze setup (+1) ---")
    r = signal.get("COST", current_price=802.00, prev_close=786.00)  # +2%
    assert r["signal"] == 1, f"Expected +1, got {r}"
    print(f"  signal={r['signal']} score={r['squeeze_score']} note={r['note']}")

    print("--- Test 2: normal SI, neutral (0) ---")
    r = signal.get("SPY", current_price=510.00, prev_close=509.00)
    assert r["signal"] == 0, f"Expected 0, got {r}"
    print(f"  signal={r['signal']} score={r['squeeze_score']} note={r['note']}")

    print("--- Test 3: high SI + falling price (-1) ---")
    r = signal.get("AMZN", current_price=183.00, prev_close=185.50)
    assert r["signal"] == -1, f"Expected -1, got {r}"
    print(f"  signal={r['signal']} note={r['note']}")

    print("--- Test 4: symbol not in data (neutral, fail open) ---")
    r = signal.get("AAPL", current_price=200.00, prev_close=198.00)
    assert r["signal"] == 0, f"Expected 0 (no data), got {r}"
    print(f"  signal={r['signal']} note={r['note']}")

    print("--- Test 5: FINRA unreachable (neutral, fail open) ---")
    s2 = ShortInterestSignal()
    s2._find_latest_file = lambda: None  # simulate total fetch failure
    r = s2.get("COST", 802.00, 786.00)
    assert r["signal"] == 0
    print(f"  signal={r['signal']} note={r['note']}")

    print("\nAll smoke tests passed.")
