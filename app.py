import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import redis
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS


load_dotenv()

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
if not FINNHUB_API_KEY:
    raise RuntimeError("FINNHUB_API_KEY environment variable not set")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
if not ALPHAVANTAGE_API_KEY:
    raise RuntimeError("ALPHAVANTAGE_API_KEY environment variable not set")

REQUEST_TIMEOUT_SECONDS = 10
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.-]{1,10}$")
VALID_RESOLUTIONS = {"1", "5", "15", "30", "60", "D", "W", "M"}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
    except Exception as error:
        logger.warning("Redis unavailable; continuing without cache: %s", error)
        redis_client = None

app = Flask(__name__, static_folder=None)
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "http://localhost:3000",
                "http://localhost:5173",
                "http://127.0.0.1:3000",
                "http://127.0.0.1:5173",
                "https://personal-website-systems.vercel.app",
            ]
        }
    },
)


class FinnhubError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def validate_symbol(symbol):
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid symbol")
    return normalized


def finnhub_get(endpoint, params):
    if not FINNHUB_API_KEY:
        logger.error("Finnhub API key is not configured")
        raise FinnhubError("Finnhub API key is not configured")

    request_params = {**params, "token": FINNHUB_API_KEY}
    url = f"{FINNHUB_BASE_URL}/{endpoint}"
    started_at = time.perf_counter()
    try:
        response = requests.get(
            url, params=request_params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "Finnhub API call endpoint=%s status=%s duration_ms=%.2f",
            endpoint,
            response.status_code,
            duration_ms,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        duration_ms = (time.perf_counter() - started_at) * 1000
        status_code = getattr(error.response, "status_code", None)
        logger.error(
            "Finnhub API failure endpoint=%s status=%s duration_ms=%.2f error_type=%s",
            endpoint,
            status_code,
            duration_ms,
            type(error).__name__,
        )
        raise FinnhubError("Finnhub request failed", status_code) from error
    except ValueError as error:
        logger.error("Finnhub returned invalid JSON endpoint=%s error=%s", endpoint, error)
        raise FinnhubError("Finnhub returned invalid data") from error


def get_quote_ttl_seconds():
    now = datetime.now(ZoneInfo("America/New_York"))
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return 30 if now.weekday() < 5 and market_open <= now < market_close else 1800


def quote_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_quote_payload(symbol, data=None):
    quote_data = data if isinstance(data, dict) else {}
    return {
        "symbol": symbol,
        "current_price": quote_number(quote_data.get("c")),
        "change": quote_number(quote_data.get("d")),
        "percent_change": quote_number(quote_data.get("dp")),
        "high": quote_number(quote_data.get("h")),
        "low": quote_number(quote_data.get("l")),
        "open": quote_number(quote_data.get("o")),
        "previous_close": quote_number(quote_data.get("pc")),
    }


def get_cached_quote(symbol):
    cache_key = f"quote:{symbol}"
    if redis_client:
        try:
            cached_value = redis_client.get(cache_key)
            if cached_value is not None:
                parsed_value = json.loads(cached_value)
                if isinstance(parsed_value, dict):
                    return parsed_value
        except Exception as error:
            logger.warning("Redis quote read failed symbol=%s error=%s", symbol, error)

    try:
        data = finnhub_get("quote", {"symbol": symbol})
    except FinnhubError as error:
        logger.warning("Quote fallback activated symbol=%s error=%s", symbol, error)
        if redis_client:
            try:
                cached_value = redis_client.get(cache_key)
                if cached_value is not None:
                    parsed_value = json.loads(cached_value)
                    if isinstance(parsed_value, dict):
                        return parsed_value
            except Exception as redis_error:
                logger.warning("Redis quote read failed during fallback symbol=%s error=%s", symbol, redis_error)
        return {}

    if redis_client:
        try:
            redis_client.setex(cache_key, get_quote_ttl_seconds(), json.dumps(data))
        except Exception as error:
            logger.warning("Redis quote write failed symbol=%s error=%s", symbol, error)

    return data


@lru_cache(maxsize=512)
def get_cached_profile(symbol, time_key):
    del time_key
    return finnhub_get("stock/profile2", {"symbol": symbol})


def get_time_key_for_resolution(resolution: str):
    r = resolution.upper()
    now = datetime.utcnow()
    if r == "D":
        return now.strftime("%Y-%m-%d")
    if r == "W":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week}"
    if r == "M":
        return now.strftime("%Y-%m")
    return now.strftime("%Y-%m-%d")


