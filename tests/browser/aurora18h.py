"""Approved sample tokens, keyboard behavior, motion and layout rails."""

import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from browser.conftest import RenderedApp
from browser.depth18e import RETINA, ready

SAMPLE = Path(__file__).with_name("sample18h.json")
# Inert compatibility name set/reset by the frozen browser fixture.
OWNER_SUBTESTS: ContextVar[pytest.Subtests | None] = ContextVar("owner_subtests", default=None)


def settle_paint(page: Page) -> None:
    page.evaluate("document.fonts.ready")
    page.wait_for_function("""() => document.getAnimations().every(a=>
      a.playState!=='running' || a.effect.getTiming().iterations===Infinity)""")


def normalized_fold(page: Page) -> tuple[float, float, float, float]:
    def bounds(target: Page, row: str, toolbar: str) -> tuple[float, float]:
        return tuple(
            target.evaluate(
                """([row,toolbar])=>{
          const r=document.querySelector(row).getBoundingClientRect();
          const boxes=[...document.querySelectorAll(toolbar)].map(e=>e.getBoundingClientRect());
          return [r.y+scrollY,Math.max(...boxes.map(r=>r.bottom))-Math.min(...boxes.map(r=>r.top))];
        }""",
                [row, toolbar],
            )
        )

    reveal_all(page)
    page.evaluate("scrollTo(0,0)")
    settle_paint(page)
    actual = bounds(page, ".patient-row", ".toolbar,.filter-summary")
    fixture = json.loads(SAMPLE.read_text())["fold"]
    reference = (fixture["row_y"], fixture["toolbar_height"])
    # R1.5: retain the restored spacing. The real wording/caption/header contribution
    # is disclosed separately from the sample measurement, with one CSS pixel tolerance.
    assert actual[0] - actual[1] <= (
        reference[0] - reference[1] + fixture["restored_doctor_extra"] + 1
    ), (actual, reference)
    print("FOLD product-y/toolbar mockup-y/toolbar", *actual, *reference)
    return *actual, *reference


@RETINA
def test_aurora_tokens(rendered: RenderedApp) -> None:
    """Compare the public sample transcription and resolved aliases in both themes."""
    fixture = json.loads(SAMPLE.read_text())
    css = (Path(__file__).parents[2] / "src/sanad/web/static/browser.css").read_text()
    blocks = re.findall(r"(?:^|\n)(:root|\[data-theme=light\])\{([^}]+)\}", css)
    page = ready(rendered)
    for theme in ("dark", "light"):
        reference = fixture["tokens"]["dark"]["values"] | (
            fixture["tokens"]["light"]["values"] if theme == "light" else {}
        )
        selector = ":root" if theme == "dark" else "[data-theme=light]"
        first = next(block for sel, block in blocks if sel == selector)
        parsed = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", first))
        assert parsed == fixture["tokens"][theme]["values"]
        page.locator(f'[data-theme-set="{theme}"]').click()
        # A separate inline reference resolves var() aliases through only the sample.
        # No product declaration or rendered element is changed by this comparison.
        comparison = page.evaluate(
            """({reference,aliases}) => {
          const probe=document.createElement('div');
          for(const [key,value] of Object.entries({...reference,...aliases}))
            probe.style.setProperty('--'+key,value);
          document.body.append(probe);
          const expected=getComputedStyle(probe),actual=getComputedStyle(document.documentElement);
          const differences=Object.keys({...reference,...aliases}).flatMap(key=>{
            const a=actual.getPropertyValue('--'+key).trim();
            const b=expected.getPropertyValue('--'+key).trim();
            return a===b?[]:[[key,a,b]];
          });probe.remove();return differences;
        }""",
            {"reference": reference, "aliases": fixture["aliases"]},
        )
        assert comparison == [], (theme, comparison)


