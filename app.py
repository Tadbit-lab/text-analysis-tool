import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

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


@lru_cache(maxsize=512)
def get_cached_quote(symbol, time_key):
    del time_key
    return finnhub_get("quote", {"symbol": symbol})


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
        data = get_cached_quote(symbol, int(time.time() // 10))
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
        return api_error(error)


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