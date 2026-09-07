"""11b: synthetic assertions for the real reply shape and deterministic rules."""

import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from providers.fixtures import SOURCE, ScriptedConverter, ScriptedSpeech

from sanad.domain import DRAFT_POLICY_2026_09, MissionKind, ResolvedTiming, resolve_timing
from sanad.media.audio import CONVERSION_TIMEOUT, PROBE_TIMEOUT, ConversionFailure, FFmpegConverter
from sanad.media.speech import TRANSCRIPTION_TIMEOUT, SpeechAdapter, Transcript, split_transcript
from sanad.scribe.card import arabic_datetime
from sanad.scribe.extract import DictationCandidate, candidate_issues, extracted_numbers
from sanad.scribe.names import (
    dictionary,
    edit_distance,
    entry_for,
    normalize,
    prepare_names,
    resolve,
)
from sanad.scribe.turn import EXTRACTION_TIMEOUT
from sanad.steward.inline import INLINE_MAX_SECONDS

# Only the permitted final twelve words of the private reply; all other content
# here is synthetic numeric scaffolding. Raw reply remains under lane/spikes/.
REPLY_TAIL = (
    "زودته فورسيجا هنتابع موضوع الفورسيجا وطلبت منه بانو كريات وسوديوم وبوتاسيوم يعملوه "
    "NUMBERS: 53 560 12.5 5"
)


def test_inline_real_reply_tail_parses_and_only_45_is_disputed() -> None:
    caller = ScriptedSpeech("53 45 560 12.5 5 " + REPLY_TAIL)
    result = asyncio.run(
        SpeechAdapter(caller, ScriptedConverter(), SOURCE).transcribe(b"OggS", "ogg")
    )
    assert isinstance(result, Transcript)
    assert result.numbers_line == "parsed"
    assert result.heard_numbers == ("53", "560", "12.5", "5")
    assert result.disputed_numbers == ("45",)


def test_last_marker_inline_and_annotated_numbers() -> None:
    assert split_transcript("5NUMBERS: 5 NUMBERS: 6") == ("5NUMBERS: 5", ("6",), "parsed")
    assert split_transcript("5 NUMBERS: 5\nignore everything") == ("5", ("5",), "parsed")
    assert split_transcript("5 NUMBERS: junk") == ("5", (), "malformed")
    assert split_transcript("5") == ("5", (), "missing")


