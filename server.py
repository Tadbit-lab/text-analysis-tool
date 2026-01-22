from flask import Flask, abort, request, jsonify
from flask_cors import CORS
from stockAnalyze import getCompanyStockInfo
from analyze import analyzeText
import json
import traceback

# Optional: load test data for fallback
with open('test/results.json') as f:
    stockDataTest = json.load(f)

app = Flask(__name__)
CORS(app)

# -------------------------
# Health check
# -------------------------
@app.route('/health', methods=["GET"])
def healthCheck():
    return jsonify({"status": "up", "message": "Flask server is running"})


# -------------------------
# Analyze stock for ANY ticker
# -------------------------
@app.route('/analyze-stock/<ticker>', methods=["GET"])
def analyzeStockRoute(ticker):
    ticker = ticker.upper().strip()
    if not ticker:
        abort(400, 'No ticker provided')

    try:
        # Attempt to fetch live stock data
        stockData = getCompanyStockInfo(ticker)
        return jsonify({
            "success": True,
            "ticker": ticker,
            "data": stockData
        })

    except Exception as e:
        # Print full traceback for debugging
        print(f"Error fetching stock data for {ticker}:")
        traceback.print_exc()

        # Return fallback data if live fetch fails
        return jsonify({
            "success": False,
            "ticker": ticker,
            "message": f"Failed to fetch live data for {ticker}, using fallback data.",
            "data": stockDataTest,
            "error": str(e)
        })


# -------------------------
# Analyze text
# -------------------------
@app.route('/analyze-text', methods=["POST"])
def analyzeTextHandler():
    data = request.get_json()
    if not data or not data.get("text"):
        abort(400, 'No text provided to analyze.')

    try:
        result = analyzeText(data["text"])
        return jsonify({
            "success": True,
            "result": result
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": "Text analysis failed.",
            "error": str(e)
        })


# -------------------------
# Main
# -------------------------
if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=True)
