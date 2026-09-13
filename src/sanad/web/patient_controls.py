"""Server-rendered accessible controls in the existing patient shell."""

import json
from html import escape

from sanad.presentation.patient_browser import CATALOG


def controls(locale: str) -> str:
    words = {
        key.removeprefix("patient_browser."): values["ar" if locale == "ar" else "en"]
        for key, values in CATALOG.items()
    }

    def w(key: str) -> str:
        return escape(words[key])

    return f'''<div id="patient-controls" data-words="{escape(json.dumps(words), quote=True)}">
<section class="section">
<h2>{w("conversation")}</h2>
<button id="patient-older" type="button" hidden>{w("older")}</button>
<div id="patient-conversation" aria-live="polite">
</div>
<form id="patient-message-form">
<label>{w("message")}<textarea id="patient-message" maxlength="4096" required>
</textarea>
</label>
<button type="submit">{w("send")}</button>
</form>
</section>
<section class="section">
<h2>{w("upload")}</h2>
<p>{w("limits")}</p>
<form id="patient-upload-form">
<label>{w("file")}<input id="patient-file" type="file" accept="image/*" required>
</label>
<label>{w("caption")}<textarea id="patient-caption" maxlength="4096">
</textarea>
</label>
<button type="submit">{w("upload")}</button>
<progress id="patient-upload-progress" max="100" value="0" hidden>
</progress>
</form>
<div id="patient-uploads">
</div>
</section>
<section class="section">
<h2>{w("preferences")}</h2>
<p id="patient-preferences">
</p>
<p id="patient-reminder-explanation">{w("reminder_explanation")}</p>
<button id="patient-stop" type="button">{w("stop")}</button>
<button id="patient-confirm" type="button" hidden>{w("confirm")}</button>
<form id="patient-quiet-form">
<label>{w("quiet_start")}<input id="patient-quiet-start" type="time" required>
</label>
<label>{w("quiet_end")}<input id="patient-quiet-end" type="time" required>
</label>
<button type="submit">{w("save")}</button>
</form>
</section>
<p id="patient-action-result" role="status">
</p>
</div>'''
