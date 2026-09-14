"""Public documentation, content hygiene and executable setup checks."""

import asyncio
import hashlib
import html
import re
import tomllib
import unicodedata
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import httpx
import pytest

from sanad.models.registry import ModelRegistry, ModelRole
from sanad.store.memory import MemoryStore


def characters(*points: int) -> str:
    return "".join(map(chr, points))


ROOT = Path(__file__).resolve().parents[2]
REFERENCE_LABEL = characters(106, 117, 100, 103, 101)
REFERENCE_PATH = f"docs/{REFERENCE_LABEL}-instructions.md"
DOCUMENTS = (
    "README.md",
    "LICENSE",
    REFERENCE_PATH,
    *(
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / "docs/diagrams").rglob("*"))
        if p.is_file()
    ),
    "tests/docs/test_package.py",
)
WORD_DIGESTS = frozenset(
    {
        "c7ce66d0fb14e3c2d4d920918cc6cc7e488668b08dddeb7b85e44de3b9f3eb01",
        "1ac7d61d1d29fc2e9a0b5474d1ba4b662022ad9618c6bcd9b24b977679cf686f",
        "11e221bd1b7fc99544f3f765b0c39dd2786e636c3422d6bec026d1ed03884cac",
        "70dd27e3d4349e0fad4c0356ffbc9b9683def260678092d8bf117180f608c523",
        "4c1029697ee358715d3a14a2add817c4b01651440de808371f78165ac90dc581",
        "6f0e3a8c0eb46e8834b43b03374ece43a030621d92a7437beb48f871e90f8d90",
        "01b8016fdce455c4343951d02b110bd9c02ce8456f43f8893c8b4ccbb1ca54aa",
        "f2e40fc1edb72ee9ed58fd7076934ed03eefc0116b9d90b32c0bcf381b2e788f",
        "08359ecc3ed93f8ee9b21b50f1d9a51979b18d0298336649221fa72f972cb73e",
        "57de4cf40144bdf7d00010f2f5557a7d642c2b9705309bfade167dd313e2ca93",
        "c857d09db23e6822e3600bc06ad8d58f92ed62bc8efd81c753f77048662cb97d",
        "09cf980b5ff304ac11b7f6d2c5c263da2a867425798ef5cc5d2ebcf55c4fcd23",
        "e0837458ff79b45618210c16c558b7ce42451bbfd211f0ebdf99b8f3741530a5",
        "c8c16f2f73a560a57c36920a9f8bb6691724aebd78199f8bc17b86f7c66d9da9",
        "d8e4511aed5fe76005ac8d5e370d3683456f0bc7c01109926eadc102d1ce24df",
        "8e562308c89d24d53d36cef6187803142ba69048f578b19806a587ae35efc6c2",
        "ff83bd0d393b0320155673a1c776fd93fee78ad424eb921055d36175a979fe78",
    }
)
IDENTIFIER_DIGESTS = frozenset(
    {
        (8, "863498dfa3f35f634f532b3cab89b0f8b6baaf9a2a94722c52ca2ef44377efc2"),
        (7, "004687553429516cefacdedeaf3a24ce9324d0eb0739c192e21d3ecb883aef5b"),
        (5, "9bf774db17deb96a38e97dce48d6e1c479f263b28c413fe7288e930b5344841f"),
        (12, "a2426c7254045a99d01d88ae5c28d909a7dddf903d4ef1899b612990d7cf9348"),
        (9, "6d74b6e7bffd3107f77136248cb299e003ef3e3456793aab31270a1a6e47bca7"),
    }
)
INTRO_DIGESTS = frozenset(
    {
        "a56f3dbc3053cc78282ebb4360025187945043453f94099157496b76530a404d",
        "10e86c6514d40f2a3e861b31847340ee8c8ed181029a17b042f137121d28863e",
    }
)
PRIVATE = re.compile(r"\bcontract\s*(?:#\s*)?\d+|lane[/\\]|fix[-_ ]up", re.IGNORECASE)
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE = re.compile(
    r"(?<![\w.])(?:\+?20[ .-]?)?01[0125](?:[ .-]?\d){8}(?![\w.])"
    r"|(?<!\w)\+\d{1,3}(?:[ ()-]*\d){8,14}(?!\w)"
    r"|(?<![\w.])\d{10,15}(?![\w.])"
)
LOOKALIKES = str.maketrans(
    {
        "\u0430": "a",
        "\u03b1": "a",
        "\u0441": "c",
        "\u03f2": "c",
        "\u03c2": "c",
        "\u03c3": "c",
        "\u0435": "e",
        "\u03b5": "e",
        "\u043d": "h",
        "\u03b7": "h",
        "\u0456": "i",
        "\u03b9": "i",
        "\u043a": "k",
        "\u03ba": "k",
        "\u043c": "m",
        "\u03bc": "m",
        "\u043e": "o",
        "\u03bf": "o",
        "\u0440": "p",
        "\u03c1": "p",
        "\u0442": "t",
        "\u03c4": "t",
        "\u0445": "x",
        "\u03c7": "x",
        "\u0443": "y",
        "\u03c5": "y",
        "\u0623": "\u0627",
        "\u0625": "\u0627",
        "\u0622": "\u0627",
        "\u0649": "\u064a",
        "\u0640": None,
    }
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(text))
    # Preserve case until word boundaries have been identified.
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def tokens(text: str) -> set[str]:
    parts = []
    for index, char in enumerate(text):
        if (
            index
            and char.isupper()
            and (
                text[index - 1].islower()
                or (text[index - 1].isupper() and text[index + 1 : index + 2].islower())
            )
        ):
            parts.append(" ")
        parts.append(char)
    words = {word.casefold() for word in re.findall(r"[^\W_]+", text)}
    words.update(word.casefold() for word in re.findall(r"[^\W_]+", "".join(parts)))
    for chunk in re.findall(r"[^\W_]+(?:[-.][^\W_]+)+", text):
        words.add("".join(char for char in chunk if char.isalpha()).casefold())
    return words


