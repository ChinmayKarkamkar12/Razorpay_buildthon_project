"""Package init — load .env once so every src.* module sees the secrets."""

from dotenv import load_dotenv

load_dotenv()
