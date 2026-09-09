"""One language-independent canonical transcript, with private raw provenance."""

import asyncio

import pytest
from providers.fixtures import SOURCE, ScriptedConverter, ScriptedSpeech

from sanad.media.speech import SpeechAdapter, Transcript, normalize_transcript


@pytest.mark.parametrize("language", ["en", "ar", "ar-EG"])
@pytest.mark.parametrize(
    "spoken,written",
    [
        ("Exforge 5 over 160", "Exforge 5/160"),
        ("Exforge five over one sixty", "Exforge 5/160"),
        ("Exforge 5 on 160", "Exforge 5/160"),
        ("Exforge 5 by 160", "Exforge 5/160"),
        ("Exforge 5 slash 160", "Exforge 5/160"),
        ("إكسفورج ٥ على ١٦٠ على ١٢٫٥", "إكسفورج 5/160/12.5"),
        ("Exforge 5 over 160 over 12 point 5", "Exforge 5/160/12.5"),
        ("EF ٤٥ percent, creatinine 1 point 2", "EF 45%, creatinine 1.2"),
        ("lesion 2 by 3 cm", "lesion 2 by 3 cm"),
        ("lesion ٢ by ٣ mm", "lesion 2 by 3 mm"),
        ("Review on Monday by the clinic", "Review on Monday by the clinic"),
        ("time is over; slash the cost", "time is over; slash the cost"),
        ("insulin 10 units; timolol 2 drops", "insulin 10 units; timolol 2 drops"),
    ],
)
def test_numeric_context_and_idempotence(spoken: str, written: str, language: str) -> None:
    canonical = normalize_transcript(spoken, language)
    assert canonical == written
    if spoken == "Exforge five over one sixty":
        assert normalize_transcript("five over one sixty", language) == "5/160"
    assert normalize_transcript(canonical, language) == canonical


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize(
    "metadata,disputed", [("1.2 mg/dL, 45 percent", ()), ("1 mg/dL, 45 percent", ("1.2", "1"))]
)
def test_decimal_metadata_still_detects_disagreement_and_preserves_raw(
    language: str, metadata: str, disputed: tuple[str, ...]
) -> None:
    raw = "creatinine ١ point ٢ mg/dL, EF ٤٥ percent\nNUMBERS: " + metadata
    caller = ScriptedSpeech(raw)
    result = asyncio.run(
        SpeechAdapter(caller, ScriptedConverter(), SOURCE).transcribe(
            b"OggSsynthetic", "ogg", expected_language=language
        )
    )
    assert isinstance(result, Transcript)
    assert result.text == "creatinine 1.2 mg/dL, EF 45%"
    assert result.raw_text == raw
    assert result.numbers == ("1.2", "45")
    assert result.disputed_numbers == disputed
    assert result.numbers_line == "parsed"
    assert len(caller.calls) == 1
    restored = Transcript.model_validate_json(result.model_dump_json())
    assert restored.raw_text == raw and restored.text == result.text
    assert raw not in repr(result) and result.text not in repr(result)
