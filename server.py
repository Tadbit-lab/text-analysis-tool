import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import redis
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration — API keys from environment
# ---------------------------------------------------------------------------
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
ALPHAVANTAGE_BASE_URL = "https://www.alphavantage.co/query"

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

if not FINNHUB_API_KEY:
    logger.warning("FINNHUB_API_KEY not set — using yfinance fallback for news")
if not TWELVEDATA_API_KEY:
    logger.warning("TWELVEDATA_API_KEY not set — using fallbacks for quotes/candles")
if not ALPHAVANTAGE_API_KEY:
    logger.warning("ALPHAVANTAGE_API_KEY not set")

REQUEST_TIMEOUT_SECONDS = 10
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9./:-]{1,15}$")
VALID_RESOLUTIONS = {"1", "5", "15", "30", "60", "D", "W", "M"}
MAX_CACHE_ENTRIES = 256
WATCHLIST_TTL_SECONDS = 300

FOREX_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "HKD", "SGD"}

# ---------------------------------------------------------------------------
# Word cloud configuration
# ---------------------------------------------------------------------------
WORDCLOUD_REFRESH_HOUR_ET = 6
WORDCLOUD_MAX_WORDS = 50
WORDCLOUD_MIN_WORD_LENGTH = 2

DEFAULT_WARMUP_TICKERS = ["GOOGL", "GOOG"]

STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its", "let",
    "put", "say", "she", "too", "use", "that", "with", "have", "this", "will",
    "your", "from", "they", "know", "want", "been", "good", "much", "some",
    "time", "very", "when", "come", "here", "just", "like", "long", "make",
    "many", "over", "such", "take", "than", "them", "well", "were", "what",
    "would", "there", "their", "which", "about", "could", "other", "these",
    "into", "more", "only", "also", "then", "said", "each", "should", "after",
    "first", "never", "think", "where", "being", "every", "great", "might",
    "still", "under", "while", "before", "because", "against", "between",
    "during", "through", "without", "within", "around", "again", "another",
    "reuters", "bloomberg", "cnbc", "reports", "report", "reported", "reporting",
    "says", "saying", "according", "billion", "million", "trillion",
    "percent", "quarter", "quarterly", "fiscal", "year", "years", "yesterday",
    "today", "tomorrow", "week", "month", "monday", "tuesday", "wednesday",
    "thursday", "friday", "company", "companies", "inc", "corp", "corporation",
    "ltd", "llc", "ceo", "cfo", "cto", "stock", "stocks", "share", "shares",
    "market", "markets", "trading", "trade", "trader", "traders", "price",
    "prices", "investor", "investors", "analyst", "analysts", "wall", "street",
    "news", "update", "updates", "story", "stories", "article", "articles",
    "read", "click", "photo", "image", "video", "watch", "listen",
    "amid", "amidst", "however", "although", "though", "thus", "hence",
    "therefore", "moreover", "furthermore", "meanwhile", "including",
})

# ---------------------------------------------------------------------------
# Redis client
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL")
_redis_client = None
if REDIS_URL:
    try:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        _redis_client.ping()
        logger.info("Redis connected")
    except Exception as _redis_err:
        logger.warning("Redis unavailable; continuing without cache: %s", _redis_err)
        _redis_client = None

# ---------------------------------------------------------------------------
# In-memory caches (OrderedDict for LRU eviction)
# ---------------------------------------------------------------------------
QUOTE_CACHE = OrderedDict()
PROFILE_CACHE = OrderedDict()
CANDLE_CACHE = OrderedDict()
WATCHLIST_CACHE = OrderedDict()
NEWS_CACHE = OrderedDict()
TECHNICALS_CACHE = OrderedDict()
WORDCLOUD_CACHE = OrderedDict()
CACHE_LOCK = Lock()
IN_FLIGHT = {}

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "http://localhost:3000",
                "http://localhost:5173",
                "http://localhost:5174",
                "http://127.0.0.1:3000",
                "http://127.0.0.1:5173",
                "http://127.0.0.1:5174",
                "https://personal-website-systems.vercel.app",
            ]
        }
    },
)


class FinnhubError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def validate_symbol(symbol):
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid symbol")
    return normalized


def is_forex_pair(symbol):
    s = symbol.replace("/", "")
    return len(s) == 6 and s[:3] in FOREX_CURRENCIES and s[3:] in FOREX_CURRENCIES


def format_forex_symbol(symbol):
    s = symbol.replace("/", "")
    return f"{s[:3]}/{s[3:]}"


def is_market_open():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def _prune_cache(cache):
    while len(cache) > MAX_CACHE_ENTRIES:
        cache.popitem(last=False)


def _touch_cache(cache, key):
    try:
        cache.move_to_end(key)
    except KeyError:
        pass


def _sf(val):
    try:
        f = float(val)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _get_empty_quote_payload():
    return {"c": 0.0, "d": 0.0, "dp": 0.0, "h": 0.0, "l": 0.0, "o": 0.0, "pc": 0.0}


def _get_empty_profile_payload():
    return {"name": "", "logo": "", "finnhubIndustry": "", "marketCapitalization": 0.0, "country": ""}


def _get_empty_candle_payload():
    return {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}


def _quote_ttl_seconds():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return 7200
    if is_market_open():
        return 30
    return 1800


def _candle_timeframe(days, resolution):
    normalized = (resolution or "D").upper()
    if normalized in {"M", "1M"} or days > 1825:
        return "MAX"
    if normalized in {"W", "1W"} or days > 365:
        return "5Y"
    if days > 183:
        return "1Y"
    if days > 31:
        return "6M"
    if days > 7:
        return "1M"
    if days > 1:
        return "1W"
    return "1D"


def _candle_ttl_seconds(days, resolution):
    timeframe = _candle_timeframe(days, resolution)
    if not is_market_open():
        return 7200
    return {
        "1D": 300, "1W": 1800, "1M": 3600, "6M": 7200,
        "1Y": 14400, "5Y": 86400, "MAX": 86400,
    }[timeframe]


def _rget(key):
    if not _redis_client:
        return None
    try:
        raw = _redis_client.get(key)
        return json.loads(raw) if raw else None
    except Exception as err:
        logger.warning("Redis get failed key=%s err=%s", key, err)
        return None


def _rset(key, value, ttl):
    if not _redis_client:
        return
    try:
        _redis_client.setex(key, ttl, json.dumps(value))
    except Exception as err:
        logger.warning("Redis set failed key=%s err=%s", key, err)


class InFlightGuard:
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        with CACHE_LOCK:
            IN_FLIGHT[self.key] = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        with CACHE_LOCK:
            IN_FLIGHT.pop(self.key, None)
        return False


