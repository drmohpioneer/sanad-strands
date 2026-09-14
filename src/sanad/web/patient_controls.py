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
<section class="section patient-talk">
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
<section class="section patient-upload">
<h2>{w("upload")}</h2>
<div class="drop">
<span class="ic" aria-hidden="true">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9">
<path d="M12 17V5M7 10l5-5 5 5M5 19h14"/></svg></span>
<p>{w("limits")}</p>
<form id="patient-upload-form">
<label>{w("file")}<input id="patient-file" type="file" accept="image/*,application/pdf" required>
</label>
<label>{w("caption")}<textarea id="patient-caption" maxlength="4096">
</textarea>
</label>
<button type="submit">{w("upload")}</button>
<progress id="patient-upload-progress" max="100" value="0" hidden>
</progress>
</form>
</div>
<div id="patient-uploads">
</div>
</section>
<section class="section remind" id="patient-settings" role="tabpanel"
 aria-labelledby="patient-tab-settings">
<h2>{w("preferences")}</h2>
<p id="patient-preferences">
</p>
<p id="patient-reminder-explanation">{w("reminder_explanation")}</p>
<button id="patient-stop" type="button">{w("stop")}</button>
<button id="patient-confirm" class="primary" type="button" hidden>{w("confirm")}</button>
<form id="patient-quiet-form">
<label>{w("quiet_start")}<input id="patient-quiet-start" type="time" class="clock" required>
</label>
<label>{w("quiet_end")}<input id="patient-quiet-end" type="time" class="clock" required>
</label>
<button type="submit">{w("save")}</button>
</form>
</section>
<p id="patient-action-result" role="status">
</p>
</div>'''
