import os
import nltk
from nltk.tokenize import word_tokenize, sent_tokenize
from wordcloud import WordCloud
import base64
from io import BytesIO

# ---------------------------
# NLTK SAFE SETUP (NO DOWNLOADS)
# ---------------------------

LOCAL_NLTK_PATH = os.path.join(os.path.dirname(__file__), "nltk_data")
nltk.data.path.append(LOCAL_NLTK_PATH)

def nltk_resource_exists(path):
    try:
        nltk.data.find(path)
        return True
    except LookupError:
        return False

HAS_STOPWORDS = nltk_resource_exists("corpora/stopwords")
HAS_WORDNET = nltk_resource_exists("corpora/wordnet")
HAS_VADER = nltk_resource_exists("sentiment/vader_lexicon")

if HAS_STOPWORDS:
    from nltk.corpus import stopwords
    STOP_WORDS = set(stopwords.words("english"))
else:
    STOP_WORDS = set()

if HAS_VADER:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    SENTIMENT = SentimentIntensityAnalyzer()
else:
    SENTIMENT = None

# ---------------------------
# Utility functions
# ---------------------------

def tokenizeSentences(text):
    try:
        return sent_tokenize(text)
    except LookupError:
        return text.split(".")

def tokenizeWords(sentences):
    words = []
    for s in sentences:
        try:
            words.extend(word_tokenize(s))
        except LookupError:
            words.extend(s.split())
    return words

def cleanseWords(words):
    clean = []
    for w in words:
        w = w.lower()
        if w.isalpha() and w not in STOP_WORDS:
            clean.append(w)
    return clean

# ---------------------------
# MAIN ANALYSIS
# ---------------------------

def analyzeText(text):
    sentences = tokenizeSentences(text)
    words = tokenizeWords(sentences)
    cleaned_words = cleanseWords(words)

    wc = WordCloud(
        width=800,
        height=500,
        background_color="white"
    ).generate(" ".join(cleaned_words))

    img_buffer = BytesIO()
    wc.to_image().save(img_buffer, format="PNG")
    img_buffer.seek(0)

    encoded_wc = base64.b64encode(img_buffer.read()).decode("utf-8")

    sentiment = (
        SENTIMENT.polarity_scores(text)
        if SENTIMENT
        else {"note": "Sentiment disabled (missing vader_lexicon)"}
    )

    return {
        "data": {
            "sentences": len(sentences),
            "words": len(cleaned_words),
            "sentiment": sentiment,
            "wordCloudImage": encoded_wc
        },
        "metadata": {
            "stopwords_loaded": HAS_STOPWORDS,
            "wordnet_loaded": HAS_WORDNET,
            "vader_loaded": HAS_VADER
        }
    }
