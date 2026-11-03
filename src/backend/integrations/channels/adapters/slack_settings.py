"""slack API configuration."""
import os
SLACK_API_BASE = os.getenv("SLACK_API_BASE", "https://slack.com/api").rstrip("/")
