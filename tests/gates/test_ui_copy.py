"""UI copy and client money-math gate (RG-2, RG-8, banner, replay label).

Scans the frontend sources in web/src and the built bundle in app/static.
Test files (``*.test.*``, ``*.spec.*`` and ``web/src/test/``) are not
user-facing and are skipped; they are the only allowlist.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"
STATIC = ROOT / "app" / "static"
BANNER = "Synthetic data • simulated filing"

_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".html"}
_STATIC_SUFFIXES = {".js", ".html"}
_FILING_WORDS = re.compile(r"\b(?:filed|paid|submitted|accepted)\b", re.IGNORECASE)
_SIMULATED = re.compile(r"\bsimulated\b", re.IGNORECASE)
_ALLOWED_CLAIM = re.compile(r"not recovered cash", re.IGNORECASE)
_CLAIM_WORDS = re.compile(r"\b(?:net|savings|recovered)\b", re.IGNORECASE)
# aria-live is an accessibility attribute, not an extraction label.
_LIVE = re.compile(r"(?<!aria-)\blive\b", re.IGNORECASE)
_MONEY_MATH = (
    re.compile(r"\bparseFloat\b"),
    re.compile(r"\bparseInt\b"),
    re.compile(r"\bNumber\s*\("),
    re.compile(r"\.toFixed\b"),
    re.compile(r"\bMath\.round\b"),
    re.compile(r"\.value\s*\+(?!\+)"),
    re.compile(r"(?<!\+)\+\s*[\w$.\[\]?]*\.value\b"),
    re.compile(r"\breduce\s*\([^;]{0,300}?\.value\b", re.DOTALL),
)
_QUOTES = "\"'`"


def is_test_file(path: Path, base: Path) -> bool:
    relative = path.relative_to(base)
    return (
        ".test." in path.name
        or ".spec." in path.name
        or "test" in relative.parts[:-1]
        or "__tests__" in relative.parts[:-1]
    )


def source_files(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [
        path
        for path in sorted(base.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in _SOURCE_SUFFIXES
        and "node_modules" not in path.parts
        and not is_test_file(path, base)
    ]


def static_files(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [
        path
        for path in sorted(base.rglob("*"))
        if path.is_file() and path.suffix.lower() in _STATIC_SUFFIXES
    ]


def split_source(text: str) -> tuple[str, list[str]]:
    """Code with comments removed and strings blanked, plus string contents."""
    code: list[str] = []
    strings: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        pair = text[index : index + 2]
        if pair == "//":
            end = text.find("\n", index)
            index = length if end == -1 else end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            code.append(" ")
            continue
        if char in _QUOTES:
            end = index + 1
            while end < length and text[end] != char:
                if text[end] == "\\":
                    end += 1
                elif text[end] == "\n" and char != "`":
                    break
                end += 1
            strings.append(text[index + 1 : end])
            code.append(char + char)
            index = end + 1
            continue
        code.append(char)
        index += 1
    return "".join(code), strings


def jsx_text(code: str) -> list[str]:
    """Text between JSX tags or expressions, in code with strings blanked."""
    return [
        match.group(1).strip()
        for match in re.finditer(r"[>}]([^<>{}]+)[<{]", code)
        if re.search(r"[A-Za-z]", match.group(1))
    ]


def user_strings(text: str) -> list[str]:
    code, strings = split_source(text)
    return [item for item in strings if item.strip()] + jsx_text(code)


def filing_violations(strings: list[str]) -> list[str]:
    return [
        item
        for item in strings
        if _FILING_WORDS.search(item) and not _SIMULATED.search(item)
    ]


def claim_violations(strings: list[str]) -> list[str]:
    found: list[str] = []
    for item in strings:
        masked = _ALLOWED_CLAIM.sub(" ", item)
        found.extend(match.group(0) for match in _CLAIM_WORDS.finditer(masked))
    return found


def live_violations(strings: list[str]) -> list[str]:
    return [item for item in strings if _LIVE.search(item)]


def money_math_violations(text: str) -> list[str]:
    code, _strings = split_source(text)
    found: list[str] = []
    for pattern in _MONEY_MATH:
        found.extend(match.group(0) for match in pattern.finditer(code))
    return found


def _quoted_window(text: str, start: int, end: int) -> str:
    left = start
    while left > 0 and start - left < 300 and text[left - 1] not in _QUOTES + "\n":
        left -= 1
    right = end
    while right < len(text) and right - end < 300 and text[right] not in _QUOTES + "\n":
        right += 1
    return text[left:right]


def bundle_violations(text: str) -> list[str]:
    """Minified output: the same copy rules over a window bounded by quotes."""
    found: list[str] = []
    for match in _FILING_WORDS.finditer(text):
        window = _quoted_window(text, match.start(), match.end())
        if not _SIMULATED.search(window):
            found.append(window)
    masked = _ALLOWED_CLAIM.sub(" ", text)
    found.extend(match.group(0) for match in _CLAIM_WORDS.finditer(masked))
    found.extend(match.group(0) for match in _LIVE.finditer(text))
    return found


def scan_source_tree(base: Path) -> dict[str, list[str]]:
    problems: dict[str, list[str]] = {}
    for path in source_files(base):
        text = path.read_text(encoding="utf-8")
        strings = user_strings(text)
        found = (
            [f"RG-8: {item}" for item in filing_violations(strings)]
            + [f"RG-2: {item}" for item in claim_violations(strings)]
            + [f"live label: {item}" for item in live_violations(strings)]
            + [f"money math: {item}" for item in money_math_violations(text)]
        )
        if found:
            problems[str(path.relative_to(base))] = found
    return problems


def scan_static_tree(base: Path) -> dict[str, list[str]]:
    problems: dict[str, list[str]] = {}
    for path in static_files(base):
        found = bundle_violations(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            problems[str(path.relative_to(base))] = found
    return problems


def tree_has_banner(base: Path, files: list[Path]) -> bool:
    return any(BANNER in path.read_text(encoding="utf-8") for path in files)


# Production scans.


def test_web_sources_pass_copy_and_money_rules() -> None:
    assert source_files(WEB_SRC), "web/src has no sources to scan"
    assert scan_source_tree(WEB_SRC) == {}


def test_built_bundle_passes_copy_rules() -> None:
    assert static_files(STATIC), "app/static has no build output to scan"
    assert scan_static_tree(STATIC) == {}


def test_banner_is_in_the_shell_and_the_build() -> None:
    assert tree_has_banner(WEB_SRC, source_files(WEB_SRC))
    assert tree_has_banner(STATIC, static_files(STATIC))


# Planted fixtures: each rule catches a violation in a scratch tree.


def _plant(tmp_path: Path, name: str, body: str) -> Path:
    base = tmp_path / "src"
    target = base / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return base


@pytest.mark.parametrize(
    "body",
    [
        "export const Status = () => <p>Filed</p>;\n",
        'const status = "Return submitted";\n',
        "const note = `Payment accepted`;\n",
        "export const Paid = () => <span>{count} paid</span>;\n",
    ],
)
def test_rg8_catches_filing_claims(tmp_path: Path, body: str) -> None:
    problems = scan_source_tree(_plant(tmp_path, "Status.jsx", body))
    assert any(item.startswith("RG-8") for item in problems["Status.jsx"])


@pytest.mark.parametrize(
    "body",
    [
        'const s = "READY_TO_FILE_SIMULATED";\n',
        "export const B = () => <strong>Synthetic data • simulated filing</strong>;\n",
        'const s = "simulated: not filed or paid";\n',
    ],
)
def test_rg8_allows_simulated_in_the_same_string(tmp_path: Path, body: str) -> None:
    assert scan_source_tree(_plant(tmp_path, "Ok.jsx", body)) == {}


def test_rg8_simulated_elsewhere_does_not_excuse_filed(tmp_path: Path) -> None:
    body = 'const a = "simulated filing";\nconst b = "Filed";\n'
    problems = scan_source_tree(_plant(tmp_path, "Mixed.jsx", body))
    assert problems["Mixed.jsx"] == ["RG-8: Filed"]


@pytest.mark.parametrize(
    "body",
    [
        "export const T = () => <h3>Net position</h3>;\n",
        'const title = "Savings found";\n',
        "export const R = () => <p>{amount} recovered</p>;\n",
    ],
)
def test_rg2_catches_claims(tmp_path: Path, body: str) -> None:
    problems = scan_source_tree(_plant(tmp_path, "Claim.jsx", body))
    assert any(item.startswith("RG-2") for item in problems["Claim.jsx"])


def test_rg2_allows_not_recovered_cash(tmp_path: Path) -> None:
    body = "export const L = () => <h3>Refund candidates, not recovered cash</h3>;\n"
    assert scan_source_tree(_plant(tmp_path, "Label.jsx", body)) == {}


def test_live_label_is_caught(tmp_path: Path) -> None:
    body = "export const M = () => <span>Extraction: live</span>;\n"
    problems = scan_source_tree(_plant(tmp_path, "Mode.jsx", body))
    assert problems["Mode.jsx"] == ["live label: Extraction: live"]


@pytest.mark.parametrize(
    "body",
    [
        "const x = parseFloat(total.value);\n",
        "const x = parseInt(total.value, 10);\n",
        "const x = Number(total.value);\n",
        "const x = amount.toFixed(2);\n",
        "const x = Math.round(cents);\n",
        "const x = a.value + b.value;\n",
        "const x = sum + row.amount.value;\n",
        "const x = rows.reduce((acc, row) => acc + row.total.value, 0);\n",
    ],
)
def test_money_math_is_caught(tmp_path: Path, body: str) -> None:
    problems = scan_source_tree(_plant(tmp_path, "Math.jsx", body))
    assert any(item.startswith("money math") for item in problems["Math.jsx"])


def test_money_math_in_comments_and_strings_is_ignored(tmp_path: Path) -> None:
    body = (
        "// parseFloat(total.value) is banned\n"
        "/* Math.round */\n"
        'const help = "never Number(x)";\n'
        "const shown = `${amount.value} ${amount.currency}`;\n"
    )
    assert scan_source_tree(_plant(tmp_path, "Fine.jsx", body)) == {}


def test_test_files_are_the_only_allowlist(tmp_path: Path) -> None:
    body = "const x = parseFloat(total.value); const s = 'Filed';\n"
    base = _plant(tmp_path, "Thing.test.jsx", body)
    (base / "test").mkdir()
    (base / "test" / "helper.js").write_text(body, encoding="utf-8")
    assert scan_source_tree(base) == {}
    (base / "Thing.jsx").write_text(body, encoding="utf-8")
    assert set(scan_source_tree(base)) == {"Thing.jsx"}


@pytest.mark.parametrize(
    "bundle",
    [
        'var a="Filed";',
        'n.jsx("p",{children:"Return submitted"})',
        'x="Net total"',
        "y='savings'",
        'z="Extraction: live"',
    ],
)
def test_bundle_scan_catches_planted_copy(bundle: str) -> None:
    assert bundle_violations(bundle)


def test_bundle_scan_allows_aria_live() -> None:
    bundle = 'n.jsx("span",{className:"job","aria-live":"polite"})'
    assert bundle_violations(bundle) == []


def test_bundle_scan_allows_simulated_and_not_recovered_cash() -> None:
    bundle = (
        'a="READY_TO_FILE_SIMULATED";b="not recovered cash";c="simulated: not paid"'
    )
    assert bundle_violations(bundle) == []


def test_missing_banner_is_caught(tmp_path: Path) -> None:
    base = _plant(tmp_path, "App.jsx", "export const A = () => <p>Tax Leak</p>;\n")
    assert not tree_has_banner(base, source_files(base))
    (base / "App.jsx").write_text(
        f"export const A = () => <p>{BANNER}</p>;\n", encoding="utf-8"
    )
    assert tree_has_banner(base, source_files(base))
