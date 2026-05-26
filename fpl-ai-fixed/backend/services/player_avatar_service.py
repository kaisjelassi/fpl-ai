"""Stub avatar service."""
DEFAULT_PLAYER_PHOTO = "/assets/player_images/default.png"


def generate_player_avatar(player_id=None, **kwargs) -> str:
    return DEFAULT_PLAYER_PHOTO


def generate_player_avatar_record(player_id=None, **kwargs) -> dict:
    return {"url": DEFAULT_PLAYER_PHOTO}