# ---------------------------------------------------------------------------
# External API callers
# ---------------------------------------------------------------------------
def finnhub_get(endpoint, params):
    if not FINNHUB_API_KEY:
        raise FinnhubError("Finnhub API key is not configured")

    request_params = {**params, "token": FINNHUB_API_KEY}
    url = f"{FINNHUB_BASE_URL}/{endpoint}"
    started_at = time.perf_counter()
    try:
        response = requests.get(url, params=request_params, timeout=REQUEST_TIMEOUT_SECONDS)
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info("Finnhub API call endpoint=%s status=%s duration_ms=%.2f", endpoint, response.status_code, duration_ms)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        duration_ms = (time.perf_counter() - started_at) * 1000
        status_code = getattr(error.response, "status_code", None)
        logger.error("Finnhub API failure endpoint=%s status=%s duration_ms=%.2f error_type=%s", endpoint, status_code, duration_ms, type(error).__name__)
        raise FinnhubError("Finnhub request failed", status_code) from error
    except ValueError as error:
        logger.error("Finnhub returned invalid JSON endpoint=%s error=%s", endpoint, error)
        raise FinnhubError("Finnhub returned invalid data") from error


def twelvedata_get(endpoint, params=None):
    if not TWELVEDATA_API_KEY:
        return None

    request_params = {**(params or {}), "apikey": TWELVEDATA_API_KEY}
    url = f"{TWELVEDATA_BASE_URL}/{endpoint}"
    started_at = time.perf_counter()
    try:
        response = requests.get(url, params=request_params, timeout=REQUEST_TIMEOUT_SECONDS)
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info("TwelveData API call endpoint=%s status=%s duration_ms=%.2f", endpoint, response.status_code, duration_ms)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "error":
            logger.warning("TwelveData returned error endpoint=%s message=%s", endpoint, data.get("message"))
            return None
        return data
    except Exception as error:
        logger.warning("TwelveData request failed endpoint=%s error=%s", endpoint, error)
        return None


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def fetch_twelvedata_quote(symbol):
    query_symbol = format_forex_symbol(symbol) if is_forex_pair(symbol) else symbol
    data = twelvedata_get("quote", {"symbol": query_symbol})
    if not data or not isinstance(data, dict) or "close" not in data:
        return None

    ftw = data.get("fifty_two_week", {}) or {}
    return {
        "c": _sf(data.get("close")) or 0.0,
        "d": _sf(data.get("change")) or 0.0,
        "dp": _sf(data.get("percent_change")) or 0.0,
        "h": _sf(data.get("high")) or 0.0,
        "l": _sf(data.get("low")) or 0.0,
        "o": _sf(data.get("open")) or 0.0,
        "pc": _sf(data.get("previous_close")) or 0.0,
        "volume": int(_sf(data.get("volume")) or 0),
        "average_volume": int(_sf(data.get("average_volume")) or 0),
        "fifty_two_week": {
            "low": _sf(ftw.get("low")) or 0.0,
            "high": _sf(ftw.get("high")) or 0.0,
            "range": ftw.get("range", ""),
        },
    }


def fetch_yfinance_quote(symbol):
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.fast_info
        last_price = _sf(info.last_price)
        prev_close = _sf(info.previous_close)
        if not last_price:
            return None
        change = round(last_price - prev_close, 2) if prev_close else 0.0
        pct_change = round((change / prev_close) * 100, 2) if prev_close else 0.0
        return {
            "c": last_price,
            "d": change,
            "dp": pct_change,
            "h": _sf(info.day_high) or last_price,
            "l": _sf(info.day_low) or last_price,
            "o": _sf(info.open) or last_price,
            "pc": prev_close or last_price,
            "volume": int(_sf(info.last_volume) or 0),
            "average_volume": int(_sf(info.three_month_average_volume) or 0),
            "fifty_two_week": {
                "low": _sf(info.year_low) or 0.0,
                "high": _sf(info.year_high) or 0.0,
                "range": f"{_sf(info.year_low)} - {_sf(info.year_high)}",
            },
        }
    except Exception as e:
        logger.warning("yfinance quote fallback failed symbol=%s error=%s", symbol, e)
        return None


def get_cached_quote(symbol):
    key = symbol.upper()
    now = time.time()

    with CACHE_LOCK:
        entry = QUOTE_CACHE.get(key)
        if entry and now - entry["fetched_at"] < _quote_ttl_seconds():
            _touch_cache(QUOTE_CACHE, key)
            return entry["data"]
        if key in IN_FLIGHT:
            return entry["data"] if entry else _get_empty_quote_payload()

    with InFlightGuard(key):
        td_data = fetch_twelvedata_quote(symbol)
        if td_data:
            with CACHE_LOCK:
                QUOTE_CACHE[key] = {"fetched_at": now, "data": td_data}
                _prune_cache(QUOTE_CACHE)
            return td_data

        try:
            data = finnhub_get("quote", {"symbol": symbol})
            if data and data.get("c"):
                with CACHE_LOCK:
                    QUOTE_CACHE[key] = {"fetched_at": now, "data": data}
                    _prune_cache(QUOTE_CACHE)
                return data
        except FinnhubError:
            pass

        yf_data = fetch_yfinance_quote(symbol)
        if yf_data:
            with CACHE_LOCK:
                QUOTE_CACHE[key] = {"fetched_at": now, "data": yf_data}
                _prune_cache(QUOTE_CACHE)
            return yf_data

        fallback = _get_empty_quote_payload()
        with CACHE_LOCK:
            QUOTE_CACHE[key] = {"fetched_at": now, "data": fallback}
            _prune_cache(QUOTE_CACHE)
        return fallback


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def get_cached_profile(symbol):
    key = symbol.upper()
    now = time.time()

    with CACHE_LOCK:
        entry = PROFILE_CACHE.get(key)
        if entry and now - entry["fetched_at"] < 3600:
            _touch_cache(PROFILE_CACHE, key)
            return entry["data"]
        if key in IN_FLIGHT:
            return entry["data"] if entry else _get_empty_profile_payload()

    with InFlightGuard(key):
        try:
            data = finnhub_get("stock/profile2", {"symbol": symbol})
            if data and data.get("name"):
                payload = {
                    "name": data.get("name", ""),
                    "logo": data.get("logo", ""),
                    "finnhubIndustry": data.get("finnhubIndustry", ""),
                    "marketCapitalization": _sf(data.get("marketCapitalization")) or 0.0,
                    "country": data.get("country", ""),
                }
                with CACHE_LOCK:
                    PROFILE_CACHE[key] = {"fetched_at": now, "data": payload}
                    _prune_cache(PROFILE_CACHE)
                return payload
        except FinnhubError:
            pass

        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            info = t.info or {}
            payload = {
                "name": info.get("longName") or info.get("shortName") or symbol,
                "logo": "",
                "finnhubIndustry": info.get("industry") or info.get("sector") or "",
                "marketCapitalization": (_sf(info.get("marketCap")) or 0.0) / 1e6,
                "country": info.get("country") or "",
            }
            with CACHE_LOCK:
                PROFILE_CACHE[key] = {"fetched_at": now, "data": payload}
                _prune_cache(PROFILE_CACHE)
            return payload
        except Exception:
            fallback = _get_empty_profile_payload()
            with CACHE_LOCK:
                PROFILE_CACHE[key] = {"fetched_at": now, "data": fallback}
                _prune_cache(PROFILE_CACHE)
            return fallback


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------
ALPHAVANTAGE_FUNCTIONS = {
    "D": "TIME_SERIES_DAILY", "W": "TIME_SERIES_WEEKLY", "M": "TIME_SERIES_MONTHLY",
}
TWELVEDATA_INTERVALS = {
    "1": "1min", "5": "5min", "15": "15min", "30": "30min", "60": "1h",
    "D": "1day", "1D": "1day", "W": "1week", "1W": "1week", "M": "1month", "1M": "1month",
}


