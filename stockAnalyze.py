import json

import yfinance as yf
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import analyze

def extractBasicInfo(data):
  keysToExtract = ['longName','sector', 'website', 'fullTimeEmployees', 'marketCap', 'totalRevenue', 'trailingEps']
  basicInfo = {}
  for key in keysToExtract:
    if key in data:
      basicInfo[key] = data[key]
    else:
      basicInfo[key] = ""
  return basicInfo

def getPriceHistory(company):
  companyHistory = company.history(period='12mo')
  prices = companyHistory['Open'].tolist()
  dates = companyHistory.index.strftime('%Y-%m-%d').tolist()
  return {
    'price': prices,
    'date': dates
  }

def getEarningsDates(company):
  return []
  earningDatesDF = company.earnings_dates
  allDates = earningDatesDF.index.strftime('%Y-%m-%d').tolist()
  date_objects = [datetime.strptime(d, '%Y-%m-%d') for d in allDates]
  today = datetime.today()
  future_dates = [d.strftime('%Y-%m-%d') for d in date_objects if d > today]
  return future_dates

def getCompanyNews(company):
  newsList = company.news
  allNewsArticles = []
  for newsDict in newsList:
    newsDictToAdd = {
      'title': newsDict['content']['title'],
      'link': newsDict['content']['canonicalUrl']['url']
    }
    allNewsArticles.append(newsDictToAdd)
  return allNewsArticles

def extractNewsArticleTextFromHtml(soup):
    selectors = [
        ("div", {"class": "body yf-h0on0w"}),
        ("article", {}),
        ("div", {"class": "article__content"}),
        ("p", {})  # fallback: collect all paragraphs
    ]
    allText = ""
    for tag, attrs in selectors:
        for element in soup.find_all(tag, attrs):
            allText += element.get_text(separator=" ", strip=True) + " "
    return allText.strip()


headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}

def extractCompanyNewsArticles(newsArticles):
  allArticleText = ""
  
  for newsArticle in newsArticles:
      url = newsArticle['link']
      
      try:
          page = requests.get(url, headers=headers)
          page.raise_for_status()  # Raise exception for bad status codes
          
          soup = BeautifulSoup(page.text, 'html.parser')

          if soup.find_all(string="Continue Reading"):
              print(f'Skipping article (paywall)')
              continue
          else:
              print(f"Processing article")
              articleText = extractNewsArticleTextFromHtml(soup)
              if articleText:
                  allArticleText += articleText + " "
                  
      except requests.exceptions.Timeout:
          print(f"Timeout fetching article: {url}")
          continue
      except requests.exceptions.ConnectionError:
          print(f"Connection error for article: {url}")
          continue
      except requests.exceptions.HTTPError as e:
          print(f"HTTP error {e.response.status_code} for article: {url}")
          continue
      except Exception as e:
          print(f"Unexpected error fetching article {url}: {e}")
          continue

  return allArticleText.strip()

def getCompanyStockInfo(tickerSymbol):
  #GEt data from Yahoo Finance API
  company = yf.Ticker(tickerSymbol)

  #Get basic info on company
  basicInfo = extractBasicInfo(company.info)

  #Check if company exists, if not, trigger error
  if not basicInfo["longName"]:
    raise NameError("Could not find stock info, ticker may be delisted or does not exist")

  priceHistory = getPriceHistory(company)
  futureEarningsDates = getEarningsDates(company)
  newsArticles = getCompanyNews(company)
  newsArticlesAllText = extractCompanyNewsArticles(newsArticles)
  newsTextAnalysis = analyze.analyzeText(newsArticlesAllText)

  finalStockAnalysis = {
    "basicInfo": basicInfo,
    "priceHistory": priceHistory,
    "futureEarningsDates": futureEarningsDates,
    "newsArticles": newsArticles,
    "newsTextAnalysis": newsTextAnalysis
  }
  return finalStockAnalysis


# companySTockAnalysis = getCompanyStockInfo('MSFT')
# print(json.dumps(companySTockAnalysis, indent=4))