"""Contract 18e: all thirteen brief checks at Retina resolution; golden via Make."""

from datetime import datetime
from functools import partial
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page, Route, expect
from store.account_fixtures import ADMIN, PATIENT

from browser.conftest import RenderedApp

ROOT = Path(__file__).parents[2]
IDENTITY = Path(__file__).with_name("identity_walk.js").read_text()
MATRIX = [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")]
RETINA = pytest.mark.parametrize("rendered", MATRIX, indirect=True)


def ready(app: RenderedApp, path: str = "/demo") -> Page:
    page = app.page
    width = page.viewport_size["width"] if page.viewport_size else 1440
    page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
    page.clock.set_fixed_time(datetime.fromisoformat("2026-09-11T20:59:00+00:00"))
    response = page.goto(app.origin + path)
    assert response.ok if response else page.url == app.origin + path and "#" in path
    if path.startswith("/demo#"):
        page.get_by_role("tab", name="Medicines", exact=True).wait_for()
    page.locator('#content[aria-busy="false"]').wait_for()
    page.evaluate("document.fonts.ready")
    expect(page.locator("#feedback")).to_be_empty()
    assert page.evaluate("devicePixelRatio") == 2
    return page


def identity(page: Page) -> None:
    assert page.evaluate(IDENTITY) == []
    # Extend, without modifying, the accepted identity walk with the phone/link rail.
    assert (
        page.evaluate(r"""() => {
      const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT), text=[];
      while(walk.nextNode())if(!walk.currentNode.parentElement.closest('script,style,details.support'))
        text.push(walk.currentNode.textContent);
      for(const e of document.querySelectorAll('[aria-label],[title]'))
        if(!e.closest('details.support'))text.push(e.getAttribute('aria-label'),e.title);
      return text.filter(Boolean).filter(t=>
        /(?:\+?20)?01[0125]\d{8}|@\w+bot|\/[dap]\/[^\s]+/i.test(t));
    }""")
        == []
    )


@RETINA
def test_depth_screenshots_and_computed_surfaces(rendered: RenderedApp, tmp_path: Path) -> None:
    ready(rendered)  # Load the shipped swap fonts before the visual comparison.
    page = ready(rendered)
    expect(page.locator(".identity > svg.sanad-lockup")).to_have_count(1)
    expect(page.locator(".identity > .visually-hidden")).to_have_text("Sanad")
    theme = page.locator("html").get_attribute("data-theme")
    assert theme in {"dark", "light"}
    assert page.locator(".work-surface").evaluate("e=>getComputedStyle(e).boxShadow") != "none"
    assert (
        page.locator(".work-surface").evaluate("e=>getComputedStyle(e).backdropFilter")
        == "blur(18px)"
    )
    expect(page.locator(".top-bar")).to_have_count(0)
    expect(page.locator(".page-heading")).to_have_count(1)
    expect(page.locator(".summary-tile").first).to_contain_text("Emergency")
    expect(page.locator(".summary-tile").first).to_have_class(
        __import__("re").compile(r"\bsummary-tile\b.*\bdanger\b")
    )
    expect(page.locator(".sort-chevron")).to_have_count(0)
    assert page.locator(".summary-tile strong,.patient-row time,.kv b").evaluate_all(
        "es=>es.every(e=>getComputedStyle(e).fontVariantNumeric.includes('tabular-nums'))"
    )
    assert page.locator(".rail").evaluate("e=>getComputedStyle(e).boxShadow") == "none"
    viewport = page.viewport_size
    assert viewport
    size = f"{viewport['width']}x{viewport['height']}"
    output = tmp_path / "design-shots-18g"
    output.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(350)  # Finish the bounded eight-row entrance before capture.
    for prefix, full in (("list", False), ("list-full", True)):
        name = f"{prefix}-{size}-{theme}.png"
        page.screenshot(path=str(output / name), full_page=full)
        with Image.open(output / name) as shot:
            assert shot.width == viewport["width"] * 2
            assert shot.height >= viewport["height"] * 2
    name = page.locator(".patient-row .who2 b").first.inner_text()
    page.locator(".patient-row").first.click()
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page.locator("#title")).to_have_text(name)
    page.wait_for_timeout(300)
    name = f"record-{size}-{theme}.png"
    page.screenshot(path=str(output / name))
    with Image.open(output / name) as shot:
        assert shot.size == (viewport["width"] * 2, viewport["height"] * 2)
    identity(page)


