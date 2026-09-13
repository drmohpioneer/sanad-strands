"""Locked logo geometry, theme paint, Retina separation and asset boundary (18h-L/R1)."""

import io
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import pytest
from PIL import Image
from playwright.sync_api import Page, Route, expect

from browser.conftest import RenderedApp

ASSETS = Path(__file__).parents[2] / "src/sanad/web/static"
SVG = "{http://www.w3.org/2000/svg}"
RETINA = pytest.mark.parametrize(
    "rendered", [(1440, "dark"), (1440, "light"), (390, "dark"), (390, "light")], indirect=True
)


def components(image: Image.Image) -> list[int]:
    """Package eight-neighbour alpha method, with the locked >=128 threshold."""
    alpha = image.convert("RGBA").getchannel("A")
    pixels = alpha.tobytes()
    todo = {
        (x, y)
        for y in range(image.height)
        for x in range(image.width)
        if pixels[y * image.width + x] >= 128
    }
    sizes = []
    while todo:
        stack, count = [todo.pop()], 0
        while stack:
            x, y = stack.pop()
            count += 1
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in todo:
                        todo.remove(q)
                        stack.append(q)
        sizes.append(count)
    return sorted(sizes)


def check_logo(page: Page) -> None:
    from browser.corrections19 import assert_identity

    assert_identity(page)
    expect(page.locator('link[rel="icon"]')).to_have_attribute("href", "/assets/favicon.svg")
    source = ET.parse(ASSETS / "logo/sanad-lockup-compact-navy.svg").getroot()
    lockup = page.locator(".identity > svg.sanad-lockup")
    assert lockup.get_attribute("viewBox") == source.attrib["viewBox"]
    actual = ET.fromstring(lockup.evaluate("e=>e.outerHTML"))
    for tag, attributes in (
        ("svg", ("viewBox", "x", "y")),
        ("path", ("d", "transform", "stroke-width", "stroke-linecap", "stroke-linejoin")),
        ("circle", ("cx", "cy", "r")),
        ("linearGradient", ("x1", "y1", "x2", "y2", "gradientUnits", "color-interpolation")),
        ("g", ("transform",)),
    ):
        expected = list(source.iter(SVG + tag))
        found = list(actual.iter(SVG + tag))
        assert len(found) == len(expected), tag
        for left, right in zip(found, expected, strict=True):
            assert {a: left.get(a) for a in attributes} == {a: right.get(a) for a in attributes}
    ids = page.locator("linearGradient").evaluate_all("es=>es.map(e=>e.id)")
    assert len(ids) == len(set(ids))
    theme = page.locator("html").get_attribute("data-theme")
    colors = (
        ["rgb(109, 123, 255)", "rgb(34, 211, 238)", "rgb(238, 241, 250)"]
        if theme == "dark"
        else ["rgb(75, 87, 216)", "rgb(14, 116, 144)", "rgb(17, 24, 52)"]
    )
    assert (
        lockup.locator("stop").evaluate_all("es=>es.map(e=>getComputedStyle(e).stopColor)")
        == colors[:2]
    )
    assert lockup.locator("circle").evaluate("e=>getComputedStyle(e).fill") == colors[1]
    assert (
        lockup.locator("g path").evaluate_all("es=>es.map(e=>getComputedStyle(e).fill)")
        == [colors[2]] * 5
    )
    symbol = lockup.locator("svg")
    size = 30 if page.locator(".rail").count() else 58
    viewport = symbol.evaluate("e=>{const m=e.getScreenCTM();return [128*m.a,128*m.d]}")
    assert viewport == pytest.approx([size, size], abs=0.02), viewport
    outer = lockup.bounding_box()
    assert outer and outer["width"] == pytest.approx(227 * size / 58, abs=0.02)
    assert outer["height"] == pytest.approx(64 * size / 58, abs=0.02)
    assert lockup.evaluate(
        "e=>getComputedStyle(e).transform==='none'&&"
        "[e,...e.querySelectorAll('*')].every(n=>{const s=getComputedStyle(n);"
        "return s.filter==='none'&&s.boxShadow==='none';})"
    )
    # Clone the actual nested symbol with its computed paint onto transparency.
    capture = symbol.evaluate("""e=>{
      const c=e.cloneNode(true);c.removeAttribute('x');c.removeAttribute('y');
      c.querySelectorAll('stop').forEach((n,i)=>n.setAttribute('stop-color',getComputedStyle(e.querySelectorAll('stop')[i]).stopColor));
      c.querySelector('circle').setAttribute('fill',getComputedStyle(e.querySelector('circle')).fill);
      return c.outerHTML;
    }""")
    with page.context.new_page() as transparent:
        transparent.set_content(
            f"<style>html,body{{margin:0;background:transparent}}svg{{display:block;width:{size}px;height:{size}px}}</style>{capture}"
        )
        png = transparent.locator("svg").screenshot(omit_background=True)
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (size * 2, size * 2)
        sizes = components(image)
    assert len(sizes) == 2, sizes
    print(f"logo theme={theme} symbol={size}px components={sizes}")


def request_rail(requests: list[tuple[str, str]]) -> None:
    allowed = {
        "/assets/favicon.svg",
        "/assets/inter.ttf",
        "/assets/playfairdisplay.ttf",
        "/assets/notosansarabic.ttf",
        "/assets/notonaskharabic.ttf",
    }
    assert all(path in allowed for path, kind in requests if kind in {"image", "font"}), requests
    print("image/font requests:", sorted({p for p, k in requests if k in {"image", "font"}}))


@RETINA
def test_logo_pages(rendered: RenderedApp) -> None:
    page = rendered.page
    requests: list[tuple[str, str]] = []
    page.on("request", lambda r: requests.append((urlsplit(r.url).path, r.resource_type)))
    for path in ("/demo", "/demo/patient", "/demo/admin"):
        page.goto(rendered.origin + path)
        page.locator(".identity > svg.sanad-lockup").wait_for()
        page.evaluate("document.fonts.ready")
        check_logo(page)
    request_rail(requests)
    response = page.request.get(rendered.origin + "/assets/favicon.svg")
    assert response.status == 200 and response.headers["content-type"] == "image/svg+xml"
    assert response.body() == (ASSETS / "favicon.svg").read_bytes()
    for path in (
        "/assets/logo/sanad-mark-navy.svg",
        "/assets/sanad-mark-navy.svg",
        "/assets/other.svg",
    ):
        assert page.request.get(rendered.origin + path).status == 404


def entry_markup(markup: str, route: Route) -> None:
    route.fulfill(content_type="text/html", body=markup)


@RETINA
def test_aurora_entry_logo(rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch) -> None:
    page = rendered.page
    requests: list[tuple[str, str]] = []
    page.on("request", lambda r: requests.append((urlsplit(r.url).path, r.resource_type)))
    assert len(rendered.entry_paths) == 4
    for path in rendered.entry_paths:
        page.goto(rendered.origin + path)
        check_logo(page)
    # Interactive entry shell and Arabic scriptless shell are checked in both paints;
    # scriptless entry pages above must keep their actual dark default.
    from sanad.web.pages import shell

    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    for locale in ("en", "ar"):
        for interactive in (False, True):
            markup = shell("Sanad", "", locale, interactive=interactive)
            page.route(
                "**/logo-entry",
                partial(entry_markup, markup),
            )
            page.goto(rendered.origin + "/logo-entry")
            if not interactive:
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            for theme in ("dark", "light"):
                page.locator("html").evaluate("(e,t)=>e.dataset.theme=t", theme)
                check_logo(page)
            page.unroute("**/logo-entry")
    request_rail(requests)
