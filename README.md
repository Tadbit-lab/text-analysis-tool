# Stock Analysis Tool

A comprehensive web application that provides in-depth financial analysis for stocks, including quotes, historical data, news, and fundamental analysis.

## Features

- **Real-time Quotes**: Up-to-date stock prices and market data.
- **Historical Data**: Charts and analysis for various timeframes.
- **News Aggregation**: Latest news from multiple financial sources.
- **Fundamental Analysis**: Comprehensive financial statements and metrics.
- **News Sentiment Analysis**: Real-time analysis of news sentiment.
- **Watchlist Management**: Create and manage personalized watchlists.
- **Caching**: Efficient caching of API responses to ensure fast performance.

## Tech Stack

- **Backend**: Python 3.12+ with Flask
- **Frontend**: React 19 with Vite
- **Data Sources**:
  - Finnhub API
  - Twelve Data API
  - Alpha Vantage API
  - NewsAPI
  - defeatbeta-api (for fundamentals data)
- **Cache**: Redis

## Installation

### Prerequisites

- Python 3.12+
- Node.js 18+
- Redis Server (or access to a Redis instance)

### Backend Setup

1.  Clone the repository:
    ```bash
    git clone <repository-url>
    cd text-analysis-tool
    ```

2.  Create a virtual environment:
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows use `venv\Scripts\activate`
    ```

3.  Install Python dependencies:
    ```bash
    pip install -r requirements.txt
    ```

4.  Create a `.env` file in the root directory and add your API keys:
    ```env
    FINNHUB_API_KEY=your_finnhub_api_key
    ALPHAVANTAGE_API_KEY=your_alphavantage_api_key
    TWELVEDATA_API_KEY=your_twelvedata_api_key
    NEWS_API_KEY=your_newsapi_key
    REDIS_URL=your_redis_url
    ```

5.  Run the Flask server:
    ```bash
    flask run
    ```

### Frontend Setup

1.  Open a new terminal and navigate to the frontend directory:
    ```bash
    cd client
    ```

2.  Install Node.js dependencies:
    ```bash
    npm install
    ```

3.  Run the React development server:
    ```bash
    npm run dev
    ```

The application will be available at `http://localhost:5173`.

## Usage

- **Quotes**: Search for stock symbols to view quotes and charts.
- **News**: Read the latest news articles with sentiment analysis.
- **Fundamentals**: Access income statements, balance sheets, and cash flow statements.
- **Watchlists**: Save stocks to your watchlist for quick access.
- **Technical Analysis**: Get support and resistance levels.

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `FINNHUB_API_KEY` | Finnhub API key | Yes |
| `ALPHAVANTAGE_API_KEY` | Alpha Vantage API key | Yes |
| `TWELVEDATA_API_KEY` | Twelve Data API key | Yes |
| `NEWS_API_KEY` | NewsAPI key | Yes |
| `REDIS_URL` | Redis connection string | Yes |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | No |

