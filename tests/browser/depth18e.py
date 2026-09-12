"""Contract 18e: all thirteen brief checks at Retina resolution; golden via Make."""

from datetime import datetime
from functools import partial
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page, Route, expect
from store.account_fixtures import ADMIN, PATIENT
from test_dashboard18_contrast import contrast

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
        page.get_by_role("tab", name="Plan", exact=True).wait_for()
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
def test_depth_screenshots_and_computed_surfaces(rendered: RenderedApp) -> None:
    ready(rendered)  # Warm the shipped optional fonts for the visual comparison.
    page = ready(rendered)
    assert page.locator(".identity").evaluate(
        "e=>getComputedStyle(e).fontFamily.startsWith('Playfair')"
    )
    values = page.evaluate("""() => {
      const s=getComputedStyle(document.documentElement);
      return Object.fromEntries(['s0','s2','s3'].map(k=>[k,s.getPropertyValue('--'+k).trim()]));
    }""")
    assert contrast(values["s0"], values["s2"]) >= 1.12
    theme = page.locator("html").get_attribute("data-theme")
    if theme == "dark":
        assert contrast(values["s2"], values["s3"]) > 1
        assert "inset" in page.locator(".top-bar").evaluate("e=>getComputedStyle(e).boxShadow")
    assert page.locator(".work-surface").evaluate("e=>getComputedStyle(e).boxShadow") != "none"
    expect(page.locator(".page-heading")).to_have_count(0)
    assert page.locator(".top-bar").evaluate("e=>e.getBoundingClientRect().height") == 56
    expect(page.locator(".summary-tile").first).to_contain_text("Danger")
    expect(page.locator(".summary-tile").first).to_have_class("summary-tile danger")
    expect(page.locator(".sort-chevron")).to_have_count(6)
    assert page.locator("body,.summary-tile strong,td,time").evaluate_all(
        "es=>es.every(e=>getComputedStyle(e).fontVariantNumeric.includes('tabular-nums'))"
    )
    assert page.locator(".rail").evaluate("e=>getComputedStyle(e).boxShadow") == "none"
    viewport = page.viewport_size
    assert viewport
    size = f"{viewport['width']}x{viewport['height']}"
    output = ROOT / "lane/runs/design-shots-18e-2"
    output.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(350)  # Finish the bounded eight-row entrance before capture.
    for prefix, full in (("list", False), ("list-full", True)):
        name = f"{prefix}-{size}-{theme}.png"
        assert (ROOT / "lane/runs/design-audit-shots" / name).is_file()
        page.screenshot(path=str(output / name), full_page=full)
        with Image.open(output / name) as shot:
            assert shot.width == viewport["width"] * 2
            assert shot.height >= viewport["height"] * 2
    page.locator("[data-record]").first.click()
    expect(page.locator("#drawer-title")).to_contain_text("Ahmed")
    page.wait_for_timeout(300)
    name = f"drawer-{size}-{theme}.png"
    page.screenshot(path=str(output / name))
    with Image.open(output / name) as shot:
        assert shot.size == (viewport["width"] * 2, viewport["height"] * 2)
    identity(page)