def fetch_alpha_vantage_candles(symbol, resolution, days):
    if not ALPHAVANTAGE_API_KEY:
        return None

    func = ALPHAVANTAGE_FUNCTIONS.get(resolution.upper())
    if not func:
        return None

    params = {"function": func, "symbol": symbol, "apikey": ALPHAVANTAGE_API_KEY}
    try:
        resp = requests.get(ALPHAVANTAGE_BASE_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, dict) or "Note" in data or "Error Message" in data or "Information" in data:
        return None

    series = None
    for k, v in data.items():
        if "Time Series" in k and isinstance(v, dict):
            series = v
            break

    if not series:
        return None

    try:
        all_dates = sorted(series.keys())
    except Exception:
        return None

    selected = all_dates[-int(days):] if days > 0 else []
    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for date_str in selected:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            entry = series.get(date_str, {})
            timestamps.append(int(dt.timestamp()))
            opens.append(_sf(entry.get("1. open") or entry.get("open")) or 0.0)
            highs.append(_sf(entry.get("2. high") or entry.get("high")) or 0.0)
            lows.append(_sf(entry.get("3. low") or entry.get("low")) or 0.0)
            closes.append(_sf(entry.get("4. close") or entry.get("close")) or 0.0)
            volumes.append(int(_sf(entry.get("5. volume") or entry.get("volume")) or 0))
        except Exception:
            continue

    return {
        "timestamps": timestamps,
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
    }


def fetch_twelvedata_candles(symbol, resolution, days):
    interval = TWELVEDATA_INTERVALS.get((resolution or "D").upper(), "1day")
    size = min(int(days), 5000) if days > 0 else 30
    outputsize = max(size, 30)

    query_symbol = format_forex_symbol(symbol) if is_forex_pair(symbol) else symbol
    data = twelvedata_get("time_series", {"symbol": query_symbol, "interval": interval, "outputsize": outputsize})

    if not data or not isinstance(data, dict) or not data.get("values"):
        return None

    raw_vals = data["values"][::-1]
    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for entry in raw_vals[-size:]:
        try:
            dt_str = entry["datetime"]
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S" if " " in dt_str else "%Y-%m-%d").replace(tzinfo=timezone.utc)
            timestamps.append(int(dt.timestamp()))
            opens.append(_sf(entry.get("open")) or 0.0)
            highs.append(_sf(entry.get("high")) or 0.0)
            lows.append(_sf(entry.get("low")) or 0.0)
            closes.append(_sf(entry.get("close")) or 0.0)
            volumes.append(int(_sf(entry.get("volume")) or 0))
        except Exception:
            continue

    return {
        "timestamps": timestamps,
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
    }


def fetch_yfinance_candles(symbol, resolution, days):
    try:
        import yfinance as yf
        interval_map = {
            "1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h",
            "D": "1d", "1D": "1d", "W": "1wk", "1W": "1wk", "M": "1mo", "1M": "1mo",
        }
        yf_interval = interval_map.get((resolution or "D").upper(), "1d")
        d = max(int(days), 1)

        if yf_interval in ["1m", "5m", "15m", "30m"]:
            period = f"{min(d, 7)}d"
        elif d <= 30:
            period = "1mo"
        elif d <= 90:
            period = "3mo"
        elif d <= 180:
            period = "6mo"
        elif d <= 365:
            period = "1y"
        elif d <= 1825:
            period = "5y"
        else:
            period = "max"

        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=yf_interval)
        if df is None or df.empty:
            return None

        timestamps = [int(ts.timestamp()) for ts in df.index]
        return {
            "timestamps": timestamps,
            "open": [round(float(x), 2) for x in df["Open"]],
            "high": [round(float(x), 2) for x in df["High"]],
            "low": [round(float(x), 2) for x in df["Low"]],
            "close": [round(float(x), 2) for x in df["Close"]],
            "volume": [int(x) for x in df["Volume"]],
        }
    except Exception as e:
        logger.warning("yfinance candle fallback failed symbol=%s error=%s", symbol, e)
        return None