@RETINA
def test_depth_summary_filters_chips_and_primary(rendered: RenderedApp) -> None:
    page = ready(rendered)
    page.locator('#filter [data-filter="all"]').click()
    expect(page.locator(".patient-row .status")).to_have_count(0)
    assert page.locator(".patient-row.danger").count()
    assert page.locator(".patient-row.warning,.patient-row.calm").count()
    expect(page.locator("#refresh")).not_to_have_class("primary")
    counts = {}
    for key in ("needs", "all", "settled"):
        page.locator(f'#filter [data-filter="{key}"]').click()
        expect(page.locator('#filter [aria-pressed="true"]')).to_have_attribute("data-filter", key)
        counts[key] = page.locator(".patient-row").count()
    assert counts["needs"] + counts["settled"] == counts["all"] == 18
    for path in ("/a", "/a/inbox", "/a/history", "/a/preferences"):
        ready(rendered, path)
        if path == "/a":
            calm = page.locator('[data-summary="danger"]')
            expect(calm.locator("strong")).to_have_text("0")
            expect(calm.locator(".tile-clause")).to_have_text("nothing urgent right now")
            assert not calm.evaluate("e=>e.classList.contains('danger')")
        assert page.locator("#content button.primary").count() <= 1
        assert "primary" not in (page.locator("#refresh").get_attribute("class") or "").split()
    rendered.detail()
    assert page.locator("#content button.primary").count() <= 1


@RETINA
def test_depth_font_and_theme_layout_stability(rendered: RenderedApp) -> None:
    page = rendered.page
    pending: list[Route] = []

    def hold_font(route: Route) -> None:
        pending.append(route)

    for path in ("/a", f"/a/patients/{rendered.world.patient_scope.patient_id}"):
        pending.clear()
        page.route("**/*.ttf", hold_font)
        page.goto(rendered.origin + path, wait_until="domcontentloaded")
        page.locator('#content[aria-busy="false"]').wait_for()
        page.wait_for_timeout(400)
        assert pending, "The font-delay scenario must actually intercept fonts"
        page.evaluate("""() => {
          window.fontShifts=[];
          window.shiftObserver=new PerformanceObserver(list=>{
            for(const entry of list.getEntries())window.fontShifts.push({value:entry.value,
              sources:entry.sources.map(s=>s.node?.tagName)});
          });
          window.shiftObserver.observe({type:'layout-shift'});
        }""")
        for route in pending:
            route.fulfill(path=ROOT / "src/sanad/web/static" / route.request.url.rsplit("/", 1)[1])
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(250)
        assert page.evaluate("""() => [...document.fonts].filter(f=>
          ['Inter','Playfair'].includes(f.family)).every(f=>f.status==='loaded'&&f.display==='swap')""")
        assert page.evaluate("document.documentElement.style.getPropertyValue('--font-ui')") == ""
        assert (
            page.evaluate("document.documentElement.style.getPropertyValue('--font-display')") == ""
        )
        page.evaluate("window.shiftObserver.takeRecords();window.fontShifts=[]")
        page.wait_for_function("""() => [...document.querySelectorAll('.rv.in')].every(e=>
          !e.getAnimations().some(a=>a.playState==='running' &&
            ['transform','opacity','filter'].includes(a.transitionProperty)))""")
        # Theme changes must not resize any rendered element, including native controls.
        geometry = """() => [...document.querySelectorAll('body *')].filter(e=>{
          if(e.matches('.aurora i'))return false;
          for(let p=e;p;p=p.parentElement)if(p.matches('.rv') &&
            p.getAnimations().some(a=>
              a.playState==='running' && ['transform','opacity','filter']
                .includes(a.transitionProperty)))return false;
          return true;
        }).map(e=>{const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height];})"""
        before = page.evaluate(geometry)
        for theme in ("dark", "light"):
            page.locator(f'[data-theme-set="{theme}"]').click()
            page.wait_for_timeout(250)
            after = page.evaluate(geometry)
            assert after == before, page.locator("body *").evaluate_all(
                "(es,changes)=>changes.map(([i,a,b])=>[es[i].tagName,es[i].className,a,b])",
                [[i, a, b] for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b],
            )
            assert page.evaluate("window.fontShifts") == [], page.evaluate(
                "JSON.stringify(window.fontShifts)"
            )
        page.evaluate("window.shiftObserver.disconnect()")
        page.unroute("**/*.ttf")


