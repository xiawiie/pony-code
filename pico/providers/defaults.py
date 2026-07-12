"""Shared provider defaults for CLI runtime and diagnostics."""

DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_PROVIDER = "deepseek"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")

MODEL_ENV_NAMES = {
    "ollama": ("PICO_OLLAMA_MODEL", "OLLAMA_MODEL"),
    "openai": ("PICO_OPENAI_MODEL", "OPENAI_MODEL"),
    "anthropic": ("PICO_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
    "deepseek": ("PICO_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
}
DEFAULT_MODELS = {
    "ollama": DEFAULT_OLLAMA_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "anthropic": DEFAULT_ANTHROPIC_MODEL,
    "deepseek": DEFAULT_DEEPSEEK_MODEL,
}
API_KEY_ENV_NAMES = {
    "openai": (
        "PICO_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "PICO_API_KEY",
    ),
    "anthropic": (
        "PICO_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "PICO_API_KEY",
    ),
    "deepseek": ("PICO_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "PICO_API_KEY"),
    "ollama": (),
}
BASE_URL_ENV_NAMES = {
    "openai": ("PICO_OPENAI_API_BASE", "OPENAI_API_BASE"),
    "anthropic": ("PICO_ANTHROPIC_API_BASE", "ANTHROPIC_API_BASE"),
    "deepseek": ("PICO_DEEPSEEK_API_BASE", "DEEPSEEK_API_BASE"),
    "ollama": ("PICO_OLLAMA_HOST",),
}
DEFAULT_BASE_URLS = {
    "ollama": DEFAULT_OLLAMA_HOST,
    "openai": DEFAULT_OPENAI_BASE_URL,
    "anthropic": DEFAULT_ANTHROPIC_BASE_URL,
    "deepseek": DEFAULT_DEEPSEEK_BASE_URL,
}
