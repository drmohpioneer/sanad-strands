"""Pure classification of credential messages for delivery and browser history."""

import re


def is_credential_message(template_id: str | None, text: str | None) -> bool:
    """Apply contract 18d's template markers and precise rendered-link guard."""
    return template_id in {
        "doctor_login_link",
        "patient_login_link",
        "admin_login_link",
        "scribe_invitation",
    } or bool(re.search(r"(?:/(?:pl|d|ad|p)/|t\.me/.*\?start=)[A-Za-z0-9_-]{43}", text or ""))