def compact_identifier(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(char for char in text if not unicodedata.category(char).startswith("M"))
    return "".join(char for char in text.translate(LOOKALIKES) if char.isalnum())


def violations(
    text: str,
    relative: str = "",
    *,
    word_digests: frozenset[str] = WORD_DIGESTS,
    identifier_digests: frozenset[tuple[int, str]] = IDENTIFIER_DIGESTS,
) -> set[str]:
    prepared = normalize(text)
    folded = prepared.casefold()
    found = {
        label
        for label, pattern in (("private wording", PRIVATE), ("email", EMAIL), ("phone", PHONE))
        if pattern.search(folded)
    }
    if any(digest(word) in word_digests for word in tokens(prepared)):
        found.add("word")
    compact = compact_identifier(prepared)
    for length in {length for length, _ in identifier_digests}:
        if any(
            (length, digest(compact[index : index + length])) in identifier_digests
            for index in range(len(compact) - length + 1)
        ):
            found.add("identifier")
            break
    if any(char in prepared for char in ("\u2014", "\u2013")):
        found.add("punctuation")
    # Remove only the exact supplied path before decoding the introduction.
    if relative == "README.md" and any(
        digest(word) in INTRO_DIGESTS
        for word in tokens(normalize(text.replace(REFERENCE_PATH, "")))
    ):
        found.add("introduction")
    return found


@pytest.mark.parametrize("relative", DOCUMENTS)
def test_public_files_exist_and_exclude_private_material(relative: str) -> None:
    path = ROOT / relative
    assert path.is_file(), relative
    body = path.read_text(encoding="utf-8")
    assert body.strip(), relative
    assert not violations(body, relative), (relative, sorted(violations(body, relative)))


def test_digest_set_sizes() -> None:
    assert len(WORD_DIGESTS) == 17
    assert len(IDENTIFIER_DIGESTS) == 5
    assert len(INTRO_DIGESTS) == 2


@pytest.mark.parametrize(
    "points",
    [
        (97, 116, 116, 101, 109, 112, 116),
        (97, 116, 116, 101, 109, 112, 116, 115),
        (118, 101, 114, 100, 105, 99, 116),
        (118, 101, 114, 100, 105, 99, 116, 115),
        (111, 119, 110, 101, 114),
        (111, 119, 110, 101, 114, 115),
        (111, 119, 110, 101, 114, 115, 104, 105, 112),
        (97, 114, 99, 104, 105, 116, 101, 99, 116),
        (97, 114, 99, 104, 105, 116, 101, 99, 116, 115),
        (99, 111, 100, 101, 120),
        (99, 108, 97, 117, 100, 101),
        (102, 97, 98, 108, 101),
        (119, 97, 108, 107, 116, 104, 114, 111, 117, 103, 104),
        (119, 97, 108, 107, 116, 104, 114, 111, 117, 103, 104, 115),
        (97, 100, 100, 101, 110, 100, 117, 109),
        (97, 100, 100, 101, 110, 100, 97),
        (102, 105, 120, 117, 112),
        (109, 111, 104, 97, 109, 109, 101, 100),
        (109, 117, 115, 116, 97, 102, 97),
        (100, 114, 109, 111, 104),
        (100, 114, 109, 111, 104, 112, 105, 111, 110, 101, 101, 114),
        (106, 117, 115, 116, 100, 114, 109, 111, 104),
    ],
)
def test_configured_entries_are_detected(points: tuple[int, ...]) -> None:
    assert violations(characters(*points))


@pytest.mark.parametrize(
    "points",
    [
        (111, 119, 110, 101, 114, 82, 101, 118, 105, 101, 119),
        (79, 87, 78, 69, 82, 95, 82, 69, 86, 73, 69, 87, 95, 80, 69, 78, 68, 73, 78, 71),
        (99, 111, 45, 100, 101, 120),
        (99, 111, 46, 100, 101, 120),
        (99, 111, 8203, 100, 101, 120),
        (100, 114, 109, 111, 104, 46, 112, 105, 111, 110, 101, 101, 114),
        (106, 117, 115, 116, 46, 100, 114, 46, 109, 111, 104),
        (38, 35, 54, 55, 59, 111, 100, 101, 120),
    ],
)
def test_encoded_private_material_is_detected(points: tuple[int, ...]) -> None:
    assert violations(characters(*points))


@pytest.mark.parametrize(
    "style",
    [
        "plain",
        "upper",
        "mixed",
        "title",
        "format",
        "camel",
        "acronym",
        "underscore",
        "hyphen",
        "dot",
        "entity",
        "width",
    ],
)
def test_synthetic_word_registration(style: str) -> None:
    seed = "quartz"
    samples = {
        "plain": seed,
        "upper": seed.upper(),
        "mixed": "".join(char.upper() if index % 2 else char for index, char in enumerate(seed)),
        "title": seed.title(),
        "format": seed[:2] + "\u200b" + seed[2:],
        "camel": seed + "Review",
        "acronym": seed.upper() + "Review",
        "underscore": seed.upper() + "_REVIEW_PENDING",
        "hyphen": seed[:2] + "-" + seed[2:],
        "dot": seed[:2] + "." + seed[2:],
        "entity": "".join(f"&#{ord(char)};" for char in seed),
        "width": "".join(chr(ord(char) + 0xFEE0) for char in seed),
    }
    registered = WORD_DIGESTS | {digest(seed)}
    assert len(registered) == 18
    assert "word" in violations(samples[style], word_digests=registered)
    assert "word" not in violations(samples[style])
    assert "word" not in violations(seed + "ite", word_digests=registered)
    assert "word" not in violations("pre" + seed, word_digests=registered)


@pytest.mark.parametrize(
    "style",
    [
        "plain",
        "upper",
        "format",
        "joined",
        "dotted",
        "accent",
        "cyrillic",
        "greek",
        "entity",
        "width",
    ],
)
def test_synthetic_identifier_registration(style: str) -> None:
    seed = "citrine"
    samples = {
        "plain": seed,
        "upper": seed.upper(),
        "format": seed[:2] + "\u200d" + seed[2:],
        "joined": "prefix" + seed + "suffix",
        "dotted": ".".join(seed),
        "accent": "c\u00edtrine",
        "cyrillic": "\u0441itrin\u0435",
        "greek": "c\u03b9\u03c4rine",
        "entity": "".join(f"&#x{ord(char):x};" for char in seed),
        "width": "".join(chr(ord(char) + 0xFEE0) for char in seed),
    }
    registered = IDENTIFIER_DIGESTS | {(len(seed), digest(seed))}
    assert len(registered) == 6
    assert "identifier" in violations(samples[style], identifier_digests=registered)
    assert "identifier" not in violations(samples[style])


def test_script_normalization() -> None:
    assert compact_identifier("\u0623\u0625\u0622\u0649\u0640\u064e") == (
        "\u0627\u0627\u0627\u064a"
    )
    expected = "acehikmoptxy"
    for script in (
        "\u0430\u0441\u0435\u043d\u0456\u043a\u043c\u043e\u0440\u0442\u0445\u0443",
        "\u03b1\u03f2\u03b5\u03b7\u03b9\u03ba\u03bc\u03bf\u03c1\u03c4\u03c7\u03c5",
    ):
        assert compact_identifier(normalize(script)) == expected
        assert compact_identifier(normalize(script.upper())) == expected


@pytest.mark.parametrize(
    "points",
    [
        (99, 111, 110, 116, 114, 97, 99, 116, 32, 50, 50),
        (99, 111, 110, 116, 114, 97, 99, 116, 32, 35, 50, 50),
        (108, 97, 110, 101, 47, 112, 114, 105, 118, 97, 116, 101, 46, 109, 100),
        (108, 97, 110, 101, 92, 112, 114, 105, 118, 97, 116, 101, 46, 109, 100),
        (102, 105, 120, 45, 117, 112),
        (102, 105, 120, 95, 117, 112),
        (102, 105, 120, 32, 117, 112),
        (
            115,
            97,
            109,
            112,
            108,
            101,
            64,
            101,
            120,
            97,
            109,
            112,
            108,
            101,
            46,
            105,
            110,
            118,
            97,
            108,
            105,
            100,
        ),
        (43, 50, 48, 32, 49, 48, 48, 32, 48, 48, 48, 32, 48, 48, 48, 48),
        (48, 49, 48, 48, 48, 48, 48, 48, 48, 48, 48),
        (43, 49, 32, 40, 50, 48, 50, 41, 32, 53, 53, 53, 45, 48, 49, 48, 48),
    ],
)
@pytest.mark.parametrize("style", ["plain", "upper", "format", "entity", "width"])
def test_pattern_checks_follow_normalization(points: tuple[int, ...], style: str) -> None:
    seed = characters(*points)
    samples = {
        "plain": seed,
        "upper": seed.upper(),
        "format": "\u200b".join(seed),
        "entity": "".join(f"&#{ord(char)};" for char in seed),
        "width": "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in seed),
    }
    assert violations(samples[style])


@pytest.mark.parametrize("mark", ["\u2014", "\u2013"])
def test_punctuation_is_refused(mark: str) -> None:
    assert "punctuation" in violations(mark)
    assert "punctuation" in violations(f"&#{ord(mark)};")


@pytest.mark.parametrize(
    "points", [(104, 97, 99, 107, 97, 116, 104, 111, 110), (106, 117, 100, 103, 101)]
)
def test_introduction_terms_are_scoped(points: tuple[int, ...]) -> None:
    text = characters(*points)
    for sample in (text, text.upper(), "\u200b".join(text), text + "Review"):
        assert "introduction" in violations(sample, "README.md")
        for relative in DOCUMENTS:
            if relative != "README.md":
                assert not violations(sample, relative)
    assert not violations(f"[testing instructions]({REFERENCE_PATH})", "README.md")
    assert "introduction" in violations(REFERENCE_PATH + " " + text, "README.md")
    assert "introduction" in violations(REFERENCE_PATH.replace("u", "&#117;"), "README.md")


@pytest.mark.parametrize("text", ["architecture", "architectural", "reconstruction", "MIT"])
def test_ordinary_public_words_are_allowed(text: str) -> None:
    assert not violations(text, "README.md")


def test_readme_inventory_covers_all_direct_libraries_models_and_fonts() -> None:
    body = (ROOT / "README.md").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {package["name"]: package["version"] for package in lock["package"]}
    requirements = (
        project["project"]["dependencies"]
        + project["dependency-groups"]["dev"]
        + project["build-system"]["requires"]
    )
    for requirement in set(requirements):
        name, version = requirement.split("==")
        assert locked[name] == version
        assert f"| {name} | {version} |" in body
    registry = ModelRegistry()
    roles: tuple[ModelRole, ...] = ("worker", "classifier", "cross_check", "vision", "speech")
    for model in {
        *(registry.model_id(role) for role in roles),
        *(model.model_id for model in registry.rejected),
    }:
        assert f"| `{model}` |" in body
    for notice in (ROOT / "src/sanad/web/static").glob("*OFL*"):
        assert notice.relative_to(ROOT).as_posix() in body
    assert all(term in body for term in ("GPLv2", "LGPLv3", "MPL-2.0", "MIT"))
    assert "LGPL-3.0-or-later" in body


def test_readme_local_setup_serves_offline_demo() -> None:
    body = (ROOT / "README.md").read_text()
    snippet = body.split("<<'PYTHON'\n", 1)[1].split("\nPYTHON", 1)[0]
    # Exercise exactly the documented composition without starting a socket listener.
    with patch("uvicorn.run") as server:
        exec(compile(snippet, "README.md", "exec"), {})
    server.assert_called_once()
    app = server.call_args.args[0]
    assert isinstance(app.state.telegram.store, MemoryStore)

    async def check_pages() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            for route in ("/demo", "/demo/patient", "/demo/admin"):
                response = await client.get(route)
                assert response.status_code == 200
                assert "Fictional records only" in response.text
            for asset in ("demo.json", "demo-patient.json", "demo-admin.json", "browser.js"):
                assert (await client.get(f"/assets/{asset}")).status_code == 200
            assert (await client.get("/a")).status_code in {401, 403}
            assert (await client.post("/tg", json={})).status_code == 401

    asyncio.run(check_pages())
    assert not app.state.telegram.transport.calls


def test_diagram_is_rendered_and_every_node_has_a_real_source() -> None:
    source = (ROOT / "docs/diagrams/architecture.mmd").read_text()
    svg = ElementTree.parse(ROOT / "docs/diagrams/architecture.svg").getroot()
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    assert "Sanad architecture and trust boundaries" in " ".join(svg.itertext())
    assert svg.findall(".//{http://www.w3.org/2000/svg}path")
    nodes = re.findall(r"^\s*(?:subgraph )?([A-Z]+)\[", source, flags=re.MULTILINE)
    assert len(nodes) >= 18
    for node in nodes:
        provenance = re.search(rf"^\s*%% {node}: (.+)$", source, flags=re.MULTILINE)
        assert provenance is not None, node
        paths = re.findall(r"(?:src|deploy)/[\w/.-]+", provenance[1])
        assert paths and all((ROOT / path).is_file() for path in paths), node
    renderer = (ROOT / "docs/diagrams/render.sh").read_text()
    assert "npx --yes @mermaid-js/mermaid-cli@11.12.0" in renderer
    assert "PUPPETEER_EXECUTABLE_PATH" in renderer
    assert "PUPPETEER_SKIP_DOWNLOAD=true" in renderer


def test_testing_instructions_name_the_hosted_environment_and_timing() -> None:
    body = (ROOT / REFERENCE_PATH).read_text()
    assert "https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws" in body
    assert "_BOT_USERNAME>" not in body and "_BASE_URL>" not in body
    assert "not deployed yet" not in body
    # The access link is shared privately; the public file never carries a code.
    assert "?start=" not in body
    for label in ("Minute tick", "Digest wait", "Compressed time"):
        assert label in body
    sections = (
        "## 1. Telegram as the doctor",
        "## 2. Telegram as the patient",
        "## 3. Doctor dashboard",
        "## 4. Patient page",
        "## 5. Voice and photo availability",
    )
    assert [body.index(section) for section in sections] == sorted(
        body.index(section) for section in sections
    )