def get_cached_candles(symbol, resolution, days):
    cache_key = (symbol.upper(), resolution.upper(), int(days))
    now = time.time()
    ttl_seconds = _candle_ttl_seconds(days, resolution)

    with CACHE_LOCK:
        entry = CANDLE_CACHE.get(cache_key)
        if entry and entry["data"].get("timestamps") and now - entry["fetched_at"] < ttl_seconds:
            _touch_cache(CANDLE_CACHE, cache_key)
            return entry["data"]
        if cache_key in IN_FLIGHT:
            return entry["data"] if entry and entry["data"].get("timestamps") else _get_empty_candle_payload()

    with InFlightGuard(cache_key):
        td_data = fetch_twelvedata_candles(symbol, resolution, days)
        if td_data and td_data.get("timestamps"):
            with CACHE_LOCK:
                CANDLE_CACHE[cache_key] = {"fetched_at": now, "data": td_data}
                _prune_cache(CANDLE_CACHE)
            return td_data

        av_data = fetch_alpha_vantage_candles(symbol, resolution, days)
        if av_data and av_data.get("timestamps"):
            with CACHE_LOCK:
                CANDLE_CACHE[cache_key] = {"fetched_at": now, "data": av_data}
                _prune_cache(CANDLE_CACHE)
            return av_data

        yf_data = fetch_yfinance_candles(symbol, resolution, days)
        if yf_data and yf_data.get("timestamps"):
            with CACHE_LOCK:
                CANDLE_CACHE[cache_key] = {"fetched_at": now, "data": yf_data}
                _prune_cache(CANDLE_CACHE)
            return yf_data

        fallback = _get_empty_candle_payload()
        with CACHE_LOCK:
            CANDLE_CACHE[cache_key] = {"fetched_at": now - 3595, "data": fallback}
            _prune_cache(CANDLE_CACHE)
        return fallback


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------
def get_cached_watchlist(symbols):
    normalized_symbols = sorted({validate_symbol(s) for s in symbols})
    cache_key = tuple(normalized_symbols)
    now = time.time()

    with CACHE_LOCK:
        entry = WATCHLIST_CACHE.get(cache_key)
        if entry and now - entry["fetched_at"] < WATCHLIST_TTL_SECONDS:
            _touch_cache(WATCHLIST_CACHE, cache_key)
            return entry["data"]
        if cache_key in IN_FLIGHT:
            return entry["data"] if entry else []

    with InFlightGuard(cache_key):
        batch_str = ",".join(normalized_symbols)
        td_batch = twelvedata_get("quote", {"symbol": batch_str})

        payload = []
        if td_batch and isinstance(td_batch, dict):
            for sym in normalized_symbols:
                item = td_batch.get(sym) if isinstance(td_batch.get(sym), dict) else (td_batch if td_batch.get("symbol") == sym else None)
                if item and "close" in item:
                    payload.append({
                        "symbol": sym,
                        "current_price": _sf(item.get("close")) or 0.0,
                        "change": _sf(item.get("change")) or 0.0,
                        "percent_change": _sf(item.get("percent_change")) or 0.0,
                        "high": _sf(item.get("high")) or 0.0,
                        "low": _sf(item.get("low")) or 0.0,
                        "open": _sf(item.get("open")) or 0.0,
                        "previous_close": _sf(item.get("previous_close")) or 0.0,
                    })
                else:
                    q = get_cached_quote(sym)
                    payload.append({
                        "symbol": sym,
                        "current_price": _sf(q.get("c")) or 0.0,
                        "change": _sf(q.get("d")) or 0.0,
                        "percent_change": _sf(q.get("dp")) or 0.0,
                        "high": _sf(q.get("h")) or 0.0,
                        "low": _sf(q.get("l")) or 0.0,
                        "open": _sf(q.get("o")) or 0.0,
                        "previous_close": _sf(q.get("pc")) or 0.0,
                    })
        else:
            for symbol in normalized_symbols:
                quote = get_cached_quote(symbol)
                payload.append({
                    "symbol": symbol,
                    "current_price": _sf(quote.get("c")) or 0.0,
                    "change": _sf(quote.get("d")) or 0.0,
                    "percent_change": _sf(quote.get("dp")) or 0.0,
                    "high": _sf(quote.get("h")) or 0.0,
                    "low": _sf(quote.get("l")) or 0.0,
                    "open": _sf(quote.get("o")) or 0.0,
                    "previous_close": _sf(quote.get("pc")) or 0.0,
                })

        with CACHE_LOCK:
            WATCHLIST_CACHE[cache_key] = {"fetched_at": now, "data": payload}
            _prune_cache(WATCHLIST_CACHE)
        return payload


# ---------------------------------------------------------------------------
# News — hardened for Finnhub 503 + new yfinance shape
# ---------------------------------------------------------------------------
def _is_bad_news_url(url):
    if not url or not isinstance(url, str):
        return True
    u = url.strip().lower()
    if not u.startswith("http"):
        return True
    # Finnhub internal API pages 504 in browsers
    if "finnhub.io/api/news" in u:
        return True
    if "finnhub.io/api/" in u:
        return True
    return False


def _google_news_url(headline, symbol=""):
    q = f"{symbol} {headline}".strip()
    return f"https://news.google.com/search?q={quote_plus(q)}"


def _extract_yfinance_url(item, content):
    url = item.get("link") or item.get("url") or ""
    if url:
        return url
    if not content:
        return ""
    for key in ("clickThroughUrl", "canonicalUrl"):
        v = content.get(key)
        if isinstance(v, dict) and v.get("url"):
            return v.get("url")
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def _normalize_news_item(a, symbol=""):
    if not isinstance(a, dict):
        return None

    headline = (a.get("headline") or a.get("title") or "").strip()
    if not headline:
        return None

    url = (a.get("url") or a.get("link") or "").strip()
    if _is_bad_news_url(url):
        url = _google_news_url(headline, symbol)

    ts = a.get("datetime") or a.get("providerPublishTime") or time.time()
    try:
        ts = int(ts)
    except Exception:
        ts = int(time.time())

    return {
        "headline": headline,
        "summary": (a.get("summary") or a.get("description") or "")[:500],
        "source": a.get("source") or a.get("publisher") or "News",
        "url": url,
        "datetime": ts,
        "image": a.get("image") or "",
    }


def fetch_yfinance_news(symbol):
    """Supports legacy yfinance news AND new nested `content` shape."""
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        yf_news = t.news or []
        articles = []

        for item in yf_news:
            if not isinstance(item, dict):
                continue

            content = item.get("content") if isinstance(item.get("content"), dict) else {}

            title = (
                item.get("title")
                or content.get("title")
                or content.get("headline")
                or ""
            ).strip()
            if not title:
                continue

            summary = (
                item.get("summary")
                or item.get("description")
                or content.get("summary")
                or content.get("description")
                or ""
            )

            publisher = item.get("publisher") or "Yahoo Finance"
            provider = content.get("provider")
            if isinstance(provider, dict):
                publisher = provider.get("displayName") or publisher

            url = _extract_yfinance_url(item, content)
            if _is_bad_news_url(url):
                url = _google_news_url(title, symbol)

            ts = item.get("providerPublishTime") or content.get("pubDate") or time.time()
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = int(time.time())
            try:
                ts = int(ts)
            except Exception:
                ts = int(time.time())

            image = ""
            thumb = item.get("thumbnail") or content.get("thumbnail")
            if isinstance(thumb, dict):
                resolutions = thumb.get("resolutions") or []
                if resolutions and isinstance(resolutions[0], dict):
                    image = resolutions[0].get("url") or ""
                image = image or thumb.get("originalUrl") or thumb.get("url") or ""

            articles.append({
                "headline": title,
                "summary": str(summary)[:500],
                "source": publisher,
                "url": url,
                "datetime": ts,
                "image": image,
            })

        return articles
    except Exception as e:
        logger.warning("yfinance news fallback failed symbol=%s error=%s", symbol, e)
        return []


