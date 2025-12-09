# config.py
import json
import os

THEME_FILE = "theme.json"

# Users allowed to use admin-level commands like /theme_set, /theme_custom, /reset, etc.
ALLOWED_USERS = [
    351396116480393220,  # You
    690712359060111430,
    921890814618108004,
    582419507637649429
]

# Default theme
DEFAULT_THEME = {
    "mode": "normal",        # normal, christmas, dark, neon, pastel, winter, custom
    "custom_color": "#ffffff"
}


def load_theme_data():
    """Load theme configuration from theme.json, or create it with defaults."""
    if not os.path.exists(THEME_FILE):
        save_theme_data(DEFAULT_THEME)
        return DEFAULT_THEME

    with open(THEME_FILE, "r") as f:
        return json.load(f)


def save_theme_data(data: dict):
    """Save theme configuration to theme.json."""
    with open(THEME_FILE, "w") as f:
        json.dump(data, f, indent=4)


# Global cache (used on startup if needed)
THEME_DATA = load_theme_data()
