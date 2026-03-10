"""
Tushare vendor for TradingAgents — A-share, futures, gold data support.

Implements the same function signatures as y_finance.py / alpha_vantage.py
so it can be plugged into VENDOR_METHODS in interface.py.

Supported ticker formats:
  A-share:  600519 / 000001 / 600519.SH / 000001.SZ
  Futures:  CU2401.SHF / RB2405.SHF / AU2406.SGE
  Gold:     AU9999.SGE / AU(T+D).SGE
"""

from typing import Annotated
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os
import re
import json
from pathlib import Path

_ts_api = None
_SECURITY_META_CACHE: dict[str, dict[str, str]] = {}
_NEWS_CACHE_DIR = Path(__file__).resolve().parent / "data_cache" / "tushare_news_cache"
_NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_api():
    """Lazy-init tushare pro_api with token from env."""
    global _ts_api
    if _ts_api is None:
        import tushare as ts
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            raise RuntimeError(
                "TUSHARE_TOKEN not set. Add it to .env or environment."
            )
        _ts_api = ts.pro_api(token)
    return _ts_api


# ── Ticker normalisation ────────────────────────────────────────────────────

_EXCHANGE_MAP = {
    "6": "SH",  # 6xxxxx -> Shanghai
    "0": "SZ",  # 0xxxxx -> Shenzhen
    "3": "SZ",  # 3xxxxx -> ChiNext (Shenzhen)
    "4": "BJ",  # 4xxxxx / 8xxxxx -> Beijing
    "8": "BJ",
}


def _normalise_ts_code(symbol: str) -> str:
    """Convert bare digits or dotted code to tushare ts_code.

    Examples:
        600519      -> 600519.SH
        000001      -> 000001.SZ
        600519.SH   -> 600519.SH  (no-op)
        CU2401.SHF  -> CU2401.SHF (no-op)
    """
    symbol = symbol.strip().upper()
    if "." in symbol:
        return symbol
    if re.match(r"^\d{6}$", symbol):
        prefix = symbol[0]
        exchange = _EXCHANGE_MAP.get(prefix, "SH")
        return f"{symbol}.{exchange}"
    return symbol


def is_a_share(symbol: str) -> bool:
    """Return True if symbol looks like an A-share ticker."""
    s = symbol.strip().upper()
    if re.match(r"^\d{6}$", s):
        return True
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", s):
        return True
    return False


def is_futures(symbol: str) -> bool:
    """Return True if symbol looks like a futures contract."""
    s = symbol.strip().upper()
    if re.match(r"^[A-Z]{1,3}\d{3,4}\.(SHF|DCE|CZC|CFX|INE|GFE)$", s):
        return True
    return False


def is_gold_spot(symbol: str) -> bool:
    """Return True if symbol is a Shanghai Gold Exchange instrument."""
    s = symbol.strip().upper()
    if ".SGE" in s or s.startswith("AU") and "SGE" in s:
        return True
    return False


def is_tushare_symbol(symbol: str) -> bool:
    """Return True if this symbol should be routed to Tushare."""
    return is_a_share(symbol) or is_futures(symbol) or is_gold_spot(symbol)


# ── Date helpers ─────────────────────────────────────────────────────────────

def _to_ts_date(date_str: str) -> str:
    """Convert yyyy-mm-dd to YYYYMMDD."""
    return date_str.replace("-", "")


def _df_to_csv_report(df, title: str, symbol: str) -> str:
    """Convert a tushare DataFrame to a readable CSV report string."""
    if df is None or df.empty:
        return f"No {title} data found for '{symbol}'"
    csv_string = df.to_csv(index=False)
    header = f"# {title} for {symbol}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def _cache_path(prefix: str, key: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", key)
    return _NEWS_CACHE_DIR / f"{prefix}_{safe_key}.json"


def _load_cached_text(prefix: str, key: str) -> str | None:
    p = _cache_path(prefix, key)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        return payload.get("text")
    except Exception:
        return None