@RETINA
def test_aurora_themes(rendered: RenderedApp) -> None:
    page = ready(rendered)
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator('meta[name="color-scheme"]')).to_have_attribute("content", "dark")
    page.evaluate("localStorage.setItem('sanad-theme','system')")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    for theme in ("light", "dark"):
        page.locator(f'[data-theme-set="{theme}"]').click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        expect(page.locator('meta[name="color-scheme"]')).to_have_attribute("content", theme)
        expect(page.locator(f'[data-theme-set="{theme}"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        assert page.evaluate("localStorage.getItem('sanad-theme')") == theme
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
    root = Path(__file__).parents[2] / "src/sanad/web/static"
    for name in ("theme.js", "browser.css", "browser.js"):
        assert "prefers-color-scheme" not in (root / name).read_text()


def reveal_all(page: Page) -> None:
    page.evaluate("scrollTo(0,0)")
    while True:
        page.wait_for_function(
            """() => [...document.querySelectorAll('.rv')].every(e=>{
          if(!e.checkVisibility({checkVisibilityCSS:true}))return true;
          const r=e.getBoundingClientRect(), h=Math.min(r.bottom,innerHeight*.92)-Math.max(r.top,0);
          return h<=0 || (h/r.height<.15&&r.height<=innerHeight) || e.classList.contains('in');
        })""",
            timeout=2000,
        )
        if page.evaluate("scrollY+innerHeight>=document.documentElement.scrollHeight-1"):
            break
        page.evaluate("scrollBy(0,innerHeight*.75)")
    assert page.locator(".rv").evaluate_all("""es=>es.every(e=>
      !e.checkVisibility({checkVisibilityCSS:true})||e.classList.contains('in'))""")


@RETINA
def test_aurora_motion_layout_and_staging(rendered: RenderedApp) -> None:
    from store.account_fixtures import ADMIN, PATIENT

    from browser.depth18e import fits, identity
    from browser.words18g import old_word_walk

    page = rendered.page
    for subject, paths in (
        (rendered.world.owner.subject, ("/demo", "/a", "/a/inbox", "/a/history", "/a/preferences")),
        (PATIENT, ("/pp", "/demo/patient")),
        (ADMIN, ("/admin", "/demo/admin")),
    ):
        rendered.login(subject)
        for path in paths:
            if "admin" in path:
                page.goto(rendered.origin + path)
                page.locator("#admin-applications .summary-strip").wait_for()
            else:
                ready(rendered, path)
            reveal_all(page)
            page.wait_for_function("""() => [...document.querySelectorAll('[data-count]')]
              .every(e=>e.textContent===e.dataset.count)""")
            identity(page)
            old_word_walk(page)
            fits(page)
            page.emulate_media(reduced_motion="reduce")
            assert page.locator(".aurora i").evaluate_all(
                "es=>es.every(e=>parseFloat(getComputedStyle(e).animationDuration)<=.001)"
            )
            page.wait_for_function(
                """() => [...document.querySelectorAll('.rv')]
              .every(e=>getComputedStyle(e).opacity==='1')""",
                timeout=2000,
            )
            assert page.evaluate("""() => [...document.querySelectorAll('*')].every(e=>
              [null,'::before','::after'].every(p=>{
                const s=getComputedStyle(e,p);return [s.animationDuration,s.transitionDuration]
                  .every(v=>v.split(',').every(d=>parseFloat(d)<=.001));
              }))""")
            assert page.locator(".tab-panel,.patient-row").evaluate_all(
                "es=>es.every(e=>getComputedStyle(e).transform==='none')"
            )
            page.emulate_media(reduced_motion="no-preference")
    rendered.login(rendered.world.owner.subject)
    ready(rendered, "/demo#demo-000")
    for tab in ("Medicines", "Requests", "Documents", "History"):
        page.get_by_role("tab", name=tab, exact=True).click()
        reveal_all(page)
        fits(page)
    # Exactly the mockup breakpoint; no inherited 959px rail recipe remains.
    # The nav scrolls only on narrow screens, so the sample's active-link bar paints on desktop.
    for width, direction, overflow in (
        (1440, "column", "visible"),
        (901, "column", "visible"),
        (900, "row", "auto"),
    ):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.locator(".rail").evaluate("e=>getComputedStyle(e).flexDirection") == direction
        assert page.locator(".rail nav").evaluate("e=>getComputedStyle(e).overflowX") == overflow


@pytest.mark.parametrize("rendered", [(1440, "dark")], indirect=True)
def test_aurora_performance(rendered: RenderedApp) -> None:
    import json

    page = rendered.page
    rendered.browser.start_tracing(
        page=page, categories=["devtools.timeline", "blink.user_timing", "toplevel"]
    )
    ready(rendered)
    page.wait_for_timeout(5000)
    trace = json.loads(rendered.browser.stop_tracing())["traceEvents"]
    paints = [e["ts"] for e in trace if e.get("name") in {"firstPaint", "firstContentfulPaint"}]
    assert paints, "The trace must contain a first paint"
    tasks = [
        e
        for e in trace
        if e.get("name") in {"RunTask", "ThreadControllerImpl::RunTask"}
        and e.get("ph") == "X"
        and e["ts"] > min(paints)
    ]
    assert tasks, sorted({e.get("name", "") for e in trace if e.get("ph") == "X"})
    assert max(e.get("dur", 0) for e in tasks) <= 200_000
    page.locator('#filter [data-filter="all"]').click()
    expect(page.locator(".patient-row")).to_have_count(18)
    assert page.locator("body *").evaluate_all("""es=>es.filter(e=>
      getComputedStyle(e).backdropFilter!=='none').length<=40""")
    assert page.locator("body *").evaluate_all("""es=>es.every(e=>
      getComputedStyle(e).filter==='none'||e.matches('.aurora i,.rv:not(.in)'))""")
    expect(page.locator(".aurora i")).to_have_count(3)


@RETINA
def test_aurora_trends(rendered: RenderedApp, aurora_monitor: dict[str, Any]) -> None:
    from browser.words18g import goto, old_word_walk, projected, record

    page = rendered.page
    for comparator, threshold, hot in (
        ("gt", "130", [False, False, True]),
        ("ge", "130", [False, True, True]),
        ("lt", "130", [True, False, False]),
        ("le", "130", [True, True, False]),
    ):
        order: dict[str, Any] = {
            "status": "active",
            "current_version": {
                "type": "value_alert",
                "structured_instruction": {
                    "metric": "blood_pressure",
                    "unit": "mmHg",
                    "comparator": comparator,
                    "threshold": threshold,
                },
            },
        }
        for variant in ("matching", "inactive", "unit", "metric", "absent"):
            import copy

            candidate = copy.deepcopy(order)
            if variant == "inactive":
                candidate["status"] = "superseded"
            if variant in {"unit", "metric"}:
                candidate["current_version"]["structured_instruction"][variant] = "other"
            data = record(
                rendered,
                missions=[aurora_monitor],
                orders=[] if variant == "absent" else [candidate],
            )
            projected(rendered, data)
            goto(rendered, f"/a/patients/{data['patient_id']}")
            page.get_by_role("tab", name="Requests", exact=True).click()
            bars = page.locator(".trend span")
            expect(bars).to_have_count(3)
            assert bars.evaluate_all("es=>es.map(e=>e.classList.contains('hot'))") == (
                hot if variant == "matching" else [False, False, False]
            )
            assert bars.evaluate_all("es=>es.map(e=>parseFloat(e.style.height))") == [
                75,
                81.25,
                100,
            ]
            reveal_all(page)
            old_word_walk(page)
    aurora_monitor["details"]["readings"] = []
    projected(rendered, record(rendered, missions=[aurora_monitor]))
    goto(rendered, f"/a/patients/{rendered.world.patient_scope.patient_id}")
    expect(page.locator(".trend")).to_have_count(0)


@RETINA
def test_aurora_keyboard_focus(rendered: RenderedApp) -> None:
    page = ready(rendered)
    page.emulate_media(reduced_motion="reduce")
    for key in ("needs", "all", "settled"):
        segment = page.locator(f'#filter [data-filter="{key}"]')
        segment.focus()
        page.keyboard.press("Enter")
        expect(segment).to_have_attribute("aria-pressed", "true")
        expect(segment).to_be_focused()
        assert page.locator('#filter button[aria-pressed="true"]').count() == 1
        assert segment.evaluate("e=>getComputedStyle(e).boxShadow") != "none"

    check_focus_controls(page)
    ready(rendered, "/demo#demo-000")
    for tab in ("Medicines", "Requests", "Documents", "History"):
        page.get_by_role("tab", name=tab, exact=True).click()
        check_focus_controls(page)
    ready(rendered, "/a/preferences")
    check_focus_controls(page)


def check_focus_controls(page: Page) -> None:
    reveal_all(page)
    settle_paint(page)
    page.mouse.move(0, 0)
    for control in page.locator("a,button,input,select,textarea,summary,[tabindex]").all():
        if not control.is_visible() or control.is_disabled():
            continue
        if control.get_attribute("tabindex") == "-1":
            continue
        control.scroll_into_view_if_needed()
        page.keyboard.press("Tab")
        control.focus()
        expect(control).to_be_focused()
        # The sample glow resolves as the box shadow, independently of its colour.
        expected = control.evaluate("""e=>{
          const probe=document.createElement('span');probe.style.boxShadow='var(--glow)';
          document.body.append(probe);const shadow=getComputedStyle(probe).boxShadow;
          probe.remove();return shadow;
        }""")
        if control.get_attribute("id") == "search":
            # As in the sample, the search box lights its border and glow; the input has no ring.
            settle_paint(page)
            box = """e=>{const w=getComputedStyle(e.closest('.search'));
              const probe=document.createElement('span');probe.style.color='var(--indigo)';
              document.body.append(probe);const indigo=getComputedStyle(probe).color;probe.remove();
              return [w.boxShadow,w.borderTopColor,indigo,getComputedStyle(e).boxShadow];}"""
            shadow, border, indigo, own = control.evaluate(box)
            assert shadow == expected and border == indigo and own == "none"
            control.evaluate("e=>e.blur()")
            settle_paint(page)
            shadow, border, indigo, _ = control.evaluate(box)
            assert shadow != expected and border != indigo
            continue
        focused = control.evaluate("e=>getComputedStyle(e).boxShadow")
        assert focused == expected and focused != "none"
        control.evaluate("e=>e.blur()")
        settle_paint(page)
        assert control.evaluate("e=>getComputedStyle(e).boxShadow") != focused


@pytest.mark.parametrize("rendered", [(1440, "dark")], indirect=True)
def test_aurora_sample_details(rendered: RenderedApp) -> None:
    """The search face and the rail count follow the sample's own rules."""
    page = ready(rendered)
    face = page.locator("#search").evaluate("e=>getComputedStyle(e).fontFamily")
    # The sample sets no face on the search input, so it keeps the browser default.
    default = page.evaluate("""() => {
      const host=document.createElement('div');document.body.append(host);
      const input=document.createElement('input');host.attachShadow({mode:'open'}).append(input);
      const face=getComputedStyle(input).fontFamily;host.remove();return face;
    }""")
    assert face == default
    rendered.login(rendered.world.owner.subject)
    ready(rendered, "/a/inbox")
    count = page.locator(".rail nav .count")
    expect(count).to_have_count(1)
    style = count.evaluate("""e=>{const s=getComputedStyle(e);
      return [s.paddingTop,s.paddingInlineStart,s.borderTopLeftRadius,s.fontVariantNumeric];}""")
    assert style == ["0px", "0px", "0px", "tabular-nums"]