ALPHAVANTAGE_FUNCTIONS = {
    "D": "TIME_SERIES_DAILY",
    "W": "TIME_SERIES_WEEKLY",
    "M": "TIME_SERIES_MONTHLY",
}


@lru_cache(maxsize=256)
def get_cached_candles(symbol, resolution, days, time_key):
    del time_key
    func = ALPHAVANTAGE_FUNCTIONS.get(resolution.upper())
    if not func:
        return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

    url = "https://www.alphavantage.co/query"
    params = {"function": func, "symbol": symbol, "apikey": ALPHAVANTAGE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.warning("AlphaVantage non-200 status=%s for symbol=%s func=%s", resp.status_code, symbol, func)
            return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        data = resp.json()
        logger.info(
            "AlphaVantage debug symbol=%s func=%s status=%s keys=%s",
            symbol,
            func,
            resp.status_code,
            list(data.keys()) if isinstance(data, dict) else type(data),
        )
        if isinstance(data, dict) and "Note" in data:
            logger.warning("AlphaVantage NOTE: %s", data.get("Note"))
        if isinstance(data, dict) and "Error Message" in data:
            logger.warning("AlphaVantage ERROR: %s", data.get("Error Message"))
        if isinstance(data, dict) and "Information" in data:
            logger.warning("AlphaVantage INFORMATION: %s", data.get("Information"))
    except Exception:
        logger.exception("AlphaVantage request failed for symbol=%s func=%s", symbol, func)
        return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

    # Handle rate limit notice or error message
    if (
        not isinstance(data, dict)
        or "Note" in data
        or "Error Message" in data
        or "Information" in data
    ):
        logger.warning("AlphaVantage returned error/note/information for symbol=%s func=%s", symbol, func)
        return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

    # Find the time series key dynamically
    series = None
    for k, v in data.items():
        if "Time Series" in k and isinstance(v, dict):
            series = v
            break

    if not series:
        logger.warning("AlphaVantage missing time series for symbol=%s func=%s", symbol, func)
        return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

    # Sort dates ascending
    try:
        all_dates = sorted(series.keys())
    except Exception:
        logger.exception("Failed sorting AlphaVantage series keys for symbol=%s", symbol)
        return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

    selected = all_dates[-int(days) :] if days > 0 else []
    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for date_str in selected:
        try:
            # date formats are YYYY-MM-DD for daily/weekly/monthly
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            entry = series.get(date_str, {})
            o = float(entry.get("1. open") or entry.get("open") or 0)
            h = float(entry.get("2. high") or entry.get("high") or 0)
            l = float(entry.get("3. low") or entry.get("low") or 0)
            c = float(entry.get("4. close") or entry.get("close") or 0)
            v = int(float(entry.get("5. volume") or entry.get("volume") or 0))
            timestamps.append(int(dt.timestamp()))
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)
        except Exception:
            logger.exception("Failed parsing AlphaVantage entry date=%s symbol=%s", date_str, symbol)
            continue

    return {
        "timestamps": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }


@lru_cache(maxsize=512)
def get_cached_news(symbol, time_key):
    del time_key
    today = datetime.now(timezone.utc).date()
    return finnhub_get(
        "company-news",
        {
            "symbol": symbol,
            "from": (today - timedelta(days=7)).isoformat(),
            "to": today.isoformat(),
        },
    )


