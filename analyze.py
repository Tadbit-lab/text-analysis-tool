from random_username.generate import generate_username
# Welcome User
def welcomeUser():
    print("Welcome to the text analysis tool. " \
    "\nI will mine and analyze a body of text in a file you give me")

# Get Username
def getUsername():

    maxAttempts = 3
    attempts = 0

    while attempts < maxAttempts:
        # Get input from user into the terminal
        inputPrompt = ""
        if attempts == 0:
            inputPrompt = ("To begin, please enter your username:\n")
        else:
            inputPrompt = ("Please, try again:\n")
        usernameFrominput = input(inputPrompt)

        if (len(usernameFrominput) < 4) or (not usernameFrominput.isidentifier()):
            print("Your username must be at least 4 characters long, alphanumeric only,\nhave no spaces, and cannot start with a symbol")
        else:
            return usernameFrominput
        
        attempts += 1

    print("\nExhausted all " + str(maxAttempts) + " attempts, assigning new username...")
    return generate_username()[0]


#Greet the user
def greetUser(name):
    print("Hello " + name + "!")

