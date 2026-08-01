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

REQUEST_TIMEOUT_SECONDS = 10
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.-]{1,10}$")

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


@lru_cache(maxsize=256)
def get_cached_candles(symbol, resolution, days, time_key):
    del time_key
    now = datetime.now(timezone.utc)
    return finnhub_get(
        "stock/candle",
        {
            "symbol": symbol,
            "resolution": resolution,
            "from": int((now - timedelta(days=days)).timestamp()),
            "to": int(now.timestamp()),
        },
    )


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
        if not resolution or days < 1 or days > 3650:
            raise ValueError("Invalid candle parameters")
        logger.info("Incoming symbol=%s", symbol)
        data = get_cached_candles(symbol, resolution, days, int(time.time() // 60))
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