def api_error(error):
    logger.error("API request failed error=%s", error)
    return jsonify({"error": "Finnhub service unavailable"}), 500


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_server_error(_error):
    return jsonify({"error": "Internal server error"}), 500


@app.get("/")
def index():
    return jsonify({"service": "stock-dashboard-api", "status": "up"})


@app.get("/health")
def health_check():
    return jsonify({"status": "up"})


@app.get("/api/quote/<symbol>")
def quote(symbol):
    try:
        symbol = validate_symbol(symbol)
        logger.info("Incoming symbol=%s", symbol)
        data = get_cached_quote(symbol)
        return jsonify(build_quote_payload(symbol, data))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (FinnhubError, TypeError) as error:
        logger.warning("Quote request degraded symbol=%s error=%s", symbol, error)
        return jsonify(build_quote_payload(symbol, {}))


@app.get("/api/profile/<symbol>")
def profile(symbol):
    try:
        symbol = validate_symbol(symbol)
        logger.info("Incoming symbol=%s", symbol)
        data = get_cached_profile(symbol, int(time.time() // 10))
        return jsonify(
            {
                "name": data.get("name", ""),
                "logo": data.get("logo", ""),
                "industry": data.get("finnhubIndustry", ""),
                "market_cap": float(data.get("marketCapitalization", 0)),
                "country": data.get("country", ""),
            }
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (FinnhubError, TypeError) as error:
        return api_error(error)


@app.get("/api/candles/<symbol>")
def candles(symbol):
    try:
        symbol = validate_symbol(symbol)
        resolution = (request.args.get("resolution") or "D").upper()
        days = int(request.args.get("days", 30))
        # Only support daily/weekly/monthly via Alpha Vantage
        if resolution not in ALPHAVANTAGE_FUNCTIONS:
            return jsonify({"error": "Invalid resolution"}), 400
        if days < 1 or days > 3650:
            raise ValueError("Invalid candle parameters")
        logger.info(
            "Incoming symbol=%s resolution=%s days=%s",
            symbol,
            resolution,
            days,
        )
        time_key = get_time_key_for_resolution(resolution)
        data = get_cached_candles(symbol, resolution, days, time_key)
        if data.get("s") != "ok":
            return jsonify(
                {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
            )
        return jsonify(
            {
                "timestamps": data.get("t", []),
                "open": data.get("o", []),
                "high": data.get("h", []),
                "low": data.get("l", []),
                "close": data.get("c", []),
                "volume": data.get("v", []),
            }
        )
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    except FinnhubError as error:
        return api_error(error)


@app.get("/api/news/<symbol>")
def news(symbol):
    try:
        symbol = validate_symbol(symbol)
        logger.info("Incoming symbol=%s", symbol)
        articles = get_cached_news(symbol, int(time.time() // 300))
        valid_articles = [
            article
            for article in (articles if isinstance(articles, list) else [])
            if article.get("headline") and article.get("url")
        ]
        valid_articles.sort(key=lambda article: article.get("datetime", 0), reverse=True)
        return jsonify(
            [
                {
                    "headline": article.get("headline", ""),
                    "source": article.get("source", ""),
                    "url": article.get("url", ""),
                    "image": article.get("image", ""),
                    "summary": article.get("summary", ""),
                    "published": datetime.fromtimestamp(
                        article.get("datetime", 0), timezone.utc
                    ).strftime("%Y-%m-%d %H:%M"),
                }
                for article in valid_articles[:5]
            ]
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (FinnhubError, TypeError, OSError) as error:
        return api_error(error)


# ---------------------------------------------------------------------------
# defeatbeta-api — fundamentals data layer
# ---------------------------------------------------------------------------
_DEFEATBETA_AVAILABLE = False
try:
    from defeatbeta_api.data.ticker import Ticker as _DefeatbetaTicker  # noqa: E402
    _DEFEATBETA_AVAILABLE = True
    logger.info("defeatbeta_api loaded successfully")
except Exception as _db_import_err:  # pragma: no cover
    logger.warning("defeatbeta_api unavailable; fundamentals endpoints will return empty: %s", _db_import_err)

_TTL_1DAY = 86400
_TTL_7DAY = 604800

_EMPTY_FUNDAMENTALS = {"symbol": "", "name": "", "sector": "", "industry": "", "market_cap": 0.0, "pe_ratio": None, "eps": None, "revenue": None, "profit_margin": None, "shares_outstanding": None, "country": "", "description": ""}
_EMPTY_INCOME = {"symbol": "", "periods": [], "total_revenue": [], "gross_profit": [], "operating_income": [], "net_income": [], "ebitda": []}
_EMPTY_BALANCE = {"symbol": "", "periods": [], "total_assets": [], "total_liabilities": [], "stockholders_equity": [], "cash_and_equivalents": [], "total_debt": []}
_EMPTY_CASHFLOW = {"symbol": "", "periods": [], "operating_cash_flow": [], "investing_cash_flow": [], "financing_cash_flow": [], "free_cash_flow": [], "capital_expenditure": []}
_EMPTY_VALUATION = {"symbol": "", "pe_ratio": None, "pb_ratio": None, "ps_ratio": None, "ev_ebitda": None, "peg_ratio": None, "enterprise_value": None, "market_cap": None}
_EMPTY_TRANSCRIPTS = {"symbol": "", "transcripts": []}


def _df_col(df, *col_candidates):
    """Return list of floats for the first matching column in *df*, else []."""
    try:
        import pandas as pd  # noqa: PLC0415
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return []
        for col in col_candidates:
            if col in df.columns:
                return [None if pd.isna(v) else round(float(v), 4) for v in df[col]]
        return []
    except Exception:
        return []


def _df_index(df):
    """Return the index of a DataFrame as a list of strings."""
    try:
        import pandas as pd  # noqa: PLC0415
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return []
        return [str(v) for v in df.index.tolist()]
    except Exception:
        return []


def _safe_ticker(symbol):
    """Instantiate defeatbeta Ticker or raise RuntimeError."""
    if not _DEFEATBETA_AVAILABLE:
        raise RuntimeError("defeatbeta_api not installed")
    return _DefeatbetaTicker(symbol)


def _redis_get(key):
    if not redis_client:
        return None
    try:
        raw = redis_client.get(key)
        return json.loads(raw) if raw else None
    except Exception as err:
        logger.warning("Redis get failed key=%s err=%s", key, err)
        return None


def _redis_set(key, value, ttl):
    if not redis_client:
        return
    try:
        redis_client.setex(key, ttl, json.dumps(value))
    except Exception as err:
        logger.warning("Redis set failed key=%s err=%s", key, err)


def _fundamentals_payload(symbol):
    t = _safe_ticker(symbol)
    try:
        import pandas as pd  # noqa: PLC0415
        info_df = t.info()
        row = info_df.iloc[0].to_dict() if isinstance(info_df, pd.DataFrame) and not info_df.empty else {}
    except Exception:
        row = {}
    try:
        pe = t.ttm_pe()
        import pandas as pd  # noqa: PLC0415
        pe_val = float(pe.iloc[0, 0]) if isinstance(pe, pd.DataFrame) and not pe.empty else None
    except Exception:
        pe_val = None
    return {
        "symbol": symbol,
        "name": str(row.get("longName") or row.get("shortName") or ""),
        "sector": str(row.get("sector") or ""),
        "industry": str(row.get("industry") or ""),
        "market_cap": float(row["marketCap"]) if row.get("marketCap") else None,
        "pe_ratio": pe_val,
        "eps": float(row["trailingEps"]) if row.get("trailingEps") else None,
        "revenue": float(row["totalRevenue"]) if row.get("totalRevenue") else None,
        "profit_margin": float(row["profitMargins"]) if row.get("profitMargins") else None,
        "shares_outstanding": float(row["sharesOutstanding"]) if row.get("sharesOutstanding") else None,
        "country": str(row.get("country") or ""),
        "description": str(row.get("longBusinessSummary") or "")[:500],
    }


def _income_payload(symbol):
    t = _safe_ticker(symbol)
    df = t.annual_income_statement()
    import pandas as pd  # noqa: PLC0415
    try:
        raw = df.df if hasattr(df, "df") else (df if isinstance(df, pd.DataFrame) else None)
    except Exception:
        raw = None
    return {
        "symbol": symbol,
        "periods": _df_index(raw),
        "total_revenue": _df_col(raw, "Total Revenue", "totalRevenue", "Revenue"),
        "gross_profit": _df_col(raw, "Gross Profit", "grossProfit"),
        "operating_income": _df_col(raw, "Operating Income", "operatingIncome", "EBIT"),
        "net_income": _df_col(raw, "Net Income", "netIncome"),
        "ebitda": _df_col(raw, "EBITDA", "ebitda"),
    }


def _balance_payload(symbol):
    t = _safe_ticker(symbol)
    df = t.annual_balance_sheet()
    import pandas as pd  # noqa: PLC0415
    try:
        raw = df.df if hasattr(df, "df") else (df if isinstance(df, pd.DataFrame) else None)
    except Exception:
        raw = None
    return {
        "symbol": symbol,
        "periods": _df_index(raw),
        "total_assets": _df_col(raw, "Total Assets", "totalAssets"),
        "total_liabilities": _df_col(raw, "Total Liabilities Net Minority Interest", "totalLiabilities", "Total Liabilities"),
        "stockholders_equity": _df_col(raw, "Stockholders Equity", "stockholdersEquity", "Total Equity Gross Minority Interest"),
        "cash_and_equivalents": _df_col(raw, "Cash And Cash Equivalents", "cashAndCashEquivalents", "Cash"),
        "total_debt": _df_col(raw, "Total Debt", "totalDebt"),
    }


def _cashflow_payload(symbol):
    t = _safe_ticker(symbol)
    df = t.annual_cash_flow()
    import pandas as pd  # noqa: PLC0415
    try:
        raw = df.df if hasattr(df, "df") else (df if isinstance(df, pd.DataFrame) else None)
    except Exception:
        raw = None
    return {
        "symbol": symbol,
        "periods": _df_index(raw),
        "operating_cash_flow": _df_col(raw, "Operating Cash Flow", "operatingCashFlow", "Total Cash From Operating Activities"),
        "investing_cash_flow": _df_col(raw, "Investing Cash Flow", "investingCashFlow", "Total Cashflows From Investing Activities"),
        "financing_cash_flow": _df_col(raw, "Financing Cash Flow", "financingCashFlow", "Total Cash From Financing Activities"),
        "free_cash_flow": _df_col(raw, "Free Cash Flow", "freeCashFlow"),
        "capital_expenditure": _df_col(raw, "Capital Expenditure", "capitalExpenditure", "Capital Expenditures"),
    }


def _valuation_payload(symbol):
    t = _safe_ticker(symbol)
    import pandas as pd  # noqa: PLC0415
    def _safe_float(df, col):
        try:
            if isinstance(df, pd.DataFrame) and not df.empty and col in df.columns:
                v = df[col].iloc[0]
                return None if pd.isna(v) else float(v)
        except Exception:
            pass
        return None

    try:
        pe_df = t.ttm_pe()
        pe = float(pe_df.iloc[0, 0]) if isinstance(pe_df, pd.DataFrame) and not pe_df.empty else None
    except Exception:
        pe = None

    try:
        info_df = t.info()
        row = info_df.iloc[0].to_dict() if isinstance(info_df, pd.DataFrame) and not info_df.empty else {}
    except Exception:
        row = {}

    return {
        "symbol": symbol,
        "pe_ratio": pe,
        "pb_ratio": float(row["priceToBook"]) if row.get("priceToBook") else None,
        "ps_ratio": float(row["priceToSalesTrailing12Months"]) if row.get("priceToSalesTrailing12Months") else None,
        "ev_ebitda": float(row["enterpriseToEbitda"]) if row.get("enterpriseToEbitda") else None,
        "peg_ratio": float(row["pegRatio"]) if row.get("pegRatio") else None,
        "enterprise_value": float(row["enterpriseValue"]) if row.get("enterpriseValue") else None,
        "market_cap": float(row["marketCap"]) if row.get("marketCap") else None,
    }


def _transcripts_payload(symbol):
    t = _safe_ticker(symbol)
    try:
        raw = t.earning_call_transcripts()
        items = []
        transcripts_list = raw.transcripts if hasattr(raw, "transcripts") else (raw if isinstance(raw, list) else [])
        for item in transcripts_list[:5]:
            items.append({
                "date": str(getattr(item, "date", "") or ""),
                "quarter": str(getattr(item, "quarter", "") or ""),
                "year": str(getattr(item, "year", "") or ""),
                "title": str(getattr(item, "title", "") or ""),
                "summary": str(getattr(item, "content", "") or getattr(item, "summary", "") or "")[:1000],
            })
        return {"symbol": symbol, "transcripts": items}
    except Exception as err:
        logger.warning("defeatbeta transcripts failed symbol=%s err=%s", symbol, err)
        return dict(_EMPTY_TRANSCRIPTS, symbol=symbol)


def _fundamentals_endpoint(symbol, cache_key, builder_fn, empty_template, ttl):
    """Generic handler: Redis → defeatbeta → fallback."""
    try:
        symbol = validate_symbol(symbol)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    cached = _redis_get(cache_key.format(symbol=symbol))
    if cached:
        return jsonify(cached)

    try:
        payload = builder_fn(symbol)
        _redis_set(cache_key.format(symbol=symbol), payload, ttl)
        return jsonify(payload)
    except Exception as err:
        logger.warning("defeatbeta %s failed symbol=%s err=%s", cache_key, symbol, err)
        stale = _redis_get(cache_key.format(symbol=symbol))
        if stale:
            return jsonify(stale)
        return jsonify(dict(empty_template, symbol=symbol))


@app.get("/api/fundamentals/<symbol>")
def fundamentals(symbol):
    return _fundamentals_endpoint(symbol, "fundamentals:{symbol}", _fundamentals_payload, _EMPTY_FUNDAMENTALS, _TTL_1DAY)


@app.get("/api/income/<symbol>")
def income(symbol):
    return _fundamentals_endpoint(symbol, "income:{symbol}", _income_payload, _EMPTY_INCOME, _TTL_1DAY)


@app.get("/api/balance/<symbol>")
def balance(symbol):
    return _fundamentals_endpoint(symbol, "balance:{symbol}", _balance_payload, _EMPTY_BALANCE, _TTL_1DAY)


@app.get("/api/cashflow/<symbol>")
def cashflow(symbol):
    return _fundamentals_endpoint(symbol, "cashflow:{symbol}", _cashflow_payload, _EMPTY_CASHFLOW, _TTL_1DAY)


@app.get("/api/valuation/<symbol>")
def valuation(symbol):
    return _fundamentals_endpoint(symbol, "valuation:{symbol}", _valuation_payload, _EMPTY_VALUATION, _TTL_1DAY)


@app.get("/api/transcripts/<symbol>")
def transcripts(symbol):
    return _fundamentals_endpoint(symbol, "transcripts:{symbol}", _transcripts_payload, _EMPTY_TRANSCRIPTS, _TTL_7DAY)
