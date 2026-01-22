import yfinance as yf
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import analyze
import time

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
}

def extractBasicInfo(data):
    keysToExtract = ['longName','sector', 'website', 'fullTimeEmployees', 'marketCap', 'totalRevenue', 'trailingEps']
    return {key: data.get(key, "") for key in keysToExtract}

def getPriceHistory(company):
    hist = company.history(period='12mo')
    return {
        'price': hist['Open'].tolist(),
        'date': hist.index.strftime('%Y-%m-%d').tolist()
    }

def getEarningsDates(company):
    try:
        df = company.earnings_dates
        allDates = df.index.strftime('%Y-%m-%d').tolist()
        future_dates = [d for d in allDates if datetime.strptime(d, '%Y-%m-%d') > datetime.today()]
        return future_dates
    except:
        return []

def getCompanyNews(company):
    newsList = getattr(company, "news", [])
    articles = []
    for newsDict in newsList:
        title = newsDict.get("title") or newsDict.get("content", {}).get("title", "")
        link = newsDict.get("link") or newsDict.get("content", {}).get("canonicalUrl", {}).get("url", "")
        if title and link:
            articles.append({"title": title, "link": link})
    return articles

def extractCompanyNewsArticles(newsArticles):
    allText = ""
    for article in newsArticles[:5]:  # limit to 5 articles to avoid timeout
        url = article["link"]
        try:
            r = requests.get(url, headers=HEADERS, timeout=5)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            allText += " ".join(p.get_text() for p in soup.find_all("p"))
        except:
            continue
    return allText.strip()

def getCompanyStockInfo(ticker):
    # Retry logic
    for attempt in range(3):
        try:
            company = yf.Ticker(ticker)
            basicInfo = extractBasicInfo(company.info)
            if not basicInfo.get("longName"):
                raise ValueError("Ticker not found")
            priceHistory = getPriceHistory(company)
            futureEarningsDates = getEarningsDates(company)
            newsArticles = getCompanyNews(company)
            newsText = extractCompanyNewsArticles(newsArticles)
            newsTextAnalysis = analyze.analyzeText(newsText)
            return {
                "basicInfo": basicInfo,
                "priceHistory": priceHistory,
                "futureEarningsDates": futureEarningsDates,
                "newsArticles": newsArticles,
                "newsTextAnalysis": newsTextAnalysis
            }
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)