@RETINA
@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_depth_identity_all_routes(rendered: RenderedApp) -> None:
    for path in (
        "/a",
        "/a/inbox",
        "/a/inbox#questions",
        "/a/history",
        "/a/preferences",
        "/demo",
        "/demo#demo-000",
    ):
        identity(ready(rendered, path))
    rendered.detail()
    for tab in ("Medicines", "Requests", "Documents", "History"):
        rendered.page.get_by_role("tab", name=tab, exact=True).click()
        identity(rendered.page)
    rendered.login(PATIENT)
    identity(ready(rendered, "/pp"))
    expect(rendered.page.locator("details.support")).to_have_count(0)
    rendered.login(ADMIN)
    rendered.page.goto(rendered.origin + "/admin")
    rendered.page.locator("#admin-applications .summary-strip").wait_for()
    identity(rendered.page)


@RETINA
@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_depth_keyboard_and_motion(rendered: RenderedApp) -> None:
    page = ready(rendered)
    page.emulate_media(reduced_motion="reduce")

    def motionless() -> None:
        assert (
            page.evaluate("""() => [...document.querySelectorAll('body *')].flatMap(e=>
          [null,'::before','::after'].map(p=>getComputedStyle(e,p))).filter(s=>
          s.transitionDuration.split(',').some(d=>parseFloat(d)>.001)||
          s.animationDuration.split(',').some(d=>parseFloat(d)>.001)).length""")
            == 0
        )

    motionless()
    if page.viewport_size and page.viewport_size["width"] == 390:
        ready(rendered, "/a/preferences")
        expect(page.locator("#navigation")).to_be_visible()
        page.locator(".rail nav a").last.scroll_into_view_if_needed()
        assert page.locator(".rail nav").evaluate("e=>e.scrollLeft") > 0
        page.get_by_role("link", name="Patients", exact=True).click()
        expect(page).to_have_url(rendered.origin + "/a")
        for selector, count in ((".rail .foot", 1), (".toggle", 1), (".toggle button", 2)):
            expect(page.locator(selector)).to_have_count(count)
            assert page.locator(selector).evaluate_all(
                """es=>es.every(e=>{const r=e.getBoundingClientRect();
                  return r.left>=0 && r.right<=innerWidth &&
                    r.top>=0 && r.bottom<=innerHeight})"""
            )
        assert page.locator(".rail").evaluate("e=>getComputedStyle(e).flexDirection") == "row"
        ready(rendered)
    page.locator(".skip").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#workspace")).to_be_focused()
    rows = page.locator(".patient-row")
    rows.first.focus()
    page.keyboard.press("ArrowDown")
    expect(rows.nth(1)).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(rows.first).to_be_focused()
    page.keyboard.press("Enter")
    expect(rows.first).to_have_attribute("aria-expanded", "true")
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page.locator("#what-to-do")).to_be_visible()
    expect(page.locator("#patient-drawer")).to_have_count(0)
    motionless()
    rendered.detail()
    first = page.get_by_role("tab", name="Medicines", exact=True)
    first.focus()
    for key, tab in (
        ("ArrowRight", "Requests"),
        ("End", "History"),
        ("Home", "Medicines"),
        ("ArrowLeft", "History"),
    ):
        page.keyboard.press(key)
        expect(page.get_by_role("tab", name=tab, exact=True)).to_be_focused()
        expect(page.get_by_role("tab", name=tab, exact=True)).to_have_attribute(
            "aria-selected", "true"
        )
    motionless()
    first.click()
    opener = page.locator("[data-correct-fact]").first
    opener.click()
    expect(page.locator("#correction-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#correction-dialog")).not_to_be_visible()
    expect(opener).to_be_focused()