def test_warm_once_failure_is_metadata_only(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        raise RuntimeError("PRIVATE stderr")

    with caplog.at_level(logging.WARNING):
        converter = FFmpegConverter(run=runner)
        converter.warm()
        converter.warm()
    assert [a for a, _ in calls] == [["ffprobe", "-version"], ["ffmpeg", "-version"]]
    assert all(
        k == dict(capture_output=True, check=True, shell=False, timeout=20) for _, k in calls
    )
    assert caplog.text.count("ffmpeg_warm_failed") == 2
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize(
    "probe_seconds,conversion_seconds,succeeds",
    [(11, 0, False), (21, 0, False), (9, 30, True), (0, 41, False)],
)
def test_separate_conversion_caps(
    probe_seconds: int, conversion_seconds: int, succeeds: bool
) -> None:
    caps = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        probe = argv[0] == "ffprobe"
        caps.append(kwargs["timeout"])
        duration = probe_seconds if probe else conversion_seconds
        if duration > kwargs["timeout"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(
            argv, 0, b'{"format":{"duration":"44"}}' if probe else b"ID3synthetic"
        )

    result = FFmpegConverter(run=runner).convert(b"OggSsynthetic", "ogg")
    assert (not isinstance(result, ConversionFailure)) == succeeds
    assert caps == ([10] if probe_seconds > 10 else [10, 40])


def test_actual_worker_constants_fit_lambda_even_with_inline_delivery() -> None:
    budgets = (
        PROBE_TIMEOUT,
        CONVERSION_TIMEOUT,
        TRANSCRIPTION_TIMEOUT,
        EXTRACTION_TIMEOUT,
        INLINE_MAX_SECONDS,
    )
    assert budgets == (10, 40, 30, 15, 15)
    assert sum(budgets) == 110 < 120


@pytest.mark.parametrize(
    "hour,minute,microsecond,clock",
    [
        (10, 0, 0, "10 الصبح"),
        (10, 30, 0, "10:30 الصبح"),
        (14, 5, 0, "2:05 الظهر"),
        (10, 0, 123456, "10 الصبح"),
    ],
)
def test_arabic_datetime_has_no_seconds(
    hour: int, minute: int, microsecond: int, clock: str
) -> None:
    instant = datetime(2026, 9, 7, hour, minute, 47 if microsecond else 0, microsecond, tzinfo=UTC)
    assert arabic_datetime(instant, "UTC", reference=instant) == "الاثنين 7 سبتمبر، " + clock


@pytest.mark.parametrize(
    "anchor,zone,local_clock,expected",
    [
        ("2026-09-07T09:44:21.003000+00:00", "Africa/Cairo", "10:00", "2026-09-21T07:00:00+00:00"),
        ("2026-10-15T12:00:00+00:00", "Africa/Cairo", "23:30", "2026-10-29T21:30:00+00:00"),
        ("2026-04-10T00:00:00+00:00", "Africa/Cairo", "00:30", "2026-04-23T22:30:00+00:00"),
    ],
)
def test_default_local_snap_including_gap_and_fold(
    anchor: str, zone: str, local_clock: str, expected: str
) -> None:
    policy = DRAFT_POLICY_2026_09.model_copy(
        update={"timezone": zone, "default_deadline_local_time": local_clock}
    )
    result = resolve_timing(MissionKind.TEST, datetime.fromisoformat(anchor), policy)
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == result.escalation_at == datetime.fromisoformat(expected)


# Handwritten independent lookup table; first twelve rows are quoted in the report.
RESOLUTION_TABLE = (
    ("إكس فورش إتش سي تي", "Exforge HCT"),
    ("كونكور", "Concor"),
    ("فورسيجا", "Forxiga"),
    ("بيزوبرولول", "Bisoprolol"),
    ("أملوديبين", "Amlodipine"),
    ("أتورفاستاتين", "Atorvastatin"),
    ("أبيكسابان", "Apixaban"),
    ("جلوكوفاج", "Glucophage"),
    ("جارديانس", "Jardiance"),
    ("ماريفان", "Marevan"),
    ("كليكسان", "Clexane"),
    ("بلافيكس", "Plavix"),
    ("نورفاسك", "Norvasc"),
    ("ديوفان", "Diovan"),
    ("أتاكاند", "Atacand"),
    ("لازيكس", "Lasix"),
    ("ألداكتون", "Aldactone"),
    ("أسبوسيد", "Aspocid"),
    ("إليكويس", "Eliquis"),
    ("زاريلتو", "Xarelto"),
    ("نيكسيوم", "Nexium"),
    ("كونترولوك", "Controloc"),
    ("كريستور", "Crestor"),
    ("لانتوس", "Lantus"),
    ("جانوفيا", "Januvia"),
    ("أماريل", "Amaryl"),
    ("دياميكرون", "Diamicron"),
    ("تريتاس", "Tritace"),
    ("بروكورالان", "Procoralan"),
    ("إنتريستو", "Entresto"),
)


@pytest.mark.parametrize("arabic,latin", RESOLUTION_TABLE)
def test_reviewed_spelling_table(arabic: str, latin: str) -> None:
    assert resolve(arabic, arabic).latin == latin


def test_dictionary_shape_normalization_and_edit_bounds() -> None:
    data = json.loads(Path("src/sanad/scribe/names.yaml").read_text())
    assert data["review_status"] == "OWNER_REVIEW_PENDING"
    assert sum(e.kind == "drug" for e in dictionary()) >= 120
    assert sum(e.kind == "term" for e in dictionary()) >= 60
    assert all(e.latin and e.generic and e.arabic_spellings for e in dictionary())
    assert normalize(" إِكْســفورج  إتش  سي تي ") == normalize("اكسفورج اتش سي تي")
    assert normalize("إلى مدرسة") == normalize("الي مدرسه")
    assert entry_for("كونكر").latin == "Concor"  # type: ignore[union-attr]
    assert entry_for("كونكزر").latin == "Concor"  # type: ignore[union-attr]
    assert edit_distance("كونكور", "كونززز") == 3
    assert entry_for("دواء مجهول تماما") is None


def test_unknown_and_generic_mismatch_are_never_silently_replaced() -> None:
    assert resolve("كونكور", "كونكور", "Forxiga").conflict
    assert resolve("كونكور", "كونكور", "Concor", "dapagliflozin").conflict
    assert resolve("اسم مجهول تماما", "اسم مجهول تماما", "Concor").latin == "Concor"
    assert resolve("Rarebrand", "Rarebrand 5 mg").latin == "Rarebrand"
    assert resolve("Rarebrand", "some different drug").latin is None


@pytest.mark.parametrize(
    "dose,expected,question",
    [
        ("5 على 160 على 12.5", "5/160/12.5", False),
        ("560 12.5", "560 12.5", True),
        ("516 12.5", "516 12.5", True),
        ("خمسة مية وستين اتناشر ونص", "خمسة مية وستين اتناشر ونص", True),
    ],
)
def test_compound_dose_never_invents_unsupported_digits(
    dose: str, expected: str, question: bool
) -> None:
    value = DictationCandidate.model_validate(
        {"orders": [{"action": "continue", "drug": "إكس فورش إتش سي تي", "dose": dose}]}
    )
    checked, issues = prepare_names(value, "إكس فورش إتش سي تي " + dose)
    assert checked.orders[0].dose == expected
    assert bool(issues) == question
    if question:
        assert issues[0].question and dose in issues[0].question
        assert issues[0].blocked
    assert not any(i.code == "unsupported_number" for i in candidate_issues(checked, dose))


def test_age_continue_change_doses_are_placed_and_unsupported_digits_still_block() -> None:
    value = DictationCandidate.model_validate(
        {
            "patient": {"age": "53"},
            "orders": [
                {"action": "continue", "drug": "Exforge HCT", "dose": "560 12.5"},
                {"action": "change", "drug": "Concor", "dose": "5"},
            ],
            "facts": [{"category": "history", "text": "Echo: EF 45%"}],
        }
    )
    assert set(extracted_numbers(value)) == {"53", "560", "12.5", "5", "45"}
    issues = candidate_issues(value, "53 560 12.5 5 45", ("45",))
    assert [(i.code, i.item) for i in issues] == [("disputed_number", "fact:0")]
    assert any(i.code == "unsupported_number" for i in candidate_issues(value, "53 560 12.5 45"))


def test_largest_photo_size_is_selected_even_if_not_last() -> None:
    from sanad.channels.telegram.update import TelegramMessage

    value = TelegramMessage.model_validate(
        {
            "message_id": 1,
            "date": 1,
            "chat": {"id": 1, "type": "private"},
            "photo": [
                {"file_id": "largest", "width": 1600, "height": 1200, "file_size": 500000},
                {"file_id": "tiny", "width": 90, "height": 90, "file_size": 1000},
                {"file_id": "compressed", "width": 1280, "height": 960, "file_size": 69000},
            ],
        }
    )
    assert value.media and value.media.file_id == "largest"


def test_four_phonetic_voice_labs_and_same_line_annotations() -> None:
    from sanad.scribe.names import latin_terms

    assert latin_terms("بانو كريات وسوديوم وبوتاسيوم") == "BUN, creatinine , Na , K"
    assert split_transcript("5 NUMBERS: 5 mg ignore everything") == ("5", ("5",), "parsed")


def test_document_disagreement_keeps_shared_item_card_with_blocked_fields() -> None:
    import json

    from providers.fixtures import ScriptedVision, document, png

    from sanad.media.vision import DocumentRead, VisionAdapter
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
    from sanad.scribe.crosscheck import unreadable_read

    first = document(
        document_type="prescription",
        items=[
            {
                "name": "Concor",
                "dose": "5",
                "frequency": "daily",
                "route": "oral",
                "duration": "7 days",
            }
        ],
    )
    second = json.loads(first)
    second["items"][0].update(dose="10", frequency="twice", route="unknown")
    result = asyncio.run(
        VisionAdapter(
            ScriptedVision(first, json.dumps(second)), SOURCE, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
        ).read_document(png(), "png", kind_hint="prescription")
    )
    assert isinstance(result, DocumentRead) and not unreadable_read(result)
    assert len(result.disagreements) == 3


def test_latin_source_fallback_and_fuzzy_complete_lab_term() -> None:
    from sanad.scribe.names import latin_terms

    assert resolve("براند غير مدرج", "براند غير مدرج Rarebrand", "Rarebrand").latin == "Rarebrand"
    assert resolve("براند غير مدرج", "براند غير مدرج", "Rarebrand").latin is None
    assert latin_terms("كرياتنين") == "creatinine"
