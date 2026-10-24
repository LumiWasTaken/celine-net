"""The intercom: addressed, threaded text messages between the user, its conversations
and the app, and the one rule that delivers them into a conversation."""
from .service import Intercom, send

__all__ = ["Intercom", "send"]