def _save_cached_text(prefix: str, key: str, text: str) -> None:
    p = _cache_path(prefix, key)
    try:
        p.write_text(
            json.dumps(
                {
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "text": text,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_security_meta(symbol: str) -> dict[str, str]:
    ts_code = _normalise_ts_code(symbol)
    cached = _SECURITY_META_CACHE.get(ts_code)
    if cached:
        return cached
    meta = {"ts_code": ts_code, "code": ts_code.split(".")[0], "name": ""}
    try:
        pro = _get_api()
        if is_a_share(ts_code):
            df = pro.stock_basic(ts_code=ts_code, fields="ts_code,symbol,name")
            if df is not None and not df.empty:
                row = df.iloc[0]
                meta["code"] = str(row.get("symbol") or meta["code"])
                meta["name"] = str(row.get("name") or "").strip()
    except Exception:
        pass
    _SECURITY_META_CACHE[ts_code] = meta
    return meta


def _name_aliases(name: str) -> list[str]:
    if not name:
        return []
    aliases = {name.strip()}
    for suffix in ("股份有限公司", "股份", "有限责任公司", "有限公司", "公司"):
        if name.endswith(suffix):
            aliases.add(name[: -len(suffix)].strip())
    return [a for a in aliases if a]


def _format_text_snippet(value: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _a_share_disclosure_report(
    ticker: str,
    start_date: str,
    end_date: str,
    limit: int = 12,
) -> str:
    ts_code = _normalise_ts_code(ticker)
    if not is_a_share(ts_code):
        return ""

    pro = _get_api()
    sections: list[str] = []
    ts_start = _to_ts_date(start_date)
    ts_end = _to_ts_date(end_date)

    try:
        anns = pro.anns_d(ts_code=ts_code, start_date=ts_start, end_date=ts_end)
        if anns is not None and not anns.empty:
            lines = []
            for _, row in anns.head(limit).iterrows():
                ann_date = row.get("ann_date", "")
                title = _format_text_snippet(row.get("title", ""), 120)
                url = str(row.get("url", "") or "").strip()
                line = f"- [{ann_date}] {title}"
                if url:
                    line += f" | PDF: {url}"
                lines.append(line)
            if lines:
                sections.append("## Company Announcements\n" + "\n".join(lines))
    except Exception as exc:
        sections.append(f"## Company Announcements\nUnavailable: {exc}")

    try:
        qa_func = None
        if ts_code.endswith(".SH"):
            qa_func = getattr(pro, "irm_qa_sh", None)
        elif ts_code.endswith(".SZ"):
            qa_func = getattr(pro, "irm_qa_sz", None)
        if qa_func is not None:
            qa_df = qa_func(ts_code=ts_code, start_date=ts_start, end_date=ts_end)
            if qa_df is not None and not qa_df.empty:
                lines = []
                for _, row in qa_df.head(8).iterrows():
                    trade_date = row.get("trade_date", "")
                    question = _format_text_snippet(row.get("q", ""), 80)
                    answer = _format_text_snippet(row.get("a", ""), 120)
                    lines.append(f"- [{trade_date}] Q: {question}\n  A: {answer}")
                if lines:
                    sections.append("## Investor Q&A\n" + "\n".join(lines))
    except Exception as exc:
        sections.append(f"## Investor Q&A\nUnavailable: {exc}")

    return "\n\n".join(section for section in sections if section.strip())


# ── Core stock data (OHLCV) ─────────────────────────────────────────────────

def get_stock_data(
    symbol: Annotated[str, "ticker symbol"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    """Get daily OHLCV data. Supports A-shares, futures, gold spot."""
    pro = _get_api()
    ts_code = _normalise_ts_code(symbol)

    if is_futures(ts_code):
        df = pro.fut_daily(
            ts_code=ts_code,
            start_date=_to_ts_date(start_date),
            end_date=_to_ts_date(end_date),
        )
        return _df_to_csv_report(df, "Futures Daily Data", ts_code)

    if is_gold_spot(ts_code):
        df = pro.sge_daily(
            ts_code=ts_code,
            start_date=_to_ts_date(start_date),
            end_date=_to_ts_date(end_date),
        )
        return _df_to_csv_report(df, "Gold Spot Daily Data", ts_code)

    # A-share daily
    df = pro.daily(
        ts_code=ts_code,
        start_date=_to_ts_date(start_date),
        end_date=_to_ts_date(end_date),
    )
    return _df_to_csv_report(df, "Stock Daily Data", ts_code)


# ── Technical indicators ─────────────────────────────────────────────────────

def get_indicators(
    symbol: Annotated[str, "ticker symbol"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current date yyyy-mm-dd"],
    look_back_days: Annotated[int, "days to look back"] = 60,
) -> str:
    """Calculate technical indicators using stockstats on Tushare OHLCV data.

    Reuses the same stockstats library as yfinance, just with Tushare as
    the data source.
    """
    import pandas as pd
    from stockstats import wrap

    pro = _get_api()
    ts_code = _normalise_ts_code(symbol)

    # Fetch enough history for indicator warm-up (300 extra days)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    fetch_start = curr_dt - relativedelta(days=look_back_days + 300)
    look_back_start = curr_dt - relativedelta(days=look_back_days)

    if is_futures(ts_code):
        df = pro.fut_daily(
            ts_code=ts_code,
            start_date=_to_ts_date(fetch_start.strftime("%Y-%m-%d")),
            end_date=_to_ts_date(curr_date),
        )
    elif is_gold_spot(ts_code):
        df = pro.sge_daily(
            ts_code=ts_code,
            start_date=_to_ts_date(fetch_start.strftime("%Y-%m-%d")),
            end_date=_to_ts_date(curr_date),
        )
    else:
        df = pro.daily(
            ts_code=ts_code,
            start_date=_to_ts_date(fetch_start.strftime("%Y-%m-%d")),
            end_date=_to_ts_date(curr_date),
        )

    if df is None or df.empty:
        return f"No data found for {ts_code}"

    # Rename columns to stockstats convention
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl == "trade_date":
            col_map[c] = "Date"
        elif cl == "vol":
            col_map[c] = "Volume"
        elif cl in ("open", "high", "low", "close"):
            col_map[c] = cl.capitalize()
    df = df.rename(columns=col_map)

    df = df.sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")

    ss = wrap(df)
    try:
        ss[indicator]  # trigger calculation
    except Exception as e:
        return f"Indicator '{indicator}' calculation failed: {e}"

    # Build result for the look-back window
    lines = []
    for _, row in ss.iterrows():
        d = row["Date"]
        if d >= look_back_start.strftime("%Y-%m-%d") and d <= curr_date:
            val = row.get(indicator, "N/A")
            if pd.isna(val):
                val = "N/A"
            lines.append(f"{d}: {val}")

    return (
        f"## {indicator} for {ts_code} "
        f"({look_back_start.strftime('%Y-%m-%d')} to {curr_date}):\n\n"
        + "\n".join(lines)
    )


# ── Fundamental data ─────────────────────────────────────────────────────────

def get_fundamentals(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get financial indicators (PE, ROE, EPS, etc.) from Tushare fina_indicator."""
    pro = _get_api()
    ts_code = _normalise_ts_code(ticker)

    try:
        df = pro.fina_indicator(ts_code=ts_code)
        if df is None or df.empty:
            return f"No financial indicator data for {ts_code}"

        # Take the most recent report
        row = df.iloc[0]
        fields = [
            ("Report Period", row.get("end_date")),
            ("EPS", row.get("eps")),
            ("Diluted EPS", row.get("dt_eps")),
            ("ROE (%)", row.get("roe")),
            ("ROA (%)", row.get("roa")),
            ("Net Profit Margin (%)", row.get("netprofit_margin")),
            ("Gross Profit Margin (%)", row.get("grossprofit_margin")),
            ("Revenue Per Share", row.get("revenue_ps")),
            ("Book Value Per Share", row.get("bps")),
            ("OCF Per Share", row.get("ocfps")),
            ("Debt to Assets (%)", row.get("debt_to_assets")),
            ("Current Ratio", row.get("current_ratio")),
            ("Quick Ratio", row.get("quick_ratio")),
            ("Assets Turnover", row.get("assets_turn")),
            ("EPS YoY (%)", row.get("basic_eps_yoy")),
            ("Net Profit YoY (%)", row.get("netprofit_yoy")),
            ("Revenue YoY (%)", row.get("tr_yoy")),
        ]
        lines = [f"{label}: {value}" for label, value in fields if value is not None]
        header = f"# Financial Indicators for {ts_code}\n"
        header += f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + "\n".join(lines)
    except Exception as e:
        return f"Error getting fundamentals for {ts_code}: {e}"


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get balance sheet data from Tushare."""
    pro = _get_api()
    ts_code = _normalise_ts_code(ticker)

    try:
        df = pro.balancesheet(ts_code=ts_code)
        if df is None or df.empty:
            return f"No balance sheet data for {ts_code}"

        if freq == "annual":
            df = df[df["end_date"].str.endswith("1231")]

        return _df_to_csv_report(df.head(8), "Balance Sheet", ts_code)
    except Exception as e:
        return f"Error getting balance sheet for {ts_code}: {e}"


def get_cashflow(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get cash flow statement from Tushare."""
    pro = _get_api()
    ts_code = _normalise_ts_code(ticker)

    try:
        df = pro.cashflow(ts_code=ts_code)
        if df is None or df.empty:
            return f"No cash flow data for {ts_code}"

        if freq == "annual":
            df = df[df["end_date"].str.endswith("1231")]

        return _df_to_csv_report(df.head(8), "Cash Flow Statement", ts_code)
    except Exception as e:
        return f"Error getting cash flow for {ts_code}: {e}"


def get_income_statement(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get income statement from Tushare."""
    pro = _get_api()
    ts_code = _normalise_ts_code(ticker)

    try:
        df = pro.income(ts_code=ts_code)
        if df is None or df.empty:
            return f"No income statement data for {ts_code}"

        if freq == "annual":
            df = df[df["end_date"].str.endswith("1231")]

        return _df_to_csv_report(df.head(8), "Income Statement", ts_code)
    except Exception as e:
        return f"Error getting income statement for {ts_code}: {e}"


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """Use disclosure and exchange Q&A data as the A-share governance analogue."""
    ts_code = _normalise_ts_code(ticker)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - relativedelta(days=30)).strftime("%Y-%m-%d")
    disclosure = _a_share_disclosure_report(ts_code, start_date, end_date, limit=10)
    if disclosure:
        return (
            f"# Corporate Disclosures and Investor Communications for {ts_code}\n\n"
            "Tushare does not expose US-style insider transaction tables for A-shares. "
            "This section uses the project's native A-share interfaces for announcements "
            "and exchange-hosted investor Q&A instead.\n\n"
            f"{disclosure}"
        )
    return (
        f"# Corporate Disclosures and Investor Communications for {ts_code}\n\n"
        "No recent company announcements or exchange Q&A were returned by the configured Tushare interfaces."
    )


def get_news(
    ticker: Annotated[str, "ticker symbol"],
    start_date: Annotated[str, "start date yyyy-mm-dd"] = None,
    end_date: Annotated[str, "end date yyyy-mm-dd"] = None,
) -> str:
    """Get financial news from Tushare (Chinese news sources)."""
    if not start_date:
        start_date = (datetime.now() - relativedelta(days=3)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    meta = _get_security_meta(ticker)
    ts_code = meta["ts_code"]
    bare_code = meta["code"]
    company_name = meta["name"]
    aliases = _name_aliases(company_name)
    cache_key = f"{ts_code}_{start_date}_{end_date}"
    cached_text = _load_cached_text("company_news", cache_key)
    if cached_text:
        return cached_text

    pro = _get_api()

    disclosure_report = _a_share_disclosure_report(ts_code, start_date, end_date)

    try:
        df = None
        last_exc = None
        for src in ("sina", "eastmoney"):
            try:
                df = pro.news(
                    src=src,
                    start_date=f"{start_date} 00:00:00",
                    end_date=f"{end_date} 23:59:59",
                )
                if df is not None and not df.empty:
                    break
            except Exception as e:
                last_exc = e

        if df is None or df.empty:
            if cached_text:
                return cached_text
            header = f"# News for {ts_code} ({start_date} to {end_date})\n\n"
            if last_exc and "每小时最多访问该接口" in str(last_exc):
                body = "Tushare company news rate limit reached."
            else:
                body = "No company-specific news articles were returned by the configured Tushare news sources."
            if disclosure_report:
                text = header + body + "\n\n# Disclosure Supplement\n\n" + disclosure_report
                _save_cached_text("company_news", cache_key, text)
                return text
            return header + body

        code_mask = (
            df["title"].astype(str).str.contains(bare_code, na=False)
            | df["content"].astype(str).str.contains(bare_code, na=False)
        )
        name_mask = False
        for alias in aliases:
            name_mask = (
                name_mask
                | df["title"].astype(str).str.contains(alias, na=False)
                | df["content"].astype(str).str.contains(alias, na=False)
            )
        relevant = df[code_mask | name_mask]

        if relevant.empty:
            relevant = df.head(20)

        lines = []
        for _, row in relevant.head(15).iterrows():
            dt = row.get("datetime", "")
            title = row.get("title", "")
            content = _format_text_snippet(row.get("content", ""), 200)
            lines.append(f"[{dt}] {title}\n{content}\n")

        name_suffix = f" / {company_name}" if company_name else ""
        header = f"# News for {ts_code}{name_suffix} ({start_date} to {end_date})\n\n"
        text = header + "\n".join(lines)
        if disclosure_report:
            text += "\n\n# Disclosure Supplement\n\n" + disclosure_report
        _save_cached_text("company_news", cache_key, text)
        return text

    except Exception as e:
        cached_text = _load_cached_text("company_news", cache_key)
        if cached_text:
            return cached_text
        if disclosure_report:
            return (
                f"# News for {ts_code} ({start_date} to {end_date})\n\n"
                f"Tushare company news call failed: {e}\n\n"
                f"# Disclosure Supplement\n\n{disclosure_report}"
            )
        return f"Error getting news: {e}"


def get_global_news(
    curr_date: Annotated[str, "current date yyyy-mm-dd"] = None,
    look_back_days: Annotated[int, "days to look back"] = 7,
    limit: Annotated[int, "max articles to return"] = 20,
) -> str:
    """Get general financial news from Tushare."""
    if not curr_date:
        curr_date = datetime.now().strftime("%Y-%m-%d")
    end_date = curr_date
    start_date = (
        datetime.strptime(curr_date, "%Y-%m-%d") - relativedelta(days=look_back_days)
    ).strftime("%Y-%m-%d")

    cache_key = f"{start_date}_{end_date}"
    cached_text = _load_cached_text("global_news", cache_key)
    if cached_text:
        return cached_text

    pro = _get_api()

    try:
        df = None
        last_exc = None
        for src in ("wallstreetcn", "cls"):
            try:
                df = pro.news(
                    src=src,
                    start_date=f"{start_date} 00:00:00",
                    end_date=f"{end_date} 23:59:59",
                )
                if df is not None and not df.empty:
                    break
            except Exception as e:
                last_exc = e

        if df is None or df.empty:
            if cached_text:
                return cached_text
            if last_exc and "每小时最多访问该接口" in str(last_exc):
                return (
                    f"# Global Financial News ({start_date} to {end_date})\n\n"
                    "Tushare global news rate limit reached. No cached macro news available yet."
                )
            return f"No global news found between {start_date} and {end_date}"

        lines = []
        for _, row in df.head(limit).iterrows():
            dt = row.get("datetime", "")
            title = row.get("title", "")
            content = _format_text_snippet(row.get("content", ""), 200)
            lines.append(f"[{dt}] {title}\n{content}\n")

        header = f"# Global Financial News ({start_date} to {end_date})\n\n"
        text = header + "\n".join(lines)
        _save_cached_text("global_news", cache_key, text)
        return text

    except Exception as e:
        cached_text = _load_cached_text("global_news", cache_key)
        if cached_text:
            return cached_text
        return f"Error getting global news: {e}"
