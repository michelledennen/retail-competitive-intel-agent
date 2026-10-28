"""Runtime configuration, read from environment variables (see .env.example)."""

import os

# The Gemini model used by every agent. Override with the MODEL env var.
MODEL = os.getenv("MODEL", "gemini-3.5-flash")

APP_NAME = "retail_intel_agent"

# Context management: summarize older turns every N user messages so long
# strategy sessions stay within the model's context window.
COMPACTION_INTERVAL = int(os.getenv("COMPACTION_INTERVAL", "5"))
COMPACTION_OVERLAP = int(os.getenv("COMPACTION_OVERLAP", "1"))