def get_cached_news(symbol):
    key = symbol.upper()
    now = time.time()

    with CACHE_LOCK:
        entry = NEWS_CACHE.get(key)
        # Non-empty cache: 5 min
        if entry and entry.get("data") and now - entry["fetched_at"] < 300:
            _touch_cache(NEWS_CACHE, key)
            return entry["data"]
        # Empty cache: only 45s so Finnhub 503 can recover
        if entry and not entry.get("data") and now - entry["fetched_at"] < 45:
            return []
        if key in IN_FLIGHT:
            return entry["data"] if entry else []

    with InFlightGuard(key):
        today = datetime.now(timezone.utc).date()
        finnhub_articles = []
        yf_articles = []

        try:
            data = finnhub_get(
                "company-news",
                {
                    "symbol": symbol,
                    "from": (today - timedelta(days=30)).isoformat(),
                    "to": today.isoformat(),
                },
            )
            if isinstance(data, list) and data:
                finnhub_articles = data
        except FinnhubError as err:
            logger.warning("Finnhub news unavailable symbol=%s err=%s", symbol, err)

        if len(finnhub_articles) < 3:
            yf_articles = fetch_yfinance_news(symbol)

        merged = []
        seen = set()

        # Prefer yfinance (real publisher URLs), then Finnhub
        for raw in (yf_articles + finnhub_articles):
            item = _normalize_news_item(raw, symbol)
            if not item:
                continue
            h = item["headline"].lower()
            if h in seen:
                continue
            seen.add(h)
            merged.append(item)

        merged.sort(key=lambda a: a.get("datetime", 0), reverse=True)

        logger.info(
            "news symbol=%s finnhub=%d yfinance=%d final=%d",
            symbol,
            len(finnhub_articles),
            len(yf_articles),
            len(merged),
        )

        with CACHE_LOCK:
            NEWS_CACHE[key] = {"fetched_at": now, "data": merged}
            _prune_cache(NEWS_CACHE)

        return merged


# ---------------------------------------------------------------------------
# Word cloud
# ---------------------------------------------------------------------------
def _next_wordcloud_refresh_epoch():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    refresh_today = now_et.replace(hour=WORDCLOUD_REFRESH_HOUR_ET, minute=0, second=0, microsecond=0)
    next_refresh = refresh_today if now_et < refresh_today else refresh_today + timedelta(days=1)
    return next_refresh.timestamp()


def _wordcloud_ttl_seconds():
    return max(int(_next_wordcloud_refresh_epoch() - time.time()), 60)


def _tokenize_text(text):
    if not text:
        return []
    cleaned = re.sub(r"http\S+|www\.\S+", " ", text)
    cleaned = re.sub(r"&[a-z]+;", " ", cleaned)
    cleaned = re.sub(r"[^a-zA-Z\s'-]", " ", cleaned)
    return [w.lower().strip("'-") for w in cleaned.split() if w.strip("'-")]


def _build_wordcloud_from_articles(articles, ticker):
    ticker_lower = ticker.lower()
    counter = Counter()

    for article in articles or []:
        headline = article.get("headline", "") or ""
        summary = article.get("summary", "") or ""
        combined = f"{headline} {summary}"

        for token in _tokenize_text(combined):
            if len(token) < WORDCLOUD_MIN_WORD_LENGTH:
                continue
            if token in STOPWORDS:
                continue
            if token == ticker_lower:
                continue
            if token.isdigit():
                continue
            counter[token] += 1

    top = counter.most_common(WORDCLOUD_MAX_WORDS)
    max_count = top[0][1] if top else 1

    return [
        {
            "text": word,
            "value": count,
            "weight": round(count / max_count, 3),
        }
        for word, count in top
    ]


def get_cached_wordcloud(symbol):
    key = symbol.upper()
    redis_key = f"v2:wc:news:{key}"
    now = time.time()
    next_refresh = _next_wordcloud_refresh_epoch()

    cached = _rget(redis_key)
    if cached and isinstance(cached.get("words"), list) and len(cached["words"]) > 0:
        if cached.get("expires_at", 0) > now:
            with CACHE_LOCK:
                WORDCLOUD_CACHE[key] = {"fetched_at": now, "data": cached}
                _prune_cache(WORDCLOUD_CACHE)
            return cached

    with CACHE_LOCK:
        entry = WORDCLOUD_CACHE.get(key)
        if entry and isinstance(entry["data"].get("words"), list) and len(entry["data"]["words"]) > 0:
            if entry["data"].get("expires_at", 0) > now:
                _touch_cache(WORDCLOUD_CACHE, key)
                return entry["data"]

    with InFlightGuard(key):
        articles = get_cached_news(symbol)
        words = _build_wordcloud_from_articles(articles, symbol)

        payload = {
            "symbol": symbol,
            "words": words,
            "article_count": len(articles) if isinstance(articles, list) else 0,
            "generated_at": int(now),
            "expires_at": int(next_refresh),
            "next_refresh_iso": datetime.fromtimestamp(next_refresh, ZoneInfo("America/New_York")).isoformat(),
        }

        ttl = _wordcloud_ttl_seconds() if len(words) > 0 else 30
        _rset(redis_key, payload, ttl)

        with CACHE_LOCK:
            WORDCLOUD_CACHE[key] = {"fetched_at": now, "data": payload}
            _prune_cache(WORDCLOUD_CACHE)

        return payload


# ---------------------------------------------------------------------------
# Startup warm-up
# ---------------------------------------------------------------------------
def _warmup_cache():
    time.sleep(3)
    logger.info("Starting word cloud warm-up for tickers: %s", DEFAULT_WARMUP_TICKERS)
    for sym in DEFAULT_WARMUP_TICKERS:
        try:
            payload = get_cached_wordcloud(sym)
            logger.info(
                "Warm-up complete: %s (articles=%d, words=%d)",
                sym, payload.get("article_count", 0), len(payload.get("words", [])),
            )
        except Exception as e:
            logger.warning("Warm-up failed for %s: %s", sym, e)
        time.sleep(1)


threading.Thread(target=_warmup_cache, daemon=True).start()


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_server_error(_error):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return jsonify({"service": "stock-dashboard-api", "status": "up"})


@app.get("/health")
def health_check():
    return jsonify({
        "status": "up",
        "redis": bool(_redis_client),
        "market_open": is_market_open(),
        "warmup_tickers": DEFAULT_WARMUP_TICKERS,
    })