def fits(page: Page) -> None:
    """Inspect rendered boxes as well as document width; clipping is not a pass."""
    from browser.aurora18h import reveal_all, settle_paint

    page.evaluate("document.fonts.ready")
    reveal_all(page)
    settle_paint(page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    failures = page.evaluate("""() => [...document.querySelectorAll('body *')].flatMap(e=>{
      if(!e.checkVisibility({checkVisibilityCSS:true})||
         e.closest('.skip,.icon-sprite,.aurora,.spark,.beacon'))return [];
      const r=e.getBoundingClientRect();if(!r.width||!r.height)return [];
      const scroll=e.closest('.tabs,.rail nav,table,.seg');
      if(scroll&&scroll!==e&&getComputedStyle(scroll).overflowX==='auto')return [];
      return r.left<-.5||r.right>innerWidth+.5?[e.tagName,e.className,r.left,r.right]:[];
    })""")
    assert failures == [], failures
    assert page.locator(".tabs").evaluate_all(
        "es=>es.every(e=>getComputedStyle(e).overflowX==='auto')"
    )
    # As in the sample, the rail nav scrolls only at <=900px; wider, it paints the active bar.
    nav = "auto" if page.evaluate("innerWidth") <= 900 else "visible"
    assert page.locator(".rail nav").evaluate_all(
        f"es=>es.every(e=>getComputedStyle(e).overflowX==='{nav}')"
    )


@RETINA
def test_depth_queue_density_and_groups(rendered: RenderedApp) -> None:
    page = ready(rendered)
    page.locator('#filter [data-filter="all"]').click()
    groups = page.locator(".day-group")
    expect(groups).to_have_count(0)
    expect(page.locator(".patient-row")).to_have_count(18)
    assert page.locator("main").evaluate("e=>getComputedStyle(e).maxInlineSize") == "1180px"
    if page.viewport_size and page.viewport_size["width"] == 1440:
        assert (
            page.locator(".patient-row").first.evaluate("e=>getComputedStyle(e).display") == "grid"
        )
        assert (
            page.locator(".patient-row .who2 b").first.evaluate("e=>getComputedStyle(e).fontSize")
            == "14.5px"
        )
    else:
        from browser.aurora18h import normalized_fold

        normalized_fold(page)
        assert (
            page.locator(".patient-row .need").first.evaluate("e=>getComputedStyle(e).gridColumn")
            == "1 / -1"
        )
    fits(page)


@RETINA
@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_depth_route_geometry_and_empty_anatomy(rendered: RenderedApp) -> None:
    for path in ("/demo", "/a", "/a/inbox", "/a/history", "/a/preferences"):
        page = ready(rendered, path)
        fits(page)
        assert page.locator(".empty").evaluate_all("""es=>es.every(e=>
          e.querySelector('.icon')&&e.querySelector('.empty-title')&&e.querySelector('.empty-next'))
        """)
    ready(rendered, "/demo")
    page.locator("#search").fill("Nobody matches this synthetic search")
    expect(page.locator(".empty [data-clear]")).to_be_visible()
    page.locator(".empty [data-clear]").click()
    expect(page.locator(".patient-row")).to_have_count(18)
    rendered.detail()
    for tab in ("Medicines", "Requests", "Documents", "History"):
        page.get_by_role("tab", name=tab, exact=True).click()
        fits(page)
        identity(page)
        assert page.locator(".tabs").evaluate("e=>getComputedStyle(e).display") == "flex"
        assert page.locator(".record-item .provenance,.record-item .record-meta").evaluate_all(
            "es=>es.every(e=>getComputedStyle(e).fontSize==='12.5px')"
        )
    rendered.login(PATIENT)
    ready(rendered, "/pp")
    page.locator('[role="tab"][aria-controls="patient-settings"]').click()
    page.locator('#patient-stop[aria-checked="true"]').wait_for()
    fits(page)
    assert page.locator("main").evaluate("e=>getComputedStyle(e).maxInlineSize") == "1180px"
    track = page.locator(".switch-track").bounding_box()
    thumb = page.locator(".switch-track>span").bounding_box()
    assert track and thumb and (track["width"], track["height"]) == (44, 26)
    assert (thumb["width"], thumb["height"]) == (20, 20)
    assert page.locator(".identity").evaluate(
        "e=>!getComputedStyle(e).fontFamily.includes('Playfair')"
    )
    rendered.login(ADMIN)
    page.goto(rendered.origin + "/admin")
    page.locator("#admin-applications .summary-strip").wait_for()
    fits(page)
    identity(page)
    page.goto(rendered.origin + "/a")
    expect(page.locator("main")).to_contain_text("Not available from an administrator session.")
    fits(page)
    identity(page)
    rendered.errors[:] = [e for e in rendered.errors if "403" not in e]


@RETINA
@pytest.mark.parametrize("world", ["medication"], indirect=True)
def test_depth_record_loading_and_retry(rendered: RenderedApp) -> None:
    page = ready(rendered, "/a?filter=all")
    page.locator(".patient-row").first.click()
    anchor = page.locator(".patient-row.open + .detail a.primary")
    held: list[Route] = []
    pattern = "**/api/patients/*/evidence"
    page.route(pattern, lambda route: held.append(route))
    anchor.click()
    expect(page.locator("#feedback .skeleton")).to_be_visible()
    assert held
    held.pop().fulfill(status=503, content_type="application/json", body='{"detail":"unavailable"}')
    expect(page.locator("#feedback .error")).to_be_visible()
    page.unroute(pattern)
    page.locator("#refresh").click()
    page.locator("#what-to-do").wait_for()
    expect(page.locator("#patient-drawer")).to_have_count(0)
    expect(page.locator("[data-amend]")).to_be_visible()
    fits(page)
    rendered.errors[:] = [e for e in rendered.errors if "503" not in e]


@RETINA
@pytest.mark.parametrize("world", ["evidence"], indirect=True)
def test_depth_lightbox_anatomy_and_both_openers(rendered: RenderedApp) -> None:
    import base64

    rendered.detail()
    page = rendered.page
    page.get_by_role("tab", name="Documents", exact=True).click()
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7XkAAAAASUVORK5CYII="
    )
    page.route("**/media/*", lambda r: r.fulfill(body=image_bytes, content_type="image/png"))
    assert page.locator("[data-media]").count() >= 2
    for anchor in page.locator("[data-media]").all():
        anchor.click()
        expect(page.locator(".lightbox img")).to_be_visible()
        expect(page.locator(".app")).to_have_css("visibility", "visible")
        expect(page.locator(".lightbox-footer")).to_contain_text(
            "Original document", ignore_case=True
        )
        assert (
            page.locator(".lightbox-header").evaluate("e=>e.getBoundingClientRect().height") == 48
        )
        fits(page)
        identity(page)
        page.keyboard.press("Escape")
        expect(anchor).to_be_focused()
    page.unroute("**/media/*")
    page.route("**/media/*", lambda r: r.fulfill(status=403, body=""))
    anchor.click()
    expect(page.locator(".lightbox .empty-title")).to_have_text("Original unavailable.")
    expect(page.locator(".app")).to_have_css("visibility", "visible")
    fits(page)
    page.locator('[aria-label="Close original document"]').click()
    expect(anchor).to_be_focused()
    rendered.errors[:] = [e for e in rendered.errors if "403" not in e]


