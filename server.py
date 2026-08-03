import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo

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
MAX_CACHE_ENTRIES = 256
WATCHLIST_TTL_SECONDS = 300
QUOTE_CACHE = {}
PROFILE_CACHE = {}
CANDLE_CACHE = {}
WATCHLIST_CACHE = {}
NEWS_CACHE = {}
CACHE_LOCK = Lock()
IN_FLIGHT = {}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

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


def is_market_open():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def _prune_cache(cache):
    if len(cache) > MAX_CACHE_ENTRIES:
        for key in list(cache)[: len(cache) - MAX_CACHE_ENTRIES]:
            del cache[key]


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
    if normalized in {"1D", "D"} or days <= 1:
        return "1D"
    if normalized in {"1W", "W"} or days <= 7:
        return "1W"
    if normalized in {"1M", "M"} or days <= 31:
        return "1M"
    if days <= 183:
        return "6M"
    if days <= 365:
        return "1Y"
    if days <= 1825:
        return "5Y"
    return "MAX"


def _candle_ttl_seconds(days, resolution):
    timeframe = _candle_timeframe(days, resolution)
    if timeframe == "1D" and not is_market_open():
        return None
    return {
        "1D": 300,
        "1W": 3600,
        "1M": 21600,
        "6M": 43200,
        "1Y": 86400,
        "5Y": 259200,
        "MAX": 604800,
    }[timeframe]


def finnhub_get(endpoint, params):
    if not FINNHUB_API_KEY:
        logger.error("Finnhub API key is not configured")
        raise FinnhubError("Finnhub API key is not configured")

    request_params = {**params, "token": FINNHUB_API_KEY}
    url = f"{FINNHUB_BASE_URL}/{endpoint}"
    started_at = time.perf_counter()
    try:
        response = requests.get(url, params=request_params, timeout=REQUEST_TIMEOUT_SECONDS)
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


def get_cached_quote(symbol):
    key = symbol.upper()
    now = time.time()
    with CACHE_LOCK:
        entry = QUOTE_CACHE.get(key)
        if entry and now - entry["fetched_at"] < _quote_ttl_seconds():
            return entry["data"]
        if key in IN_FLIGHT:
            return entry["data"] if entry else _get_empty_quote_payload()
        IN_FLIGHT[key] = now

    try:
        data = finnhub_get("quote", {"symbol": symbol})
    except FinnhubError as error:
        logger.warning("Quote fallback activated symbol=%s error=%s", symbol, error)
        with CACHE_LOCK:
            IN_FLIGHT.pop(key, None)
            entry = QUOTE_CACHE.get(key)
            if entry:
                return entry["data"]
            fallback = _get_empty_quote_payload()
            QUOTE_CACHE[key] = {"fetched_at": now, "data": fallback}
            _prune_cache(QUOTE_CACHE)
            return fallback

    payload = data or _get_empty_quote_payload()
    with CACHE_LOCK:
        QUOTE_CACHE[key] = {"fetched_at": now, "data": payload}
        _prune_cache(QUOTE_CACHE)
        IN_FLIGHT.pop(key, None)
    return payload


def get_cached_profile(symbol):
    key = symbol.upper()
    now = time.time()
    with CACHE_LOCK:
        entry = PROFILE_CACHE.get(key)
        if entry and now - entry["fetched_at"] < 3600:
            return entry["data"]
        if key in IN_FLIGHT:
            return entry["data"] if entry else _get_empty_profile_payload()
        IN_FLIGHT[key] = now

    try:
        data = finnhub_get("stock/profile2", {"symbol": symbol})
    except FinnhubError as error:
        logger.warning("Profile fallback activated symbol=%s error=%s", symbol, error)
        with CACHE_LOCK:
            IN_FLIGHT.pop(key, None)
            entry = PROFILE_CACHE.get(key)
            if entry:
                return entry["data"]
            fallback = _get_empty_profile_payload()
            PROFILE_CACHE[key] = {"fetched_at": now, "data": fallback}
            _prune_cache(PROFILE_CACHE)
            return fallback

    payload = {
        "name": data.get("name", "") if isinstance(data, dict) else "",
        "logo": data.get("logo", "") if isinstance(data, dict) else "",
        "finnhubIndustry": data.get("finnhubIndustry", "") if isinstance(data, dict) else "",
        "marketCapitalization": float(data.get("marketCapitalization", 0)) if isinstance(data, dict) else 0.0,
        "country": data.get("country", "") if isinstance(data, dict) else "",
    }
    with CACHE_LOCK:
        PROFILE_CACHE[key] = {"fetched_at": now, "data": payload}
        _prune_cache(PROFILE_CACHE)
        IN_FLIGHT.pop(key, None)
    return payload


