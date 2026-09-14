"""Approved, versioned consent copy, separate from bilingual message catalogs."""

import html

SHORT_TEXT = (
    "Hello, this is Sanad, the assistant Dr {doctor} uses to follow your plan after your visi"
    "t.\n"
    "\n"
    "Before we start, we need you to agree to the terms. In short:\n"
    "- Sanad reads your messages, voice notes and photos to explain your plan, remind you, an"
    "d collect follow-up for your doctor.\n"
    "- Messages pass through Telegram. Your data is stored with Amazon, whose AI reads your m"
    "essages. Google Gemini reads your voice notes and photos.\n"
    "- Sanad is not an emergency service. It does not diagnose or prescribe.\n"
    "- You can stop reminders at any time. To withdraw your consent fully, contact the clinic"
    "."
)

FULL_TEXT = (
    "What Sanad is\n"
    "Sanad is an AI assistant that Dr {doctor} uses to follow the plan made at your visit. Li"
    "nking your Telegram account needs your consent and your doctor's confirmation that the a"
    "ccount is really yours.\n"
    "\n"
    "What we process, and why\n"
    "Your messages, voice notes, photos, account details and your doctor's plan. We use them "
    "only to explain your plan, remind you, and collect follow-up for your doctor.\n"
    "\n"
    "Who handles your data\n"
    "- Telegram carries the messages between you and Sanad.\n"
    "- Amazon Web Services stores your data on servers in the United States, and Amazon's AI "
    "models read your messages.\n"
    "- Google Gemini reads your voice notes and photos.\nMessages with the bot are not end-to-"
    "end encrypted clinical communication.\n"
    "\n"
    "How often we contact you\n"
    "Routine follow-up is at most one reminder message a day, outside your quiet hours ({quie"
    "t_start} to {quiet_end}, {timezone}). Reminders at fixed times, or during your quiet hou"
    "rs, need your separate agreement.\n"
    "\n"
    "What Sanad cannot do\n"
    "Sanad can make mistakes. It does not diagnose, prescribe or change your doctor's instruc"
    "tions. It is not an emergency service and does not promise a time for your doctor to rep"
    "ly. In an emergency, go to the nearest emergency department or call 123 for an ambulance"
    ".\n"
    "\n"
    "Your choices\n"
    "You can say no now. You can pause reminders at any time. You can withdraw your consent c"
    "ompletely by contacting the clinic: {clinic_contact}. Your doctor can also remove you fr"
    "om Sanad. Withdrawing does not change your treatment.\n"
    "\n"
    "How long we keep your data\n"
    "While you are linked to your doctor, we keep your data to follow your plan. If you withd"
    "raw or your doctor removes you, Sanad stops contacting you straight away. Your messages,"
    " voice notes and photos are deleted within 30 days. The medical record your doctor accep"
    "ted is kept for as long as your doctor is required to keep medical records, then deleted"
    ". Deleted data can remain in our encrypted backups for up to 35 more days before it is g"
    "one.\n"
    "\n"
    "Your record of agreement\n"
    "We keep a record that you agreed and which version of these terms you saw."
)

INVITE_CONTACT_MISSING = "Set the clinic contact before inviting patients."
CONTACT_UNAVAILABLE = "Your clinic's details are being updated. Please try again later."
OFFER_SUPERSEDED = "These terms have been updated. Please use the buttons on the latest offer."


def render(
    doctor: str, quiet_start: str, quiet_end: str, timezone: str, clinic_contact: str
) -> tuple[str, str]:
    """Escape substitutions once without applying the message field length cap."""
    fields = {
        name: html.escape(value, quote=True).replace("{", "&#123;").replace("}", "&#125;")
        for name, value in {
            "doctor": doctor,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "timezone": timezone,
            "clinic_contact": clinic_contact,
        }.items()
    }
    return SHORT_TEXT.format(**fields), FULL_TEXT.format(**fields)
