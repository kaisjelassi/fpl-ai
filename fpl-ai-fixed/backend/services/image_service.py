"""Stub image service — returns safe defaults for all image helpers."""
from pathlib import Path

DEFAULT_PLAYER_PHOTO = "/assets/player_images/default.png"
_ASSETS = Path(__file__).resolve().parent.parent.parent / "assets"


def local_player_image_path(player_id) -> str:
    pid = int(player_id or 0)
    path = _ASSETS / "player_images" / f"{pid}.png"
    if path.exists():
        return f"/assets/player_images/{pid}.png"
    return DEFAULT_PLAYER_PHOTO


def is_local_player_image_source(url: str) -> bool:
    return str(url or "").startswith("/assets/player_images/")


def is_blocked_image_source(url: str) -> bool:
    return False


def is_low_quality_image_source(url: str) -> bool:
    return "placeholder" in str(url or "")


def is_default_player_photo(url: str) -> bool:
    return str(url or "") == DEFAULT_PLAYER_PHOTO


def is_generated_avatar_source(url: str) -> bool:
    return False


def is_official_fpl_cdn_url(url: str) -> bool:
    return "resources.premierleague.com" in str(url or "")


def extract_fpl_photo_id(url: str) -> str:
    return ""


def get_official_fpl_image_url(photo_id: str) -> str:
    return DEFAULT_PLAYER_PHOTO


def image_dimensions(path: str):
    return (0, 0)


def image_extension_from_content_type(ct: str) -> str:
    return ".png"


def fetch_remote_image_bytes(url: str):
    return None
