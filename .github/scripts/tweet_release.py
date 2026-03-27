import os
import sys
import tweepy

# --- Check for required credentials ---
required = {
    "X_API_KEY": os.environ.get("X_API_KEY"),
    "X_API_SECRET": os.environ.get("X_API_SECRET"),
    "X_ACCESS_TOKEN": os.environ.get("X_ACCESS_TOKEN"),
    "X_ACCESS_TOKEN_SECRET": os.environ.get("X_ACCESS_TOKEN_SECRET"),
}

missing = [key for key, val in required.items() if not val]

if missing:
    # Exit cleanly — won't fail the GitHub Action as an error
    print(f"⚠️  Skipping tweet: missing credentials: {', '.join(missing)}")
    sys.exit(0)  # 0 = success, so the action stays green

# --- Auth ---
client = tweepy.Client(
    consumer_key=required["X_API_KEY"],
    consumer_secret=required["X_API_SECRET"],
    access_token=required["X_ACCESS_TOKEN"],
    access_token_secret=required["X_ACCESS_TOKEN_SECRET"],
)

# --- Build the tweet ---
tag  = os.environ.get("RELEASE_TAG", "unknown")
name = os.environ.get("RELEASE_NAME", "")
url  = os.environ.get("RELEASE_URL", "")
repo = os.environ.get("REPO_NAME", "")

tweet = f"🚀 New release: {repo} {tag}\n\n{name}\n\n{url}"

# --- Post it ---
response = client.create_tweet(text=tweet)
print(f"✅ Tweet posted! ID: {response.data['id']}")

