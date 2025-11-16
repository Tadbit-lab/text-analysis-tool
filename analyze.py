import os
import re
from random_username.generate import generate_username
import nltk
from nltk.tokenize import word_tokenize, sent_tokenize
from nltk.stem import WordNetLemmatizer
from nltk.corpus import wordnet, stopwords
from wordcloud import WordCloud

# Download necessary NLTK data
nltk.download('stopwords')
nltk.download('wordnet')
nltk.download('averaged_perceptron_tagger')

# Initialize tools
wordLemmatizer = WordNetLemmatizer()
stopWords = set(stopwords.words('english'))

# Welcome User
def welcomeUser():
    print("\nWelcome to the text analysis tool! I will mine and analyze a body of text from a file you give me.")

# Get Username
def getUsername():
    maxAttempts = 3
    attempts = 0
    while attempts < maxAttempts:
        inputPrompt = "\nTo begin, please enter your username:\n" if attempts == 0 else "\nPlease try again:\n"
        usernameFromInput = input(inputPrompt)
        if len(usernameFromInput) < 5 or not usernameFromInput.isidentifier():
            print("Your username must be at least 5 characters long, alphanumeric only (a-z/A-Z/0-9), have no spaces, and cannot start with a number!")
        else:
            return usernameFromInput
        attempts += 1
    print(f"\nExhausted all {maxAttempts} attempts, assigning username instead...")
    return generate_username()[0]

# Greet the user
def greetUser(name):
    print(f"Hello, {name}")

# Get text from file
def getArticleText():
    try:
        with open("files/article.txt", "r") as f:
            rawText = f.read()
        return rawText.replace("\n", " ").replace("\r", "")
    except FileNotFoundError:
        print("Error: 'files/article.txt' not found.")
        return ""

# Tokenize sentences
def tokenizeSentences(rawText):
    return sent_tokenize(rawText)

# Tokenize words
def tokenizeWords(sentences):
    words = []
    for sentence in sentences:
        words.extend(word_tokenize(sentence))
    return words

# Extract key sentences
def extractKeySentences(sentences, searchPattern):
    return [s for s in sentences if re.search(searchPattern, s.lower())]

# Average words per sentence
def getWordsPerSentence(sentences):
    totalWords = sum(len(s.split()) for s in sentences)
    return totalWords / len(sentences) if sentences else 0

# POS tag conversion
posToWordnetTag = {
    "J": wordnet.ADJ,
    "V": wordnet.VERB,
    "N": wordnet.NOUN,
    "R": wordnet.ADV
}
def treebankPosToWordnetPos(pos):
    return posToWordnetTag.get(pos[0], wordnet.NOUN)

# Cleanse word list
def cleanseWordList(posTaggedWordTuples):
    cleansedWords = []
    invalidWordPattern = "[^a-zA-Z-+]"
    for word, pos in posTaggedWordTuples:
        cleaned = word.replace(".", "").lower()
        if not re.search(invalidWordPattern, cleaned) and len(cleaned) > 1 and cleaned not in stopWords:
            cleansedWords.append(wordLemmatizer.lemmatize(cleaned, treebankPosToWordnetPos(pos)))
    return cleansedWords

# Main execution
def main():
    welcomeUser()
    username = getUsername()
    greetUser(username)

    articleTextRaw = getArticleText()
    if not articleTextRaw:
        print("No text to analyze. Exiting.")
        return

    articleSentences = tokenizeSentences(articleTextRaw)
    articleWords = tokenizeWords(articleSentences)

    stockSearchPattern = r"[0-9]|[%$€£]|thousand|million|billion|trillion|profit|loss"
    keySentences = extractKeySentences(articleSentences, stockSearchPattern)
    wordsPerSentence = getWordsPerSentence(articleSentences)

    wordsPosTagged = nltk.pos_tag(articleWords)
    articleWordsCleansed = cleanseWordList(wordsPosTagged)

    print(f"Total sentences: {len(articleSentences)}")
    print(f"Average words per sentence: {wordsPerSentence:.2f}")
    print(f"Key sentences found: {len(keySentences)}")
    print(f"Cleaned word count: {len(articleWordsCleansed)}")

    if not articleWordsCleansed:
        print("No valid words found for word cloud. Exiting.")
        return

    try:
        os.makedirs("results", exist_ok=True)
        wordcloud = WordCloud(width=1000, height=700, background_color="white", colormap="Set3", collocations=False)
        wordcloud.generate(" ".join(articleWordsCleansed))
        wordcloud.to_file("results/wordcloud.png")
        print("Word cloud saved to 'results/wordcloud.png'")
    except Exception as e:
        print(f"Error generating word cloud: {e}")

if __name__ == "__main__":
    main()