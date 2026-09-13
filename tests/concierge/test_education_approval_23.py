"""Owner approval enables sourced explanations without bypassing the pending gate."""

from collections.abc import Iterator

import pytest
from domain_fixtures import NOW
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import PATIENT
from store.login_fixtures import browser_login
from store.test_browser_upload import UploadWorld, mount
from store.test_patient_browser_controls import headers
from system.test_clock_paths_20_6e import AdvancingClock
from system.test_walkthrough_20_6e import planned_world

from sanad.concierge import education
from sanad.store.memory import MemoryStore
from sanad.store.records import WebSession


def test_pending_entry_remains_synthetic_only(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = education.EducationEntry.model_validate(
        education.source_set()[0].model_dump() | {"reviewed_by": "pending owner review"}
    )
    monkeypatch.setattr(education, "source_set", lambda: (pending,))
    assert education.retrieve("hypertension", synthetic=False) == ()
    assert education.retrieve("hypertension", synthetic=True) == (pending,)


@pytest.fixture
def browser() -> Iterator[UploadWorld]:
    clock = AdvancingClock(NOW)
    w = planned_world(MemoryStore(clock=clock), clock)
    w.concierge.synthetic = False
    with w.client() as client:
        assert browser_login(client, w.login_path(PATIENT)).status_code == 303
        session = w.login.session(client.cookies["sanad_session"])
        assert session
        yield mount(w, client, WebSession.model_validate(session.model_dump()))


@pytest.mark.parametrize("channel", ["telegram", "browser"])
def test_approved_source_reaches_patient_in_contest_english(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    question = "What is hypertension?"
    entries = education.retrieve(question, synthetic=False)
    assert tuple(e.id for e in entries) == ("hypertension",)
    entry = entries[0]
    assert entry.reviewed_by == "Clinical reviewer of record"
    # Two unchanged sentences, selected by a scripted provider from the real source.
    expected = (
        "General information: Blood pressure describes the force of blood against vessel walls"
        " (Source: World Health Organization: High blood pressure)\n"
        "General information: The two readings reflect the heartbeat and the rest between beats"
        " (Source: World Health Organization: High blood pressure)"
    )
    value = {"reply": expected, "kind": "education", "needs_doctor": False}
    w = browser.world
    from store.medication_fixtures import scripted_barrier_factory

    w.concierge.barrier_model_factory = scripted_barrier_factory
    assert w.concierge.synthetic is False
    if channel == "telegram":
        model, reply = w.send(question, value)
        assert reply.template_id == "patient_answer" and reply.payload
        text = str(reply.payload["text"])
    else:
        model = ScriptedModel(candidate(value))
        w.concierge.model_factory = lambda registry, role: model
        response = browser.client.post(
            "/api/patient/messages",
            headers=headers(browser),
            json={"text": question, "command_id": "education23"},
        )
        assert response.status_code == 200 and response.json()["status"] == "accepted"
        conversation = browser.client.get("/api/patient/conversation")
        assert conversation.status_code == 200
        answers = [
            item["text"]
            for item in conversation.json()["items"]
            if item.get("text", "").startswith("General information: ")
        ]
        assert len(answers) == 1
        text = answers[0]
    assert len(model.script.calls) == 1
    assert text == expected
    assert all(
        line.startswith("General information: ")
        and line.endswith(f"(Source: {entry.source_label_en})")
        for line in text.splitlines()
    )
    assert not any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))


@pytest.mark.parametrize(
    "entry_id,field,removed,remaining",
    [
        (
            "photo",
            "text_en",
            "NHS guidance supports image quality; document and bot instructions are Sanad’s own.",
            "A clear photograph needs good lighting, a steady device and an uncluttered "
            "background. "
            "Sanad’s document instructions are to show the whole page from above, with names, "
            "numbers, units and dates readable and without glare. Separate pages reduce row "
            "confusion. Use your bound private bot conversation. Saving an image does not "
            "interpret a result or activate a prescription.",
        ),
        (
            "photo",
            "text_ar",
            "صفحة NHS خلفية عامة لجودة الصور، وتعليمات الورق والبوت هنا من سند.",
            "الصورة الواضحة محتاجة نور كويس وإيد ثابتة وخلفية من غير حاجات كتير تشتت النظر. "
            "إرشادات سند للورق: الصفحة كلها تبقى ظاهرة من فوق، والأسماء والأرقام والوحدات "
            "والتاريخ يبقوا مقروءين، من غير لمعان مغطي الكتابة. كل ورقة لوحدها بتقلل لخبطة "
            "الصفوف. الصورة بتتبعت في محادثتك الخاصة مع البوت بعد الربط والموافقة. حفظ "
            "الصورة مش معناه إن التحليل اتفسر أو الروشتة بقت خطة جديدة.",
        ),
        (
            "qr",
            "text_en",
            "The NHS page provides background on QR-based sign-in; these invitation rules are "
            "Sanad’s own specification.",
            "A Sanad invitation code opens a service-linking flow and contains no patient name or "
            "treatment details. Opening it shows the service, doctor and consent instructions "
            "rather than the chart. After consent and a claim, the doctor confirms the intended "
            "person before plan access. Invitations expire and cannot transfer a chart.",
        ),
        (
            "qr",
            "text_ar",
            "صفحة NHS بتشرح مثال عام لفتح كود في الدخول، لكن قواعد الدعوة دي من تصميم سند نفسه.",
            "كود الدعوة في سند وسيلة تفتح بيها رابط الربط بسند. الكود نفسه مفيهوش اسم المريض "
            "أو تفاصيل علاجه. فتح الرابط بيعرض تعريف سند والدكتور وتعليمات الموافقة، مش الملف "
            "الطبي. بعد طلب الربط والموافقة، الدكتور بيأكد إن الحساب للشخص المقصود قبل إتاحة "
            "الخطة. دعوة سند مؤقتة وصلاحيتها محدد، ومش وسيلة لنقل الملف لشخص تاني.",
        ),
    ],
)
def test_product_help_preserves_every_remaining_character(
    entry_id: str, field: str, removed: str, remaining: str
) -> None:
    entry = next(e for e in education.source_set() if e.id == entry_id)
    text = getattr(entry, field)
    assert removed not in text
    assert text.encode("utf-8") == remaining.encode("utf-8")
    assert entry.version == "education-v1-2026-09-13"


def test_product_help_word_counts_under_owner_option_a() -> None:
    products = {e.id: e for e in education.source_set() if e.content_kind == "product"}
    assert {key: len(e.text_ar.split()) for key, e in products.items()} == {"photo": 62, "qr": 53}
    assert all(len(e.text_ar.split()) <= 200 for e in products.values())
