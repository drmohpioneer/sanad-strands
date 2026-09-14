"""Measured reuse inventory.

Unknown is never normal; a missing protocol is not a safe result.
Screening covers readable text and captions only, never hidden content of
unprocessed media; callers own the media_failure route. A verdict is not a
diagnosis. Original adult cardiology cohort; Sanad v2 re-approval pending.
"""

from datetime import date

from sanad.domain.boundaries import NonblankStr, _BoundaryValue


class ReusedModule(_BoundaryValue):
    destination: NonblankStr
    source_repository: NonblankStr
    source_commit: NonblankStr
    source_path: NonblankStr
    copy_date: date
    modifications: tuple[NonblankStr, ...]
    source_sha256: NonblankStr


REUSED_MODULES: tuple[ReusedModule, ...] = (
    ReusedModule(
        destination="src/sanad/safety/normalize.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/core/sentinel.py",
        copy_date=date(2026, 9, 6),
        modifications=("Extract the normalizer without changing its implementation or tables.",),
        source_sha256="0ab24cf0c0d463222ab14dcce8e460fd5ea7f2a714a9827238c26cd86a1ae958",
    ),
    ReusedModule(
        destination="src/sanad/safety/sentinel.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/core/sentinel.py",
        copy_date=date(2026, 9, 6),
        modifications=(
            "Import the extracted normalizer.",
            "Replace provider access with an unavailable compatibility hook; "
            "remove bounded coupling.",
            "Add provenance and boundary documentation.",
        ),
        source_sha256="0ab24cf0c0d463222ab14dcce8e460fd5ea7f2a714a9827238c26cd86a1ae958",
    ),
    ReusedModule(
        destination="src/sanad/safety/labs.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/core/labs.py",
        copy_date=date(2026, 9, 6),
        modifications=(
            "Use sanad.safety imports.",
            "Add provenance and boundary documentation; no timing helper exists in this source.",
        ),
        source_sha256="14495286b954491da1eccd2e15c95dec6891b4b037a7591270fe7079e465dc1d",
    ),
    ReusedModule(
        destination="src/sanad/safety/vitals.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/core/vitals.py",
        copy_date=date(2026, 9, 6),
        modifications=("Add provenance and boundary documentation.",),
        source_sha256="4da6b037540cda74de99870c7fca53e185d8abb5cf4294dddee19fb4c20be15d",
    ),
    ReusedModule(
        destination="src/sanad/safety/validator.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/core/validator.py",
        copy_date=date(2026, 9, 6),
        modifications=(
            "Import the extracted normalizer.",
            "Replace provider access with an unavailable compatibility hook; "
            "remove bounded coupling.",
            "Add provenance and boundary documentation.",
        ),
        source_sha256="6dabda324587755cf3a681ed6ad4b08df31e3913697abb1c7f0e022e0e98f162",
    ),
    ReusedModule(
        destination="src/sanad/safety/templates.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/core/templates.py",
        copy_date=date(2026, 9, 6),
        modifications=(
            "Add provenance and boundary documentation.",
            "Add deterministic urgent templates and import-time field checks.",
        ),
        source_sha256="a419baff9a89d0f43202a138ffccc3c6575fe19109ba13bad8bea185e35e3584",
    ),
    ReusedModule(
        destination="tests/safety/test_sentinel.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/tests/test_sentinel.py",
        copy_date=date(2026, 9, 6),
        modifications=("Adapt imports only: core -> sanad.safety.",),
        source_sha256="d770cbd9cc9410caa6e6bb8e08fe827ba7ff38af010ec70b121f454772360ef1",
    ),
    ReusedModule(
        destination="tests/safety/test_labs.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/tests/test_labs.py",
        copy_date=date(2026, 9, 6),
        modifications=("Adapt imports only: core -> sanad.safety.",),
        source_sha256="1d63c83d8fdb571cb23c8245312a92b349036b7ffa301d808f2c08de09a6420c",
    ),
    ReusedModule(
        destination="tests/safety/test_vitals.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/tests/test_vitals.py",
        copy_date=date(2026, 9, 6),
        modifications=("Adapt imports only: core -> sanad.safety.",),
        source_sha256="21ad97e9112c073d00662fe7862df481e61bd17fde277dc2bc9c93df36d9b79c",
    ),
    ReusedModule(
        destination="tests/safety/test_validator.py",
        source_repository="../sanad (frozen Google Sanad)",
        source_commit="b65f569",
        source_path="app/tests/test_validator.py",
        copy_date=date(2026, 9, 6),
        modifications=("Adapt imports only: core -> sanad.safety.",),
        source_sha256="d73649268c7e0f850c70b26b802b7e7cc6520dc4b0517c735093483bbe77aa6e",
    ),
)