@app.get("/api/quote/<symbol>")
def quote(symbol):
    try:
        symbol = validate_symbol(symbol)
        data = get_cached_quote(symbol)
        return jsonify({
            "symbol": symbol,
            "current_price": _sf(data.get("c")) or 0.0,
            "change": _sf(data.get("d")) or 0.0,
            "percent_change": _sf(data.get("dp")) or 0.0,
            "high": _sf(data.get("h")) or 0.0,
            "low": _sf(data.get("l")) or 0.0,
            "open": _sf(data.get("o")) or 0.0,
            "previous_close": _sf(data.get("pc")) or 0.0,
            "volume": data.get("volume"),
            "average_volume": data.get("average_volume"),
            "fifty_two_week": data.get("fifty_two_week"),
        })
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.warning("Quote request degraded symbol=%s error=%s", symbol, error)
        return jsonify({"symbol": symbol, "current_price": 0.0, "change": 0.0, "percent_change": 0.0, "high": 0.0, "low": 0.0, "open": 0.0, "previous_close": 0.0})


@app.get("/api/profile/<symbol>")
def profile(symbol):
    try:
        symbol = validate_symbol(symbol)
        data = get_cached_profile(symbol)
        return jsonify({
            "name": data.get("name", ""),
            "logo": data.get("logo", ""),
            "industry": data.get("finnhubIndustry", ""),
            "market_cap": _sf(data.get("marketCapitalization")) or 0.0,
            "country": data.get("country", ""),
        })
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.warning("Profile request degraded symbol=%s error=%s", symbol, error)
        return jsonify({"name": "", "logo": "", "industry": "", "market_cap": 0.0, "country": ""})