ALPHAVANTAGE_FUNCTIONS = {
    "D": "TIME_SERIES_DAILY",
    "W": "TIME_SERIES_WEEKLY",
    "M": "TIME_SERIES_MONTHLY",
}


def fetch_alpha_vantage_candles(symbol, resolution, days):
    func = ALPHAVANTAGE_FUNCTIONS.get(resolution.upper())
    if not func:
        return _get_empty_candle_payload()

    url = "https://www.alphavantage.co/query"
    params = {"function": func, "symbol": symbol, "apikey": ALPHAVANTAGE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.warning(
                "AlphaVantage non-200 status=%s for symbol=%s func=%s",
                resp.status_code,
                symbol,
                func,
            )
            return _get_empty_candle_payload()
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
        return _get_empty_candle_payload()

    if (
        not isinstance(data, dict)
        or "Note" in data
        or "Error Message" in data
        or "Information" in data
    ):
        logger.warning(
            "AlphaVantage returned error/note/information for symbol=%s func=%s",
            symbol,
            func,
        )
        return _get_empty_candle_payload()

    series = None
    for k, v in data.items():
        if "Time Series" in k and isinstance(v, dict):
            series = v
            break

    if not series:
        logger.warning("AlphaVantage missing time series for symbol=%s func=%s", symbol, func)
        return _get_empty_candle_payload()

    try:
        all_dates = sorted(series.keys())
    except Exception:
        logger.exception("Failed sorting AlphaVantage series keys for symbol=%s", symbol)
        return _get_empty_candle_payload()

    selected = all_dates[-int(days) :] if days > 0 else []
    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for date_str in selected:
        try:
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


def get_cached_candles(symbol, resolution, days):
    cache_key = (symbol.upper(), resolution.upper(), int(days))
    now = time.time()
    ttl_seconds = _candle_ttl_seconds(days, resolution)
    with CACHE_LOCK:
        entry = CANDLE_CACHE.get(cache_key)
        if entry and (ttl_seconds is None or now - entry["fetched_at"] < ttl_seconds):
            return entry["data"]
        if ttl_seconds is None:
            return entry["data"] if entry else _get_empty_candle_payload()
        if cache_key in IN_FLIGHT:
            return entry["data"] if entry else _get_empty_candle_payload()
        IN_FLIGHT[cache_key] = now

    try:
        data = fetch_alpha_vantage_candles(symbol, resolution, days)
    except Exception as error:
        logger.warning("Candle fallback activated symbol=%s error=%s", symbol, error)
        data = None

    if data is None:
        with CACHE_LOCK:
            IN_FLIGHT.pop(cache_key, None)
            entry = CANDLE_CACHE.get(cache_key)
            if entry:
                return entry["data"]
            fallback = _get_empty_candle_payload()
            CANDLE_CACHE[cache_key] = {"fetched_at": now, "data": fallback}
            _prune_cache(CANDLE_CACHE)
            return fallback

    with CACHE_LOCK:
        CANDLE_CACHE[cache_key] = {"fetched_at": now, "data": data}
        _prune_cache(CANDLE_CACHE)
        IN_FLIGHT.pop(cache_key, None)
    return data


def get_cached_watchlist(symbols):
    normalized_symbols = [validate_symbol(symbol) for symbol in symbols]
    cache_key = tuple(normalized_symbols)
    now = time.time()
    with CACHE_LOCK:
        entry = WATCHLIST_CACHE.get(cache_key)
        if entry and now - entry["fetched_at"] < WATCHLIST_TTL_SECONDS:
            return entry["data"]
        if cache_key in IN_FLIGHT:
            return entry["data"] if entry else []
        IN_FLIGHT[cache_key] = now

    payload = []
    for symbol in normalized_symbols:
        quote = get_cached_quote(symbol)
        payload.append(
            {
                "symbol": symbol,
                "current_price": float(quote.get("c", 0)),
                "change": float(quote.get("d", 0)),
                "percent_change": float(quote.get("dp", 0)),
                "high": float(quote.get("h", 0)),
                "low": float(quote.get("l", 0)),
                "open": float(quote.get("o", 0)),
                "previous_close": float(quote.get("pc", 0)),
            }
        )

    with CACHE_LOCK:
        WATCHLIST_CACHE[cache_key] = {"fetched_at": now, "data": payload}
        _prune_cache(WATCHLIST_CACHE)
        IN_FLIGHT.pop(cache_key, None)
    return payload


def get_cached_news(symbol, time_key):
    del time_key
    key = symbol.upper()
    now = time.time()
    with CACHE_LOCK:
        entry = NEWS_CACHE.get(key)
        if entry and now - entry["fetched_at"] < 300:
            return entry["data"]
        if key in IN_FLIGHT:
            return entry["data"] if entry else []
        IN_FLIGHT[key] = now

    today = datetime.now(timezone.utc).date()
    try:
        data = finnhub_get(
            "company-news",
            {
                "symbol": symbol,
                "from": (today - timedelta(days=7)).isoformat(),
                "to": today.isoformat(),
            },
        )
    except FinnhubError as error:
        logger.warning("News fallback activated symbol=%s error=%s", symbol, error)
        with CACHE_LOCK:
            IN_FLIGHT.pop(key, None)
            entry = NEWS_CACHE.get(key)
            if entry:
                return entry["data"]
            NEWS_CACHE[key] = {"fetched_at": now, "data": []}
            _prune_cache(NEWS_CACHE)
            return []

    payload = data if isinstance(data, list) else []
    with CACHE_LOCK:
        NEWS_CACHE[key] = {"fetched_at": now, "data": payload}
        _prune_cache(NEWS_CACHE)
        IN_FLIGHT.pop(key, None)
    return payload


def api_error(error):
    logger.warning("API request degraded error=%s", error)
    return jsonify({"error": "service unavailable"}), 200


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
        return jsonify(
            {
                "symbol": symbol,
                "current_price": float(data.get("c", 0)),
                "change": float(data.get("d", 0)),
                "percent_change": float(data.get("dp", 0)),
                "high": float(data.get("h", 0)),
                "low": float(data.get("l", 0)),
                "open": float(data.get("o", 0)),
                "previous_close": float(data.get("pc", 0)),
            }
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (FinnhubError, TypeError) as error:
        logger.warning("Quote request degraded symbol=%s error=%s", symbol, error)
        return jsonify(
            {
                "symbol": symbol,
                "current_price": 0.0,
                "change": 0.0,
                "percent_change": 0.0,
                "high": 0.0,
                "low": 0.0,
                "open": 0.0,
                "previous_close": 0.0,
            }
        )


@app.get("/api/profile/<symbol>")
def profile(symbol):
    try:
        symbol = validate_symbol(symbol)
        logger.info("Incoming symbol=%s", symbol)
        data = get_cached_profile(symbol)
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
        logger.warning("Profile request degraded symbol=%s error=%s", symbol, error)
        return jsonify(
            {
                "name": "",
                "logo": "",
                "industry": "",
                "market_cap": 0.0,
                "country": "",
            }
        )


@app.get("/api/candles/<symbol>")
def candles(symbol):
    try:
        symbol = validate_symbol(symbol)
        resolution = (request.args.get("resolution") or "D").upper()
        days = int(request.args.get("days", 30))
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
        data = get_cached_candles(symbol, resolution, days)
        return jsonify(
            {
                "timestamps": data.get("timestamps", []),
                "open": data.get("open", []),
                "high": data.get("high", []),
                "low": data.get("low", []),
                "close": data.get("close", []),
                "volume": data.get("volume", []),
            }
        )
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    except FinnhubError as error:
        logger.warning("Candle request degraded symbol=%s error=%s", symbol, error)
        return jsonify(
            {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        )


@app.get("/api/watchlist")
def watchlist():
    try:
        symbols_param = request.args.get("symbols", "")
        if not symbols_param:
            return jsonify([])
        symbols = [segment.strip().upper() for segment in symbols_param.split(",") if segment.strip()]
        if not symbols:
            return jsonify([])
        logger.info("Incoming watchlist symbols=%s", symbols)
        payload = get_cached_watchlist(symbols)
        return jsonify(payload)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (FinnhubError, TypeError) as error:
        logger.warning("Watchlist request degraded error=%s", error)
        return jsonify([])


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
        logger.warning("News request degraded symbol=%s error=%s", symbol, error)
        return jsonify([])