# Reviewer wording removed from the copied tests for publication. Each entry is the
# published line and the original line as base64; the provenance test restores the
# original before comparing the recorded source hash.
SCRUBBED_TEST_LINES: dict[str, tuple[tuple[str, str], ...]] = {
    "tests/safety/test_labs.py": (
        (
            "    an adversarial review finding: a check that was not done, reported as done.",
            "ICAgIENvZGV4IGl0ZW0gMzogYSBjaGVjayB0aGF0IHdhcyBub3QgZG9uZSwgcmVwb3J0ZWQgYXMgZG9uZS4=",
        ),
        (
            '    """Public adversarial regression, S11 wave A item 2:',
            "ICAgICIiIlB1YmxpYyBDb2RleCBhZHZlcnNhcmlhbCByZWdyZXNzaW9uLCBTMTEgd2F2ZSBBIGl0ZW0gMjo=",
        ),
        (
            "# Public adversarial-review finding reproduced by the regression below:",
            "IyBQdWJsaWMgQ29kZXggYWR2ZXJzYXJpYWwtcmV2aWV3IGZpbmRpbmcgcmVwcm9kdWNlZCBieSB0aGUgcmVncmVzc2lvbiBiZWxvdzo=",
        ),
    ),
    "tests/safety/test_sentinel.py": (
        (
            "    Every one of these walked past the phrase table in the red-team review. They",
            "ICAgIEV2ZXJ5IG9uZSBvZiB0aGVzZSB3YWxrZWQgcGFzdCB0aGUgcGhyYXNlIHRhYmxlIGluIHRoZSBDb2RleCByZWQgdGVhbS4gVGhleQ==",
        ),
        (
            "# Public adversarial-review finding reproduced by the regression below:",
            "IyBQdWJsaWMgQ29kZXggYWR2ZXJzYXJpYWwtcmV2aWV3IGZpbmRpbmcgcmVwcm9kdWNlZCBieSB0aGUgcmVncmVzc2lvbiBiZWxvdzo=",
        ),
    ),
    "tests/safety/test_validator.py": (
        (
            "    because 7 was 7 (red-team review). Every number is now typed by the unit",
            "ICAgIGJlY2F1c2UgNyB3YXMgNyAoQ29kZXggcmVkIHRlYW0pLiBFdmVyeSBudW1iZXIgaXMgbm93IHR5cGVkIGJ5IHRoZSB1bml0",
        ),
        (
            '    """Public adversarial regression, S11 wave A item 4:',
            "ICAgICIiIlB1YmxpYyBDb2RleCBhZHZlcnNhcmlhbCByZWdyZXNzaW9uLCBTMTEgd2F2ZSBBIGl0ZW0gNDo=",
        ),
        (
            "    def test_a_bare_number_is_not_a_blood_pressure(self) -> None:",
            "ICAgIGRlZiB0ZXN0X3RoZV9jb2RleF9zZW50ZW5jZShzZWxmKSAtPiBOb25lOg==",
        ),
    ),
    "tests/safety/test_vitals.py": (
        (
            "    def test_the_table_is_the_approved_numbers(self) -> None:",
            "ICAgIGRlZiB0ZXN0X3RoZV90YWJsZV9pc190aGVfbnVtYmVyc19tb2hhbWVkX2dhdmUoc2VsZikgLT4gTm9uZTo=",
        ),
    ),
}