@RETINA
def test_depth_summary_filters_chips_and_primary(rendered: RenderedApp) -> None:
    page = ready(rendered)
    colors = page.locator(".status").evaluate_all(
        "es=>[...new Set(es.map(e=>getComputedStyle(e).backgroundColor))]"
    )
    assert len(colors) > 1
    danger = page.locator(".status.danger").first
    quiet = page.locator(".status.quiet").first
    assert danger.count() and quiet.count()
    assert danger.evaluate("e=>getComputedStyle(e).backgroundColor") != quiet.evaluate(
        "e=>getComputedStyle(e).backgroundColor"
    )
    expect(page.locator("#refresh")).not_to_have_class("primary")
    for key in ("danger", "overdue", "pending_review", "due_today"):
        tile = page.locator(f'[data-summary="{key}"]')
        number = int(tile.locator("strong").inner_text())
        assert str(number) in (tile.get_attribute("aria-label") or "")
        tile.click()
        expect(tile).to_have_attribute("aria-pressed", "true")
        expect(tile).to_be_focused()
        expect(page.locator("#filter")).to_have_value(key)
        rows = page.locator(".clinical tbody tr.patient-row")
        assert rows.count() > 0 if number else rows.count() == 0
        if key == "danger":
            assert rows.count() == page.locator(".clinical tbody tr.patient-row.urgent").count()
        tile.click()
        expect(tile).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#filter")).to_have_value("all")
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
        assert page.evaluate("window.fontShifts") == [], page.evaluate(
            "JSON.stringify(window.fontShifts)"
        )
        # Theme changes must not resize any rendered element, including native controls.
        geometry = """() => [...document.querySelectorAll('body *')].map(e=>{
          const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
        })"""
        before = page.evaluate(geometry)
        for theme in ("dark", "light"):
            page.locator("#theme").select_option(theme)
            page.wait_for_timeout(250)
            assert page.evaluate(geometry) == before
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
    for tab in ("Plan", "Requests", "Evidence", "History"):
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
          s.transitionProperty!=='none'||s.animationName!=='none').length""")
            == 0
        )

    motionless()
    if page.viewport_size and page.viewport_size["width"] == 390:
        disclosure = page.locator(".nav-disclosure")
        disclosure.click()
        expect(disclosure).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#navigation")).to_be_visible()
        disclosure.click()
        expect(page.locator("#navigation")).not_to_be_visible()
    page.locator(".skip").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#workspace")).to_be_focused()
    rows = page.locator(".clinical tbody tr.patient-row")
    rows.first.focus()
    page.keyboard.press("ArrowDown")
    expect(rows.nth(1)).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(rows.first).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#patient-drawer")).to_be_visible()
    motionless()
    page.keyboard.press("Escape")
    expect(page.locator("#patient-drawer")).to_have_count(0)
    expect(rows.first.locator("[data-record]")).to_be_focused()
    rendered.detail()
    first = page.get_by_role("tab", name="Plan", exact=True)
    first.focus()
    for key, tab in (
        ("ArrowRight", "Requests"),
        ("End", "History"),
        ("Home", "Plan"),
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


@RETINA
def test_depth_rendered_text_contrast(rendered: RenderedApp) -> None:
    """Measure actual foregrounds over their composited ancestor surfaces, not class names."""
    for path in ("/demo", "/a", "/a/inbox", "/a/history", "/a/preferences"):
        page = ready(rendered, path)
        text_contrast(page)


def text_contrast(page: Page) -> None:
    failures = page.evaluate(r"""() => {
          const rgb=s=>{
            const a=s.match(/[\d.]+/g).map(Number);
            return s.startsWith('color(srgb')?a.map((v,i)=>i<3?v*255:v):a;
          };
          const blend=(a,b)=>a.slice(0,3).map((v,i)=>v*(a[3]??1)+b[i]*(1-(a[3]??1)));
          const lum=a=>a.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4)
            .reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
          const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
          const background=e=>{
            const chain=[];for(let p=e;p;p=p.parentElement)chain.unshift(p);
            return chain.reduce((color,p)=>
              blend(rgb(getComputedStyle(p).backgroundColor),color),[255,255,255]);
          };
          const failed=[],walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
          while(walk.nextNode()){
            const node=walk.currentNode,e=node.parentElement;
            if(!node.textContent.trim()||e.closest('script,style,details.support,option')||
               !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))continue;
            const fg=rgb(getComputedStyle(e).color), bg=background(e);
            const minimum=e.closest(':disabled')?2:4.5;
            if(ratio(fg,bg)<minimum)failed.push({text:node.textContent.trim().slice(0,60),
              element:e.tagName+'.'+e.className,foreground:fg,background:bg,ratio:ratio(fg,bg)});
          }
          return failed;
        }""")
    assert failures == [], failures


def fits(page: Page) -> None:
    """Inspect rendered boxes as well as document width; clipping is not a pass."""
    page.wait_for_timeout(300)
    if not page.evaluate("matchMedia('(forced-colors:active)').matches"):
        text_contrast(page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    failures = page.evaluate("""() => [...document.querySelectorAll('body *')].flatMap(e=>{
      if(!e.checkVisibility({checkVisibilityCSS:true})||e.closest('.skip,.icon-sprite'))return [];
      const r=e.getBoundingClientRect();if(!r.width||!r.height)return [];
      const scroll=e.closest('.tabs,.rail nav,table');
      if(scroll&&scroll!==e&&getComputedStyle(scroll).overflowX==='auto')return [];
      return r.left<-.5||r.right>innerWidth+.5?[e.tagName,e.className,r.left,r.right]:[];
    })""")
    assert failures == [], failures
    for selector in (".tabs", ".rail nav"):
        assert page.locator(selector).evaluate_all(
            "es=>es.every(e=>getComputedStyle(e).overflowX==='auto')"
        )


@RETINA
def test_depth_queue_density_and_groups(rendered: RenderedApp) -> None:
    page = ready(rendered)
    groups = page.locator(".day-group")
    assert groups.count() >= 5
    assert groups.evaluate_all("es=>es.every(e=>!e.hasAttribute('tabindex'))")
    assert sum(int(n) for n in groups.locator(".count").all_text_contents()) == 50
    assert groups.locator('th[scope="rowgroup"][colspan="6"]').count() == groups.count()
    assert groups.locator("th").evaluate_all(
        "es=>es.every(e=>getComputedStyle(e).position==='sticky')"
    )
    values = page.locator(".age-value")
    assert values.evaluate_all("""es=>es.every(e=>{
      const s=getComputedStyle(e);return s.whiteSpace==='nowrap'&&
        Math.abs(e.scrollHeight-parseFloat(s.lineHeight))<=.5&&e.scrollWidth<=e.clientWidth;
    })""")
    assert "Missing" not in values.all_text_contents()
    assert page.locator("main").evaluate("e=>getComputedStyle(e).maxInlineSize") == "1280px"
    assert page.locator(".clinical").evaluate("e=>getComputedStyle(e).tableLayout") == "fixed"
    if page.viewport_size and page.viewport_size["width"] == 1440:
        table = page.locator(".clinical").bounding_box()
        age = page.locator(".clinical col").nth(1).bounding_box()
        assert table and age and abs(age["width"] / table["width"] - 0.08) < 0.001
        assert (
            page.locator(".patient-row td").first.evaluate("e=>getComputedStyle(e).fontSize")
            == "14px"
        )
    else:
        first = page.locator(".patient-row").first.bounding_box()
        assert first and first["y"] < 844, first
    fits(page)
    page.evaluate("""() => scrollTo(0,
      document.querySelector('.day-group').getBoundingClientRect().top+scrollY+250)""")
    page.wait_for_timeout(100)
    band = groups.first.locator("th").bounding_box()
    expected_top = 200 if page.viewport_size and page.viewport_size["width"] == 390 else 96
    assert band and abs(band["y"] - expected_top) <= 1, band
    page.evaluate("scrollTo(0,0)")
    dates = groups.locator("th > bdi").all_text_contents()
    assert dates == sorted(dates, key=lambda d: datetime.strptime(d, "%b %d, %Y"))
    page.locator('[data-sort="due"]').click()
    dates = groups.locator("th > bdi").all_text_contents()
    assert dates == sorted(dates, key=lambda d: datetime.strptime(d, "%b %d, %Y"), reverse=True)
    page.locator('[data-sort="patient"]').click()
    expect(groups).to_have_count(0)
    page.locator('[data-sort="last_activity"]').click()
    expect(page.locator('th[aria-sort="ascending"]')).to_contain_text("Last activity")
    expect(groups).to_have_count(0)
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
    expect(page.locator("[data-record]")).to_have_count(50)
    rendered.detail()
    for tab in ("Plan", "Requests", "Evidence", "History"):
        page.get_by_role("tab", name=tab, exact=True).click()
        fits(page)
        identity(page)
        assert page.locator(".tabs").evaluate("e=>getComputedStyle(e).display") == "flex"
        assert page.locator(".record-item .provenance,.record-item .record-meta").evaluate_all(
            "es=>es.every(e=>getComputedStyle(e).fontSize==='13px')"
        )
    rendered.login(PATIENT)
    ready(rendered, "/pp")
    page.locator('#patient-stop[aria-checked="true"]').wait_for()
    fits(page)
    assert page.locator("main").evaluate("e=>getComputedStyle(e).maxInlineSize") == "760px"
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
def test_depth_drawer_anatomy_and_retry(rendered: RenderedApp) -> None:
    page = ready(rendered, "/a")
    anchor = page.locator("[data-record]").first
    held: list[Route] = []
    pattern = "**/api/patients/*/evidence"
    page.route(pattern, lambda route: held.append(route))
    anchor.click()
    drawer = page.locator("#patient-drawer")
    expect(drawer.locator(".skeleton")).to_be_visible()
    expect(anchor.locator("xpath=ancestor::tr")).to_have_attribute("aria-selected", "true")
    assert held
    held.pop().fulfill(status=503, content_type="application/json", body='{"detail":"unavailable"}')
    expect(drawer.locator(".error")).to_be_visible()
    page.unroute(pattern)
    drawer.locator("[data-retry]").click()
    drawer.locator(".inline-stats").wait_for()
    expect(drawer.locator(".summary-tile")).to_have_count(0)
    expect(drawer.locator(".inline-stats>span")).to_have_count(4)
    expect(drawer.locator(".drawer-footer a.primary")).to_have_count(1)
    expect(drawer.locator("[data-amend]")).to_be_visible()
    assert drawer.locator(".drawer-content").evaluate("e=>getComputedStyle(e).overflowY") == "auto"
    page.wait_for_timeout(300)
    footer = drawer.locator("footer").bounding_box()
    drawer.locator(".drawer-content").evaluate("e=>e.scrollTop=e.scrollHeight")
    assert drawer.locator("footer").bounding_box() == footer
    fits(page)
    if page.viewport_size and page.viewport_size["width"] == 390:
        assert drawer.evaluate("e=>getComputedStyle(e).animationName") == "sheet-in"
        assert drawer.bounding_box()["width"] == 390  # type: ignore[index]
    page.keyboard.press("Escape")
    expect(anchor).to_be_focused()
    expect(anchor.locator("xpath=ancestor::tr")).not_to_have_attribute("aria-selected", "true")
    rendered.errors[:] = [e for e in rendered.errors if "503" not in e]


@RETINA
@pytest.mark.parametrize("world", ["evidence"], indirect=True)
def test_depth_lightbox_anatomy_and_both_openers(rendered: RenderedApp) -> None:
    import base64

    rendered.detail()
    page = rendered.page
    page.get_by_role("tab", name="Evidence", exact=True).click()
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7XkAAAAASUVORK5CYII="
    )
    page.route("**/media/*", lambda r: r.fulfill(body=image_bytes, content_type="image/png"))
    assert page.locator("[data-media]").count() >= 2
    for anchor in page.locator("[data-media]").all():
        anchor.click()
        expect(page.locator(".lightbox img")).to_be_visible()
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
            assert button.evaluate("e=>getComputedStyle(e).backgroundColor") == "rgb(94, 106, 210)"
        page.unroute("**/entry-proof")


@RETINA
@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_depth_rtl_and_forced_colors(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    # The stored fixture doctor is Arabic; exercise the server's locale resolution.
    page = ready(rendered, "/a")
    expect(page.locator("html")).to_have_attribute("dir", "rtl")
    fits(page)
    if page.viewport_size and page.viewport_size["width"] == 1440:
        rail = page.locator(".rail").bounding_box()
        main = page.locator("main").bounding_box()
        assert rail and main and rail["x"] > main["x"]
    assert page.locator("h1,label,th").evaluate_all(
        "es=>es.every(e=>getComputedStyle(e).letterSpacing==='normal')"
    )
    anchor = page.locator("[data-record]").first
    anchor.click()
    page.locator(".inline-stats").wait_for()
    drawer = page.locator(".drawer")
    page.wait_for_timeout(300)
    assert drawer.bounding_box()["x"] == 0  # type: ignore[index]
    assert drawer.evaluate("e=>getComputedStyle(e).animationName") == (
        "sheet-in" if page.viewport_size and page.viewport_size["width"] == 390 else "drawer-rtl"
    )
    fits(page)
    page.keyboard.press("Escape")
    rendered.detail()
    page.get_by_role("tab", name="Plan", exact=True).focus()
    page.keyboard.press("ArrowLeft")
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