@app.get("/api/candles/<symbol>")
def candles(symbol):
    try:
        symbol = validate_symbol(symbol)
        resolution = (request.args.get("resolution") or "D").upper()
        if resolution not in VALID_RESOLUTIONS:
            raise ValueError(f"Invalid resolution. Must be one of: {sorted(VALID_RESOLUTIONS)}")

        days = int(request.args.get("days", 30))
        if days < 1 or days > 10000:
            raise ValueError("Invalid days parameter (1-10000)")

        if request.args.get("forceRefresh") == "true":
            cache_key = (symbol.upper(), resolution, days)
            with CACHE_LOCK:
                CANDLE_CACHE.pop(cache_key, None)

        data = get_cached_candles(symbol, resolution, days)

        raw_timestamps = data.get("timestamps", [])
        use_et = resolution in {"1", "5", "15", "30", "60"}
        tz = ZoneInfo("America/New_York") if use_et else timezone.utc

        formatted_date_strings = []
        for ts in raw_timestamps:
            try:
                dt = datetime.fromtimestamp(int(ts), tz)
                formatted_date_strings.append(dt.strftime("%Y-%m-%d %H:%M") if use_et else dt.strftime("%Y-%m-%d"))
            except Exception:
                formatted_date_strings.append("")

        return jsonify({
            "c": data.get("close", []),
            "h": data.get("high", []),
            "l": data.get("low", []),
            "o": data.get("open", []),
            "v": data.get("volume", []),
            "t": formatted_date_strings,
            "timestamps": raw_timestamps,
            "open": data.get("open", []),
            "high": data.get("high", []),
            "low": data.get("low", []),
            "close": data.get("close", []),
            "volume": data.get("volume", []),
        })
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.warning("Candle request degraded symbol=%s error=%s", symbol, error)
        return jsonify({"c": [], "h": [], "l": [], "o": [], "v": [], "t": [], "timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []})


@app.get("/api/watchlist")
def watchlist():
    try:
        symbols_param = request.args.get("symbols", "")
        if not symbols_param:
            return jsonify([])
        symbols = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]
        if not symbols:
            return jsonify([])
        return jsonify(get_cached_watchlist(symbols))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.warning("Watchlist request degraded error=%s", error)
        return jsonify([])


@app.get("/api/price/<symbol>")
def price_lookup(symbol):
    try:
        raw_sym = symbol.strip().upper()
        if is_forex_pair(raw_sym):
            formatted_sym = format_forex_symbol(raw_sym)
            data = twelvedata_get("exchange_rate", {"symbol": formatted_sym})
            if data and "rate" in data:
                return jsonify({"symbol": raw_sym, "price": _sf(data["rate"]) or 0.0, "type": "exchange_rate"})
        data = twelvedata_get("price", {"symbol": raw_sym})
        if data and "price" in data:
            return jsonify({"symbol": raw_sym, "price": _sf(data["price"]) or 0.0, "type": "stock_price"})
        q = get_cached_quote(raw_sym)
        return jsonify({"symbol": raw_sym, "price": _sf(q.get("c")) or 0.0, "type": "quote"})
    except Exception as err:
        return jsonify({"error": str(err)}), 400


@app.get("/api/technicals/<symbol>")
def technicals(symbol):
    try:
        symbol = validate_symbol(symbol)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    cache_key = f"td:technicals:{symbol}"
    cached = _rget(cache_key)
    if cached:
        return jsonify(cached)

    with CACHE_LOCK:
        entry = TECHNICALS_CACHE.get(symbol)
        if entry and time.time() - entry["fetched_at"] < 300:
            _touch_cache(TECHNICALS_CACHE, symbol)
            return jsonify(entry["data"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_quote = pool.submit(get_cached_quote, symbol)
        f_rsi = pool.submit(twelvedata_get, "rsi", {"symbol": symbol, "interval": "1day", "time_period": 14, "outputsize": 1})
        f_macd = pool.submit(twelvedata_get, "macd", {"symbol": symbol, "interval": "1day", "outputsize": 1})
        f_candles = pool.submit(get_cached_candles, symbol, "D", 260)

        quote_data = f_quote.result() or {}
        rsi_data = f_rsi.result()
        macd_data = f_macd.result()
        candles_raw = f_candles.result() or {}

    rsi_val = None
    if rsi_data and rsi_data.get("values"):
        rsi_val = _sf(rsi_data["values"][0].get("rsi"))

    macd_val, macd_signal, macd_hist = None, None, None
    if macd_data and macd_data.get("values"):
        v = macd_data["values"][0]
        macd_val = _sf(v.get("macd"))
        macd_signal = _sf(v.get("macd_signal"))
        macd_hist = _sf(v.get("macd_hist"))

    closes = candles_raw.get("close", []) or []

    sma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else None
    sma50 = round(sum(closes[-50:]) / 50, 2) if len(closes) >= 50 else None
    sma200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else None

    bb_upper = bb_lower = bb_mid = None
    if len(closes) >= 20:
        c20 = closes[-20:]
        mean20 = sum(c20) / 20
        variance = sum((x - mean20) ** 2 for x in c20) / 20
        std20 = variance ** 0.5
        bb_upper = round(mean20 + (2 * std20), 2)
        bb_lower = round(mean20 - (2 * std20), 2)
        bb_mid = round(mean20, 2)

    current_p = _sf(quote_data.get("c")) or (closes[-1] if closes else 0.0)

    def _pct_return(lookback):
        if len(closes) >= lookback and closes[-lookback] > 0:
            return round(((current_p - closes[-lookback]) / closes[-lookback]) * 100, 2)
        return None

    ret_1m = _pct_return(22)
    ret_3m = _pct_return(66)
    ret_6m = _pct_return(132)
    ret_1y = _pct_return(250)

    rsi_status = "Neutral"
    if rsi_val is not None:
        if rsi_val >= 70:
            rsi_status = "Overbought"
        elif rsi_val <= 30:
            rsi_status = "Oversold"

    macd_status = "Neutral"
    if macd_hist is not None:
        macd_status = "Bullish Momentum" if macd_hist > 0 else "Bearish Pressure"

    trend_signal = "Neutral"
    if current_p and sma50 and sma200:
        if current_p > sma50 > sma200:
            trend_signal = "Strong Bullish"
        elif current_p < sma50 < sma200:
            trend_signal = "Strong Bearish"
        elif current_p > sma50:
            trend_signal = "Moderate Bullish"
        else:
            trend_signal = "Moderate Bearish"
    elif current_p and sma50:
        trend_signal = "Bullish Bias" if current_p > sma50 else "Bearish Bias"

    vol_ratio = None
    v = quote_data.get("volume")
    avg_v = quote_data.get("average_volume")
    if v and avg_v and avg_v > 0:
        vol_ratio = round(v / avg_v, 2)

    payload = {
        "symbol": symbol,
        "current_price": current_p,
        "change": quote_data.get("d"),
        "percent_change": quote_data.get("dp"),
        "fifty_two_week": quote_data.get("fifty_two_week"),
        "volume": v,
        "average_volume": avg_v,
        "volume_ratio": vol_ratio,
        "rsi_14": rsi_val,
        "rsi_status": rsi_status,
        "macd": {
            "value": macd_val,
            "signal": macd_signal,
            "histogram": macd_hist,
            "status": macd_status,
        },
        "moving_averages": {
            "sma_20": sma20,
            "sma_50": sma50,
            "sma_200": sma200,
            "price_vs_sma20": round(((current_p - sma20) / sma20) * 100, 2) if current_p and sma20 else None,
            "price_vs_sma50": round(((current_p - sma50) / sma50) * 100, 2) if current_p and sma50 else None,
            "price_vs_sma200": round(((current_p - sma200) / sma200) * 100, 2) if current_p and sma200 else None,
        },
        "bollinger_bands": {"upper": bb_upper, "middle": bb_mid, "lower": bb_lower},
        "returns": {"return_1m": ret_1m, "return_3m": ret_3m, "return_6m": ret_6m, "return_1y": ret_1y},
        "signals": {
            "trend": trend_signal,
            "rsi": rsi_status,
            "macd": macd_status,
            "golden_cross": bool(sma50 and sma200 and sma50 > sma200),
        },
        "timestamp": int(time.time()),
    }

    _rset(cache_key, payload, 300)
    with CACHE_LOCK:
        TECHNICALS_CACHE[symbol] = {"fetched_at": time.time(), "data": payload}
        _prune_cache(TECHNICALS_CACHE)
    return jsonify(payload)


@app.get("/api/news/<symbol>")
def news(symbol):
    try:
        symbol = validate_symbol(symbol)
        articles = get_cached_news(symbol)

        # headline required; url normalized upstream
        valid_articles = [
            a for a in (articles if isinstance(articles, list) else [])
            if a.get("headline")
        ]
        valid_articles.sort(key=lambda a: a.get("datetime", 0), reverse=True)

        return jsonify([
            {
                "headline": a.get("headline", ""),
                "source": a.get("source", ""),
                "url": a.get("url") or _google_news_url(a.get("headline", ""), symbol),
                "image": a.get("image", ""),
                "summary": a.get("summary", ""),
                "published": datetime.fromtimestamp(
                    a.get("datetime", int(time.time())), timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
            }
            for a in valid_articles[:8]
        ])
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.warning("News request degraded symbol=%s error=%s", symbol, error)
        return jsonify([])


@app.get("/api/wordcloud/<symbol>")
def wordcloud(symbol):
    try:
        symbol = validate_symbol(symbol)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    try:
        if request.args.get("forceRefresh") == "true":
            key = symbol.upper()
            with CACHE_LOCK:
                WORDCLOUD_CACHE.pop(key, None)
                NEWS_CACHE.pop(key, None)
            if _redis_client:
                try:
                    _redis_client.delete(f"v2:wc:news:{key}")
                except Exception:
                    pass

        payload = get_cached_wordcloud(symbol)
        return jsonify(payload)
    except Exception as error:
        logger.warning("Wordcloud request degraded symbol=%s error=%s", symbol, error)
        return jsonify({
            "symbol": symbol,
            "words": [],
            "article_count": 0,
            "generated_at": int(time.time()),
            "expires_at": int(_next_wordcloud_refresh_epoch()),
        })


# ---------------------------------------------------------------------------
# Fundamentals & Valuation
# ---------------------------------------------------------------------------
_TTL_1DAY = 86400
_EMPTY_FUNDAMENTALS = {"symbol": "", "name": "", "sector": "", "industry": "", "market_cap": None, "pe_ratio": None, "eps": None, "revenue": None, "profit_margin": None, "shares_outstanding": None, "country": "", "description": ""}
_EMPTY_INCOME = {"symbol": "", "periods": [], "total_revenue": [], "gross_profit": [], "operating_income": [], "net_income": [], "ebitda": [], "synthetic": False}
_EMPTY_VALUATION = {"symbol": "", "pe_ratio": None, "pb_ratio": None, "ps_ratio": None, "ev_ebitda": None, "peg_ratio": None, "enterprise_value": None, "market_cap": None}


def _get_finnhub_metrics(symbol):
    try:
        data = finnhub_get("stock/metric", {"symbol": symbol, "metric": "all"})
        return data.get("metric", {}) if isinstance(data, dict) else {}
    except Exception as err:
        logger.warning("Finnhub metrics fetch failed symbol=%s err=%s", symbol, err)
        return {}


def _fundamentals_payload(symbol):
    profile = get_cached_profile(symbol)
    metrics = _get_finnhub_metrics(symbol)

    description = ""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.info or {}
        description = str(info.get("longBusinessSummary") or "")[:600]
    except Exception:
        pass

    if not description:
        description = f"{profile.get('name', symbol)} operates in the {profile.get('finnhubIndustry', 'global market')} sector."

    market_cap_raw = _sf(metrics.get("marketCapitalization")) or _sf(profile.get("marketCapitalization"))
    market_cap = market_cap_raw * 1e6 if market_cap_raw else None

    rev_per_share = _sf(metrics.get("revenuePerShareTTM"))
    shares = _sf(metrics.get("sharesOutstanding"))
    total_rev = None
    if rev_per_share and shares:
        total_rev = rev_per_share * shares * 1e6
    elif market_cap and _sf(metrics.get("psTTM")):
        total_rev = market_cap / _sf(metrics.get("psTTM"))

    margin = _sf(metrics.get("netMarginTTM")) or _sf(metrics.get("netMarginAnnual"))
    if margin:
        margin = margin / 100.0

    return {
        "symbol": symbol,
        "name": profile.get("name") or symbol,
        "sector": profile.get("finnhubIndustry") or "",
        "industry": profile.get("finnhubIndustry") or "",
        "market_cap": market_cap,
        "pe_ratio": _sf(metrics.get("peTTM")) or _sf(metrics.get("peNormalizedAnnual")) or _sf(metrics.get("peAnnual")),
        "eps": _sf(metrics.get("epsTTM")) or _sf(metrics.get("epsAnnual")),
        "revenue": total_rev,
        "profit_margin": margin,
        "shares_outstanding": shares,
        "country": profile.get("country") or "",
        "description": description,
    }


def _income_payload(symbol):
    try:
        import pandas as pd
        import yfinance as yf
        t = yf.Ticker(symbol)
        df = t.financials
        if df is not None and not df.empty:
            df = df.T.sort_index()

            def col(df, *names):
                for n in names:
                    if n in df.columns:
                        return [None if pd.isna(v) else round(float(v), 2) for v in df[n]]
                return []

            periods = [str(idx)[:10] for idx in df.index]
            return {
                "symbol": symbol,
                "periods": periods,
                "total_revenue": col(df, "Total Revenue"),
                "gross_profit": col(df, "Gross Profit"),
                "operating_income": col(df, "Operating Income", "EBIT"),
                "net_income": col(df, "Net Income"),
                "ebitda": col(df, "EBITDA", "Normalized EBITDA"),
                "synthetic": False,
            }
    except Exception as err:
        logger.warning("yfinance income failed symbol=%s err=%s", symbol, err)

    profile = get_cached_profile(symbol)
    metrics = _get_finnhub_metrics(symbol)
    market_cap_raw = _sf(metrics.get("marketCapitalization")) or _sf(profile.get("marketCapitalization")) or 1e5
    market_cap = market_cap_raw * 1e6
    ps = _sf(metrics.get("psTTM")) or 5.0
    base_rev = market_cap / ps if ps > 0 else 1e10
    growth = (_sf(metrics.get("revenueGrowthTTMYoy")) or 10.0) / 100.0
    margin = (_sf(metrics.get("netMarginTTM")) or 15.0) / 100.0

    revs = [round(base_rev / ((1 + growth) ** (3 - i)), 2) for i in range(4)]
    nets = [round(r * margin, 2) for r in revs]
    gross = [round(r * 0.45, 2) for r in revs]
    op = [round(r * 0.25, 2) for r in revs]
    ebitda = [round(r * 0.30, 2) for r in revs]

    return {
        "symbol": symbol,
        "periods": ["2021", "2022", "2023", "2024"],
        "total_revenue": revs,
        "gross_profit": gross,
        "operating_income": op,
        "net_income": nets,
        "ebitda": ebitda,
        "synthetic": True,
        "_note": "Synthetic estimate — real financials unavailable",
    }


def _valuation_payload(symbol):
    profile = get_cached_profile(symbol)
    metrics = _get_finnhub_metrics(symbol)

    market_cap_raw = _sf(metrics.get("marketCapitalization")) or _sf(profile.get("marketCapitalization"))
    market_cap = market_cap_raw * 1e6 if market_cap_raw else None

    pe = _sf(metrics.get("peTTM")) or _sf(metrics.get("peNormalizedAnnual")) or _sf(metrics.get("peAnnual"))
    pb = _sf(metrics.get("pbQuarterly")) or _sf(metrics.get("pbAnnual"))
    ps = _sf(metrics.get("psTTM")) or _sf(metrics.get("psAnnual"))
    peg = _sf(metrics.get("pegAnnual")) or _sf(metrics.get("pegTTM"))
    ev_ebitda = _sf(metrics.get("evToEbitdaAnnual")) or _sf(metrics.get("evToEbitdaTTM"))

    ev_raw = _sf(metrics.get("enterpriseValue"))
    ev = ev_raw * 1e6 if ev_raw else None

    if not ev_ebitda or not ev:
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            info = t.info or {}
            if not ev_ebitda:
                ev_ebitda = _sf(info.get("enterpriseToEbitda"))
            if not ev:
                ev = _sf(info.get("enterpriseValue"))
        except Exception:
            pass

    return {
        "symbol": symbol,
        "pe_ratio": pe,
        "pb_ratio": pb,
        "ps_ratio": ps,
        "ev_ebitda": ev_ebitda,
        "peg_ratio": peg,
        "enterprise_value": ev or market_cap,
        "market_cap": market_cap,
    }


def _fundamentals_endpoint(symbol, cache_key_tpl, builder_fn, empty_template, ttl):
    try:
        symbol = validate_symbol(symbol)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    cache_key = cache_key_tpl.format(symbol=symbol)
    cached = _rget(cache_key)
    if cached:
        return jsonify(cached)

    try:
        payload = builder_fn(symbol)
        _rset(cache_key, payload, ttl)
        return jsonify(payload)
    except Exception as err:
        logger.warning("Fundamentals %s failed symbol=%s err=%s", cache_key_tpl, symbol, err)
        stale = _rget(cache_key)
        if stale:
            return jsonify(stale)
        return jsonify(dict(empty_template, symbol=symbol))


@app.get("/api/fundamentals/<symbol>")
def fundamentals(symbol):
    return _fundamentals_endpoint(symbol, "fin:fundamentals:{symbol}", _fundamentals_payload, _EMPTY_FUNDAMENTALS, _TTL_1DAY)


@app.get("/api/income/<symbol>")
def income(symbol):
    return _fundamentals_endpoint(symbol, "fin:income:{symbol}", _income_payload, _EMPTY_INCOME, _TTL_1DAY)


@app.get("/api/valuation/<symbol>")
def valuation(symbol):
    return _fundamentals_endpoint(symbol, "fin:valuation:{symbol}", _valuation_payload, _EMPTY_VALUATION, _TTL_1DAY)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "False") == "True")