@RETINA
def test_depth_entry_shells(rendered: RenderedApp) -> None:
    """Exercise exact server shells, including the script-free credential surface."""
    from sanad.web import pages

    page = rendered.page
    for path in rendered.entry_paths:
        response = page.goto(rendered.origin + path)
        assert response and response.status == 200
        page.evaluate("document.fonts.ready")
        expect(page.locator("script")).to_have_count(0)
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        expect(page.locator('meta[name="color-scheme"]')).to_have_attribute("content", "dark")
        fits(page)
        identity(page)

    def serve_entry(html: str, route: Route) -> None:
        route.fulfill(content_type="text/html", body=html)

    for html in (
        pages.continue_page("/d/synthetic", "synthetic", "en"),
        pages.refused_page("wrong_account"),
        pages.invitation_page("Synthetic doctor", "/invite/synthetic", "en"),
        pages.admin_denied_page(),
        pages.admin_entry_page(),
    ):
        page.route("**/entry-proof", partial(serve_entry, html))
        page.goto(rendered.origin + "/entry-proof")
        page.evaluate("document.fonts.ready")
        fits(page)
        identity(page)
        assert page.locator("main").evaluate("e=>getComputedStyle(e).maxInlineSize") == "440px"
        assert page.locator("main").evaluate("e=>getComputedStyle(e).boxShadow") != "none"
        if "<script" not in html:
            expect(page.locator("script")).to_have_count(0)
        for button in page.locator('button[type="submit"]').all():
            assert "linear-gradient" in button.evaluate("e=>getComputedStyle(e).backgroundImage")
        page.unroute("**/entry-proof")