class TableHash(_BoundaryValue):
    module: NonblankStr
    name: NonblankStr
    sha256: NonblankStr


# SHA-256 of exact UTF-8 assignment source, including literal layout and comments.
TABLE_HASHES: tuple[TableHash, ...] = (
    TableHash(
        module="normalize",
        name="_DIACRITICS",
        sha256="a701851ab0ff1a621574d2a4a15149a44602285b2d25dcd35cbaf459e99cc92b",
    ),
    TableHash(
        module="normalize",
        name="_LETTER_VARIANTS",
        sha256="6d46462e170a2a5eb0dd2cfd6a0a80da7355bcbf25bf098f6f6a5d116c3cfd3b",
    ),
    TableHash(
        module="normalize",
        name="_NON_TEXT",
        sha256="c14045128c8d352d93f9bc68f8cd69e8dbacfac4898f67fb48645bc2a175d77c",
    ),
    TableHash(
        module="normalize",
        name="FRANCO_ALIASES",
        sha256="ce1d0f206638bfedf721ed669a076326c45cf67561729daefb621c8b0e6fa49f",
    ),
    TableHash(
        module="sentinel",
        name="MUST_WAKE",
        sha256="70629645d850a5973e3d93c2198cdc83f7962f65862b5b398f5efb0512a14679",
    ),
    TableHash(
        module="sentinel",
        name="NEEDS_SUPPORT",
        sha256="23f6d69ffa14e4b5fc9d4de2d52b17a5a7a3482e55c4345e34823a438070faa7",
    ),
    TableHash(
        module="sentinel",
        name="_SUPPORT",
        sha256="5a9969b48f012f80372655bc2c677d9b85e1c1419b597d49b4d2ddb1e3df63ca",
    ),
    TableHash(
        module="sentinel",
        name="_NORMALIZED",
        sha256="029e6bca4251b1fdefde695f8764647d73b9310415ca90d1616d5a6f598759fb",
    ),
    TableHash(
        module="sentinel",
        name="CONCEPT_RULES",
        sha256="e9b50b63a812051718dac913f727338d02501ae193cfc934582e470ee8304f5d",
    ),
    TableHash(
        module="sentinel",
        name="_NORMALIZED_RULES",
        sha256="17d4990432ffda517a86fe8b38e7144c1619e47c531e8a7e5aebdcb2751f1f16",
    ),
    TableHash(
        module="sentinel",
        name="RESOLVED_MARKERS",
        sha256="75dd1da0cf6afb683d9b33b712d2638bbed5c8b11149736e26f5b915b935b30c",
    ),
    TableHash(
        module="sentinel",
        name="_RESOLVED",
        sha256="803c0f7397469769947109ca9de8a1a807c0d302270b9f51e4f5ea270a395e64",
    ),
    TableHash(
        module="sentinel",
        name="LAUGHTER_MARKERS",
        sha256="59cc88bc79e24f02cc04a83c512014a6082df7bad9c93b0f564eb0266e16108b",
    ),
    TableHash(
        module="sentinel",
        name="_LAUGHTER",
        sha256="44fbbe432008447de0bd80f5fb6f6994e334751732afcbb6a27691f123e15170",
    ),
    TableHash(
        module="sentinel",
        name="_LAUGHING_AMBIGUOUS",
        sha256="c1576ef2982050ceeafa0fd3b3a7df8b271662154cab145f70cd1f79be658e2a",
    ),
    TableHash(
        module="sentinel",
        name="NEVER_WAKE",
        sha256="56c7efcc02a841cd57ed9b64d23f31c76db952ff6a14ad10cd59052d0b641f90",
    ),
    TableHash(
        module="sentinel",
        name="EMERGENCY_AR",
        sha256="1b14ab1c24b34f9f1098fe8c3e7fea0854f0143a14ad9102a4334d80448e03e2",
    ),
    TableHash(
        module="sentinel",
        name="EMERGENCY_EN",
        sha256="3cddc0b1d320caa1c625a2c6111495383339910d16f7df686d63f4993dc41b61",
    ),
    TableHash(
        module="sentinel",
        name="MODEL_ERROR_NET",
        sha256="eae012e1e32b1f69f527afc0c4697c06a40aa84136c318fc0a4b2d0d1640b905",
    ),
    TableHash(
        module="sentinel",
        name="TRIAGE_UNAVAILABLE",
        sha256="03fd05cabd655b752b8c6ecb69dc08c826ba194eb71bfbeac1025744eb7e23ae",
    ),
    TableHash(
        module="sentinel",
        name="TRIAGE_PROMPT",
        sha256="2aeb830132171b2355c61d975ca1eb0608181841218106cf5bf87adfb60c7151",
    ),
    TableHash(
        module="labs",
        name="CANNOT_JUDGE_NOTE",
        sha256="5e1e4b7c6d05d2c0793cf4de037e41af473f1db50b0909b89bbc9fef27620d14",
    ),
    TableHash(
        module="labs",
        name="NOT_IN_TABLE_NOTE",
        sha256="af76271d8554a23ca7478964f8214ef02a3bb9c00f485d2bb04726139d07670a",
    ),
    TableHash(
        module="labs",
        name="URGENT_REVIEW_NOTE",
        sha256="172b658c7b9536d6da37c4713311dfe7b5f60c36d6eefb92d485c2d4b5b48045",
    ),
    TableHash(
        module="labs",
        name="CRITICAL_LABS",
        sha256="46d570c9ac376f2e92254d47b03480d4e5a0f729f8c279dfb560d8ece765da0c",
    ),
    TableHash(
        module="labs",
        name="ALIASES",
        sha256="052bfc8a6c091963a74181d1846f611250b12df6a0f60c874f4adfb30aeedb8d",
    ),
    TableHash(
        module="labs",
        name="HIGH_FLAGS",
        sha256="9924bd12f36400b1085e3e51a23adf81cfe2cd9be63fd88163966b04923e8e25",
    ),
    TableHash(
        module="labs",
        name="LOW_FLAGS",
        sha256="b23f1146bea15ab92ea0dd3d2d038dfcbb988a6d4cff22af1050d006586ede47",
    ),
    TableHash(
        module="labs",
        name="FLAG_PREFIX_MIN",
        sha256="8409071558146a92748834a7ebda8a45d62a6f180e5d0deb00f7c08a0a247299",
    ),
    TableHash(
        module="labs",
        name="URGENT_FLAGS",
        sha256="cd31dd83be0430138c6f39d3c3d672268fd3cdfb6912471538c7bd9189e74fc9",
    ),
    TableHash(
        module="labs",
        name="EXTREME_FLAGS",
        sha256="599e415b269688f7321d7878ca42607b2f47270f1d0fb4178808faa3b7825e14",
    ),
    TableHash(
        module="labs",
        name="ABDOMEN_WORDS",
        sha256="be0c15c5fa8f986b38d908922ee5bbcac86bbcf5a27609752535214490e06b20",
    ),
    TableHash(
        module="labs",
        name="PAIN_WORDS",
        sha256="82e868535e4e50c1463eb84f0a60743f223bfdb1d2a747695511daa6d0461ede",
    ),
    TableHash(
        module="labs",
        name="ABDOMINAL_PAIN_PHRASES",
        sha256="789a5955cd6bd939cb4bf1a90971efd093e98c5ba3817c0c11f66db057c6615b",
    ),
    TableHash(
        module="labs",
        name="NEGATION_WORDS",
        sha256="ba0bd4ac54e56af30d1901ccf2b23469cf13a241da390b31a43a716eef6fb757",
    ),
    TableHash(
        module="labs",
        name="NEGATION_REACH",
        sha256="2896b7da34c45d79bf31e47aba7360bc17bb694e60d72c0336bb656c4d69517e",
    ),
    TableHash(
        module="labs",
        name="PROXIMITY_TOKENS",
        sha256="4d59fbc76fb6838e922568b6781f2c8c7f267b7f29fe2bdda441ebe11216be02",
    ),
    TableHash(
        module="labs",
        name="_ABDOMEN",
        sha256="ebfc36cabb2e06b2d6c86dbd0175edb8e7bf91ab737b5a74ec113a3940b3eaeb",
    ),
    TableHash(
        module="labs",
        name="_PAIN",
        sha256="e82a776f468cc85584ec1065d38ba091926623c81b109477a4ce1059d33d1f14",
    ),
    TableHash(
        module="labs",
        name="_NEGATIONS",
        sha256="c919ce76d42612a30f939d1165f24f5ef9c513a08bcf6d2955aaa9696973dbdc",
    ),
    TableHash(
        module="labs",
        name="_ABDOMINAL_PHRASES",
        sha256="15c3183f6d2b16b93f1499e2b8566655754c5883dd42b548cf9b282c287e463a",
    ),
    TableHash(
        module="labs",
        name="PREGNANCY_NOT_CHECKED_NOTE",
        sha256="4a52def56097e1827f7d24350f03a5aa6543c641e2f62173e341a1c7075936e5",
    ),
    TableHash(
        module="labs",
        name="PREGNANCY_NONE_FOUND_NOTE",
        sha256="4e6575b949d7b3f36e03023ca4c14ec87c4e9eb615b0cb6cd7c3689fd426c568",
    ),
    TableHash(
        module="labs",
        name="_STRIP_PREFIXES",
        sha256="85a6f67cd6f7e9520c8332762df8ee0947e1239b2c96aa388f4a02b0c3d2bfe8",
    ),
    TableHash(
        module="labs",
        name="BY_ANALYTE",
        sha256="d502c2399aa649f08a913d4fecc31ea1c32a140b6940bab5514b000ae911b3ae",
    ),
    TableHash(
        module="labs",
        name="PANEL_WORDS",
        sha256="7e4e6fb5952cb7188f18c07865f41f4fd354e30b1d66b30554960a4c1f598f8a",
    ),
    TableHash(
        module="labs",
        name="EXTRA_PANEL_WORDS",
        sha256="bdca1ff5186c6a49d95e3a3ad078298bd336c408b1e4dedc35903083db0c2031",
    ),
    TableHash(
        module="labs",
        name="PANEL_ANALYTES",
        sha256="c54203d8d5d934c711f5bc933137950da52abc2063b7f400b1a9cb147683bcc3",
    ),
    TableHash(
        module="labs",
        name="DISPLAY",
        sha256="703f417d55fa7d158eba01d7b9848eaaf29086054a0b26cfa191df14250e37b1",
    ),
    TableHash(
        module="labs",
        name="_ANALYTE_SPLIT",
        sha256="9eb72b987dedcea7c40989a316ac3e9224f973745a7a5b9b845849f43c482c85",
    ),
    TableHash(
        module="labs",
        name="READABLE",
        sha256="4a94a87a73eeb59277ad673fbd9873a6e8cdc29a1fa3c4eccebf66c6c18973f9",
    ),
    TableHash(
        module="labs",
        name="ARABIC_TEST_NAMES",
        sha256="946f8ae09083f04f43cef189b2b07332cdad661ce4059bb8f02432c9d33330cb",
    ),
    TableHash(
        module="labs",
        name="_ARABIC_INDIC",
        sha256="e4d209ca1766d7849cb1de307acb87952ae2a2bbf19764bc8397daf56b2e6624",
    ),
    TableHash(
        module="labs",
        name="_VALUE",
        sha256="e2d2e31509d5dc4597840d892b1f8ce7d7f043334f9bea6427ce462a5a19c082",
    ),
    TableHash(
        module="labs",
        name="UNIT_CONVERSIONS",
        sha256="0cf7829b447acdfc16fb016e69f7c6c9bcb2374427ffda45cdfe95aab44d6e92",
    ),
    TableHash(
        module="vitals",
        name="SYSTOLIC_CRISIS",
        sha256="a11ef0af3621299e54e042ffabc044c815ffaecb5bd6dea2acb25becc041c902",
    ),
    TableHash(
        module="vitals",
        name="DIASTOLIC_CRISIS",
        sha256="ce3f13ae9ebd496a0a2a675dfe7220999c3db582e82358f8bfc74f6033ee3119",
    ),
    TableHash(
        module="vitals",
        name="SYSTOLIC_LOW",
        sha256="0e9bf85401a3022a5bca8f4302da730e73a62c44f8fb843b0e54987d5eb53854",
    ),
    TableHash(
        module="vitals",
        name="DECIDED_BY",
        sha256="0b26b46a0e6915c55308ba2bc009fb37c0a5cb31d9d34296d1d1b8da972df662",
    ),
    TableHash(
        module="vitals",
        name="CRISIS_CONCEPT",
        sha256="f3765b3a00bc7bdcf07a84314f24aca70ba01507311c08a7ddaaf26ec8c14ddf",
    ),
    TableHash(
        module="vitals",
        name="LOW_CONCEPT",
        sha256="f685372554a184fb811e5aac859ec46f8516b796fdd5627004e562b2936dd0b5",
    ),
    TableHash(
        module="vitals",
        name="BP_TEXT",
        sha256="68ecb6da07096aea00b8bfcb3cbdd0f82947fc0382cab27b005f14d713d6754a",
    ),
    TableHash(
        module="vitals",
        name="BP_IN_TEXT",
        sha256="f68281a2fd15467cc63ea941901e5b76b79ba15f61d82e0c0db0a756d34c804f",
    ),
    TableHash(
        module="vitals",
        name="PLAUSIBLE_SYSTOLIC",
        sha256="688acca9a299af4d479bdd91f2b9f5445a9e96950a439f765f621949506f0e2a",
    ),
    TableHash(
        module="vitals",
        name="PLAUSIBLE_DIASTOLIC",
        sha256="efb30f80f18d685b01840b98df8a748e261bf198033614db24ff8f55f3a93e16",
    ),
    TableHash(
        module="vitals",
        name="_SEVERITY",
        sha256="3a644ce7876f1ba8348f857d106ccf3b42c1034cf55ff02de2d7525b3598cce9",
    ),
    TableHash(
        module="validator",
        name="REASSURANCE",
        sha256="3108218d8738ee4d6a7654291a3760bbd2763d2c3e03e3d63f0e0de7e9870d0c",
    ),
    TableHash(
        module="validator",
        name="AMBULANCE",
        sha256="cbbf568d83c3e4d92634751c8d792c6eb5864fb784e955027b19b6965d0e6d57",
    ),
    TableHash(
        module="validator",
        name="_ARABIC_DIGITS",
        sha256="44160523749b593de501b4b55c03260ed60fe927929f29c5f49c6f94f39a25ec",
    ),
    TableHash(
        module="validator",
        name="_NUMBER",
        sha256="ba76ca573e9079a593c4271ad7b5ac080dc8b09e9ddf12edb2d76092baf3d221",
    ),
    TableHash(
        module="validator",
        name="UNIT_CLASSES",
        sha256="1071ef3d3246e2382bc40d73a582b6b07dabb04ebcc75af67f4394838fa8caf5",
    ),
    TableHash(
        module="validator",
        name="_CLASS_OF_UNIT",
        sha256="30cb72db919dbed2fc038f1bf6b08cb350f2f8a43bbab9ac2b6a6f40ca0fc0c3",
    ),
    TableHash(
        module="validator",
        name="CONTEXT_CLASSES",
        sha256="0519d738b3693b44cda4992eac562c214a93cbf30b260a33699bcef4c7ca4299",
    ),
    TableHash(
        module="validator",
        name="_UNIT_AFTER",
        sha256="879478d8280c2a563bd56a59b182857cc310664c6c7a91dd55432d68c0570295",
    ),
    TableHash(
        module="validator",
        name="WORD_NUMBERS",
        sha256="b19792f3993b7a61e45fc611b8db562a5fcebe258be9157d639996f03c74e5dc",
    ),
    TableHash(
        module="validator",
        name="GENERAL_FRAMING",
        sha256="07f478e16cdb120cb2398cb93f7c084214023dd382e56b3fde6e25907e74c664",
    ),
    TableHash(
        module="validator",
        name="IMPERATIVES",
        sha256="8d360191b15bfa91427253c14cdd8b6d62858d71c23e2f5f7c205c4f907939a4",
    ),
    TableHash(
        module="validator",
        name="DRUG_LEXICON",
        sha256="49abf4851c455abd1905d8692e29f27f10c416ce5b0a8ad4275535183c942b45",
    ),
    TableHash(
        module="validator",
        name="_DRUG_SUFFIX",
        sha256="48498d9a4b9ae7c7fa1502f492c09b3d3ed1aa1f61c675caf8a840b64ece5e00",
    ),
    TableHash(
        module="validator",
        name="NOT_A_DRUG",
        sha256="67898d342f57e529b9d4ff7eec949cdca1b88e880e984a284fcf8b69bd46e414",
    ),
    TableHash(
        module="validator",
        name="_NOT_A_DRUG",
        sha256="6cfac4d304c47decdbe06ff6def089a5b41f58d7852721b9f0eb6f3d445a168c",
    ),
    TableHash(
        module="validator",
        name="TAKE_WORDS",
        sha256="3fc1fd91948d2b28a1025672a063663959841bfd0f283b058ee7198f2132834b",
    ),
    TableHash(
        module="validator",
        name="ENTITY_WINDOW",
        sha256="374e169679a83f58ef6b88d649e32b3e56f0cb47fd3d18e62b2fa9e43f07b74e",
    ),
    TableHash(
        module="validator",
        name="_WORD",
        sha256="8e500eac5604b4422a4c532b14e37a09f16064c9caa564bf44211accda375c96",
    ),
    TableHash(
        module="validator",
        name="CHANGE_REQUESTS",
        sha256="cd71408b195bc486fa44003a44e2cd2c116ca11ad57b4367a53ecc4840f674ea",
    ),
    TableHash(
        module="validator",
        name="CHANGE_RULES",
        sha256="ef72ebe60098208a98317d9127d186d2ecf76ab12b784f55e189261075a37212",
    ),
    TableHash(
        module="validator",
        name="_CHANGE_RULES",
        sha256="0cdddd560fd2cebe21b178a2693d9a77c22d6783714738f8d53fa97af60ff33b",
    ),
    TableHash(
        module="validator",
        name="CHANGE_VOTE_PROMPT",
        sha256="3e47ddbbeb930ad99c47516a6ed9f4273bc2da9381e6fe4736f422d3d82d568c",
    ),
    TableHash(
        module="validator",
        name="REASSURANCE_VOTE_PROMPT",
        sha256="53308d6f084e70a03812c344ed3ade6bb10ea3f4c430b37ff958ae85d82c187e",
    ),
    TableHash(
        module="templates",
        name="ALLOWED_FIELDS",
        sha256="d6976ae3995c423f1663357f3063318432ad95627ebc0e221edd7e06a7500956",
    ),
    TableHash(
        module="templates",
        name="CLOSE_HELD",
        sha256="7d43c1ecc15331177cf0b681fc875df58b50e722464aacba2324ba8248ef6766",
    ),
    TableHash(
        module="templates",
        name="CLOSED_WITH_GAP",
        sha256="2f43b1bc9fbb9f5d38b511d9d8b476e9e682be3486df8738ef323ed6ff835436",
    ),
    TableHash(
        module="templates",
        name="CHECK_AGAIN",
        sha256="560e5415b7f213ac5a41f054f414d19b403844a903678d3adf62df5573dcd0b7",
    ),
    TableHash(
        module="templates",
        name="COST_TOLD",
        sha256="ecbf57b3d0b7e530f8acb205a5ed2d494bf6ee8fad246abcf40f8da016b15d76",
    ),
    TableHash(
        module="templates",
        name="SEND_WHEN_READY",
        sha256="4b02e96fdd03f27c51a951a03bb2a8af0b4c24dc5068ed8f77aff4444aa7e968",
    ),
    TableHash(
        module="templates",
        name="MISSING_PART",
        sha256="30a9f806ce0a2a7da3a8a9000aeca21f9bcd6d729539d922c324ebd15f39b2c7",
    ),
    TableHash(
        module="templates",
        name="FOLLOWUP_REASON",
        sha256="b84a24460374c8bdb5fb5d15dc4970e850816bf0c2ae7a6f248556b7e6b5ba65",
    ),
    TableHash(
        module="templates",
        name="TOLD_DOCTOR",
        sha256="f3d297ca4bf8535578a7ed16ad1d9ccf731de73ca1d03191ef2de18f1f52fb5b",
    ),
    TableHash(
        module="templates",
        name="PLAN_AGAIN",
        sha256="44ff3432f5764621b7bfaec4e29cb25dd84c1721bfcce30db57feb76ed8d42ba",
    ),
    TableHash(
        module="templates",
        name="SEND_IT_HERE",
        sha256="9e92b9e200704ec18fd5e83fa897c1c2cea75a13d183b9baea8341509805df04",
    ),
    TableHash(
        module="templates",
        name="TOLD_DOCTOR_WILL_ANSWER",
        sha256="30b1c22123ff7eec7701fd0d6a966f165228ce0a68cb58dae1d02057425129f0",
    ),
    TableHash(
        module="templates",
        name="WELCOME",
        sha256="a7db8d1fdb5d299f6f486ce904c5a7cdfa147e3741cf443f61fbbc3c295573de",
    ),
    TableHash(
        module="templates",
        name="WELCOME_NEXT",
        sha256="d6f9054c1294950688674421adb2c95b24bb01a7dfd60c445e75d0ab96f66b15",
    ),
    TableHash(
        module="templates",
        name="DOCTOR_SAYS",
        sha256="66a1046c6d70cb2ed6d8facfc14315b92cd56c0e273e45d2e1c5fde7c5450c59",
    ),
    TableHash(
        module="templates",
        name="ASK_AREA",
        sha256="40d5f460e8429af5bff0acc4a0e4e2d37599984da02dfaca626bb4695a13613d",
    ),
    TableHash(
        module="templates",
        name="ASK_PUBLIC_LAB",
        sha256="4da4a6f87d8ea641590eaebc5ff98673891696ab7cca2bd1715978ec689624f9",
    ),
    TableHash(
        module="templates",
        name="PLACES_FOUND",
        sha256="6202fbab7923b4711ac6f84aacf9a2fd7bcdfad4c539f3faaf4786932eb55cd4",
    ),
    TableHash(
        module="templates",
        name="PLACES_CHEAP",
        sha256="74ff0dc3f0549d52b64c5ba26a0d0ee6559fc59fb570c6824a4e05f03ebb0f98",
    ),
    TableHash(
        module="templates",
        name="TEMPLATES",
        sha256="2bd8851558fcb126d780d1172108ace684b3f939cc078887305520ec74d8b044",
    ),
    TableHash(
        module="templates",
        name="_HANGING",
        sha256="86d93be3bcb6e3104992bbb99b18b55df5a2833d500b3a3afb1d2caf9a99f1d9",
    ),
)