@RETINA
@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_depth_rtl_and_forced_colors(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    page = ready(rendered, "/a")
    expect(page.locator("html")).to_have_attribute("dir", "ltr")
    rendered.login(PATIENT)
    ready(rendered, "/pp")
    expect(page.locator("html")).to_have_attribute("dir", "rtl")
    fits(page)
    if page.viewport_size and page.viewport_size["width"] == 1440:
        rail = page.locator(".rail").bounding_box()
        main = page.locator("main").bounding_box()
        assert rail and main and rail["x"] > main["x"]
    assert page.locator("h1,label,th").evaluate_all(
        "es=>es.every(e=>getComputedStyle(e).letterSpacing==='normal')"
    )
    rendered.login(rendered.world.owner.subject)
    rendered.detail()
    page.get_by_role("tab", name="Medicines", exact=True).focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Requests", exact=True)).to_be_focused()
    fits(page)
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    assert (
        page.locator('.tabs [aria-selected="true"]').evaluate(
            "e=>getComputedStyle(e).borderBottomStyle"
        )
        == "solid"
    )
    assert page.locator(".section,.record-item,.status").evaluate_all("""es=>es.every(e=>
      getComputedStyle(e).borderTopStyle==='solid'&&getComputedStyle(e).boxShadow==='none')""")
    fits(page)


@RETINA
def test_depth_form_fields_and_pending_state(rendered: RenderedApp) -> None:
    page = ready(rendered, "/a/preferences")
    form = page.locator("#digest-form")
    field = page.locator("#digest-time")
    field.fill("")
    form.get_by_role("button", name="Save digest").click()
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_attribute("aria-describedby", "digest-result")
    expect(page.locator("#digest-result")).to_have_text("Choose a valid daily time.")
    field.fill("19:30")
    expect(field).not_to_have_attribute("aria-invalid", "true")
    assert field.evaluate("e=>getComputedStyle(e).borderTopWidth") == "0px"
    held: list[Route] = []
    page.route("**/api/preferences", lambda r: held.append(r))
    form.get_by_role("button", name="Save digest").click()
    expect(form.get_by_role("button", name="Saving…")).to_be_disabled()
    fits(page)
    assert held
    held.pop().fulfill(status=409, content_type="application/json", body='{"detail":"stale"}')
    expect(form.get_by_role("button", name="Save digest")).to_be_enabled()
    expect(page.locator("#digest-result")).to_contain_text("changed in another tab")
    page.unroute("**/api/preferences")
    rendered.errors[:] = [e for e in rendered.errors if "409" not in e]
