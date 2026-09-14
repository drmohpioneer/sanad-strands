"""Manual synthetic readability observations; never a test or a colour policy.

Run only with make readability-report. Report failures are written as coverage gaps,
never exit failures. Product files and the private approved sample stay read-only.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import uvicorn
from PIL import Image, ImageDraw
from playwright.sync_api import Browser, Page, Route, sync_playwright

ROOT = Path(__file__).parents[2]
OUTPUT = ROOT / "lane/runs/readability-report"
SAMPLE = ROOT / "lane/design-directions-2026-09-12/direction-1/index.html"
# The command runs as a script, outside pytest collection or conftest fixtures.
sys.path.insert(0, str(ROOT / "tests"))


def rgba(value: str, values: dict[str, str] | None = None) -> tuple[float, ...]:
    value = value.strip()
    if value.startswith("var("):
        if values is None:
            raise ValueError("Unresolved custom colour")
        return rgba(values[value[6:-1]], values)
    if value.startswith("color-mix"):
        match = re.fullmatch(r"color-mix\(in srgb,(.+) ([\d.]+)%,transparent\)", value)
        if match is None:
            raise ValueError("Unsupported colour syntax")
        color = rgba(match[1], values)
        return (*color[:3], color[3] * float(match[2]) / 100)
    if value.startswith("color(srgb"):
        parts = value.removeprefix("color(srgb").removesuffix(")").split("/")
        rgb = tuple(float(c) * 255 for c in parts[0].split())
        return (*rgb, float(parts[1]) if len(parts) == 2 else 1)
    if value.startswith("#") and len(value) == 4:
        value = "#" + "".join(c * 2 for c in value[1:])
    if value.startswith("#"):
        return (*[int(value[i : i + 2], 16) for i in (1, 3, 5)], 1)
    return tuple(map(float, re.findall(r"[\d.]+", value)))


def blend(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    alpha = a[3] if len(a) == 4 else 1
    return (*(a[i] * alpha + b[i] * (1 - alpha) for i in range(3)), 1)


@lru_cache(maxsize=65536)
def luminance(color: str | tuple[float, ...]) -> float:
    channels = rgba(color) if isinstance(color, str) else color
    linear = [
        v / 255 / 12.92 if v / 255 <= 0.04045 else ((v / 255 + 0.055) / 1.055) ** 2.4
        for v in channels[:3]
    ]
    return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def contrast(a: str | tuple[float, ...], b: str | tuple[float, ...]) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def percentile(values: list[tuple[float, int]]) -> float:
    if not values:
        return float("nan")
    rank = math.ceil(sum(n for _, n in values) * 0.05)
    total = 0
    for score, count in sorted(values):
        total += count
        if total >= rank:
            return score
    return float("nan")


def pixel_score(im: Image.Image, foreground: str) -> float:
    colors = im.convert("RGB").getcolors(im.width * im.height)
    if not colors:
        return float("nan")
    return percentile(
        [
            (
                contrast(
                    blend(rgba(foreground), cast(tuple[float, ...], c)), cast(tuple[float, ...], c)
                ),
                n,
            )
            for n, c in colors
        ]
    )


def pause_aurora(page: Page, keyframe: str) -> None:
    page.emulate_media(reduced_motion="no-preference")
    page.evaluate(
        """async key => {
      const results=[];
      for(const [i,e] of [...document.querySelectorAll('.aurora i')].entries()){
        const a=e.getAnimations().find(a=>a.animationName==='drift');
        if(!a)throw Error('Missing drift animation');
        a.pause();await a.ready;
        const t=a.effect.getTiming();
        a.currentTime=t.delay+t.duration*(key==='from'?2:1);
        const m=new DOMMatrixReadOnly(getComputedStyle(e).transform);
        if(!m.is2D)throw Error('Drift must have the specified 2D transform');
        results.push([a.currentTime,m.a,m.b,m.c,m.d,m.e,m.f]);
      }
      return results;
    }""",
        keyframe,
    )
    # Freeze independent pulses as well: the two difference images need one scene.
    page.evaluate("""async () => {
      for(const a of document.getAnimations()){
        if(a.animationName==='drift')continue;
        if(a.effect.getTiming().iterations===Infinity){a.pause();await a.ready;}
      }
    }""")


def settle_paint(page: Page) -> None:
    page.wait_for_function("""() => document.getAnimations().every(a=>
      a.playState!=='running' || a.effect.getTiming().iterations===Infinity)""")


def measure_text(page: Page) -> list[dict[str, Any]]:
    """Foreground first; screenshot each inset client rect on the rendered stage.

    Crops share a viewport screenshot only while the scroll position is identical.
    Every CSS pixel of every rect is still represented at DPR 2, without resampling.
    """
    reveal_all(page)
    page.mouse.move(0, 0)
    settle_paint(page)
    entries = page.evaluate(
        r"""() => {
      const modal=document.querySelector('dialog:modal');
      const former='.identity,.rail nav a,button,.button,.av';
      window.pixelElements=[...(modal||document.body).querySelectorAll('*')].filter(e=>
        (e.matches('input,select,textarea,.summary-tile strong')||[...e.childNodes].some(n=>
          n.nodeType===Node.TEXT_NODE&&n.textContent.trim())) &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) &&
        !e.matches('script,style'));
      // Each direct text node has one owner; descendants are never counted twice.
      window.pixelRuns=window.pixelElements.flatMap(e=>{
        const rangeTarget=e.matches(former)?e:null;
        const eligible=rangeTarget && (document.body.dataset.audience!=='patient' ||
          rangeTarget.matches('#theme button'));
        if(!eligible)return [{e,node:null}];
        if(e.closest('.visually-hidden'))return [];
        return [...e.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim())
          .map(node=>({e,node}));
      });
      return window.pixelRuns.map(({e,node},index)=>{
        const s=getComputedStyle(e), r=e.getBoundingClientRect();
        const gradient=s.backgroundClip==='text';
        const stops=gradient?s.backgroundImage.match(/(?:rgba?\([^)]*\)|color\(srgb[^)]*\))/g):null;
        return {element:e.tagName.toLowerCase()+(e.id?'#'+e.id:'.'+e.className),
          foreground:stops||[s.color], gradient,
          instance:window.pixelElements.indexOf(e),
          run:index,
          context:e.parentElement.tagName+'.'+e.parentElement.className+
            (e.parentElement.id?'#'+e.parentElement.id:'')+
            (e.parentElement.dataset.filter?'[data-filter='+e.parentElement.dataset.filter+']':'')+
            (e.parentElement.dataset.themeSet?'[data-theme-set='+e.parentElement.dataset.themeSet+']':''),
          floor:parseFloat(s.fontSize)>=24 ||
            (parseFloat(s.fontSize)>=18.66&&parseFloat(s.fontWeight)>=700)?3:4.5,
          width:r.width,height:r.height};
      });
    }"""
    )
    page.evaluate("""() => {
      window.pixelSheet=new CSSStyleSheet();
      window.pixelSheet.replaceSync(`body *{color:transparent!important;
        -webkit-text-fill-color:transparent!important;text-shadow:none!important}
        [data-pixel-gradient]{visibility:hidden!important}`);
      for(const e of window.pixelElements)
        if(getComputedStyle(e).backgroundClip==='text')e.setAttribute('data-pixel-gradient','');
      document.adoptedStyleSheets=[...document.adoptedStyleSheets,window.pixelSheet];
    }""")
    settle_paint(page)
    pairs: list[dict[str, Any]] = []
    cached_position = None
    shot = None
    try:
        for index, entry in enumerate(entries):
            geometry = page.evaluate(
                """i => {
              const {e,node}=window.pixelRuns[i];
              const initial=e.getBoundingClientRect();
              e.scrollIntoView({block:initial.top<0||initial.bottom>innerHeight?'center':'nearest',
                inline:'nearest',behavior:'instant'});
              let rects;
              if(node){const range=document.createRange();range.selectNodeContents(node);
                rects=[...range.getClientRects()];}
              else rects=[e.getBoundingClientRect()];
              return {rects:rects.map(r=>({x:r.x+1,y:r.y+1,width:r.width-2,height:r.height-2})),
                scroll:[scrollX,scrollY,...[...document.querySelectorAll('.tabs,.rail nav,.seg')]
                  .map(e=>e.scrollLeft)]};
            }""",
                index,
            )
            vp = page.viewport_size
            if vp is None:
                raise ValueError("Measurement needs a viewport")
            boxes = []
            for rect in geometry["rects"]:
                x, y = max(0, rect["x"]), max(0, rect["y"])
                right = min(vp["width"], rect["x"] + rect["width"])
                bottom = min(vp["height"], rect["y"] + rect["height"])
                if right > x and bottom > y:
                    boxes.append(tuple(round(v * 2) for v in (x, y, right, bottom)))
            if not boxes:
                continue
            if cached_position != geometry["scroll"]:
                shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                cached_position = geometry["scroll"]
            if shot is None:
                continue
            if len(boxes) == 1:
                crop = shot.crop(boxes[0])
            else:
                # The mask is the union of individual run rects, never their envelope.
                mask = Image.new("1", shot.size)
                draw = ImageDraw.Draw(mask)
                for x, y, right, bottom in boxes:
                    draw.rectangle((x, y, right - 1, bottom - 1), fill=1)
                pixels = [
                    c
                    for c, keep in zip(
                        shot.get_flattened_data(), mask.get_flattened_data(), strict=True
                    )
                    if keep
                ]
                if not pixels:
                    continue
                crop = Image.new("RGB", (len(pixels), 1))
                crop.putdata(pixels)
            if not crop.width or not crop.height:
                continue
            for fg in entry["foreground"]:
                score = pixel_score(crop, fg)
                pairs.append(
                    {
                        "route": page.url.split(page.url.split("/")[2])[-1],
                        "width": vp["width"],
                        "theme": page.locator("html").get_attribute("data-theme"),
                        "element": entry["element"],
                        "instance": entry["instance"],
                        "run": entry["run"],
                        "context": entry["context"],
                        "foreground": fg,
                        "score": score,
                        "floor": entry["floor"],
                    }
                )
    finally:
        page.evaluate(
            "document.adoptedStyleSheets=document.adoptedStyleSheets.filter(s=>s!==window.pixelSheet)"
        )
        page.evaluate(
            """() => {for(const e of window.pixelElements)
              e.removeAttribute('data-pixel-gradient');}"""
        )
    return pairs


def reveal_all(page: Page) -> None:
    """Scroll the real choreography into view, including long clinical panels."""
    page.evaluate("document.fonts.ready")
    page.evaluate("scrollTo(0,0)")
    while True:
        page.wait_for_timeout(150)
        if page.evaluate("scrollY+innerHeight>=document.documentElement.scrollHeight-1"):
            break
        page.evaluate("scrollBy(0,innerHeight*.75)")
    settle_paint(page)
    page.evaluate("scrollTo(0,0)")


def synthetic_data(scenario: str) -> tuple[Any, dict[str, str]]:
    """Prepare fixtures outside Playwright's running event loop."""
    from domain_fixtures import NOW
    from harness import FakeClock
    from store import evidence_fixtures as f
    from store.account_fixtures import PATIENT
    from store.executors_15_fixtures import question
    from store.login_fixtures import browser_login
    from store.medication_fixtures import confirm, send
    from store.test_admin_boundary import admin_path
    from store.test_corrections_19 import fact_change
    from store.test_monitor import monitor

    from sanad.steward.corrections import current_facts
    from sanad.store.memory import MemoryStore

    clock = FakeClock(NOW)
    world = f.world(MemoryStore(clock=clock), clock)
    if scenario == "monitor":
        monitor(world, count=1)
        world.send("BP 120/80", id=1700)
        fact_change(world, current_facts(world.store, world.patient_scope)[0].id, "BP 130/85")
    elif scenario == "evidence":
        f.mission(world, order_refs=())
        f.providers(world, f.lab(), f.lab())
        f.upload(world)
    elif scenario in {"medication", "patient"}:
        confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
        send(world, "I started Atorvastatin")
    elif scenario == "question":
        question(world)
    with world.client() as client:
        path = (
            admin_path(world)
            if scenario == "admin"
            else world.login_path(
                PATIENT if scenario == "patient" else world.owner.subject, id=9600
            )
        )
        browser_login(client, path)
        cookies = dict(client.cookies)
    return world, cookies


@contextmanager
def synthetic_browser(browser: Browser, scenario: str, width: int, theme: str) -> Iterator[Page]:
    """Independent synthetic application setup; no browser test/score fixtures."""
    from sanad.web.settings import WebSettings

    with ThreadPoolExecutor(max_workers=1) as pool:
        world, cookies = pool.submit(synthetic_data, scenario).result()
    with tempfile.TemporaryDirectory(prefix="sanad-readability-") as temp:
        cert, key = Path(temp) / "cert.pem", Path(temp) / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
            world.app.state.web_settings = WebSettings(
                public_base_url=origin, bot_username="synthetic_sanad_bot"
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    world.app,
                    ssl_keyfile=str(key),
                    ssl_certfile=str(cert),
                    access_log=False,
                    log_level="error",
                    ws="none",
                )
            )
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not server.started:
                    raise RuntimeError("Synthetic HTTPS server unavailable")
                with browser.new_context(
                    ignore_https_errors=True,
                    device_scale_factor=2,
                    viewport={"width": width, "height": 900 if width == 1440 else 844},
                    timezone_id="UTC",
                    color_scheme="dark" if theme == "dark" else "light",
                ) as context:
                    context.add_init_script(
                        "localStorage.setItem('sanad-theme'," + json.dumps(theme) + ");"
                    )
                    context.add_cookies(
                        [
                            {
                                "name": name,
                                "value": value,
                                "url": origin,
                                "secure": True,
                                "sameSite": "Strict",
                            }
                            for name, value in cookies.items()
                        ]
                    )

                    def local_only(route: Route) -> None:
                        if route.request.url.startswith(origin + "/"):
                            route.continue_()
                        else:
                            route.abort()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.clock.set_fixed_time(datetime.fromisoformat("2026-09-11T20:59:00+00:00"))
                    # No credentials or patient content are added to the report URL.
                    page.goto(
                        origin
                        + (
                            "/admin"
                            if scenario == "admin"
                            else "/pp"
                            if scenario == "patient"
                            else "/a"
                        )
                    )
                    page.locator(
                        "#admin-applications .summary-strip"
                        if scenario == "admin"
                        else '#content[aria-busy="false"]'
                    ).wait_for()
                    page.evaluate("document.fonts.ready")
                    yield page
            finally:
                server.should_exit = True
                thread.join(timeout=10)


def report() -> None:
    os.environ["SANAD_CONTEST_ENGLISH"] = "1"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    observations: list[dict[str, Any]] = []
    gaps: list[str] = []
    scenes: list[str] = []

    def capture(page: Page, label: str) -> None:
        for frame in ("from", "to"):
            shot = f"{label}-{frame}.png"
            try:
                reveal_all(page)
                pause_aurora(page, frame)
                # Save the real visible page before any text is masked.
                page.screenshot(path=str(OUTPUT / shot), full_page=True)
                pairs = measure_text(page)
                for pair in pairs:
                    pair.update(scene=label, keyframe=frame, screenshot=shot)
                observations.extend(pairs)
                scenes.append(f"{label}/{frame}: {len(pairs)} text runs")
                if not pairs:
                    gaps.append(f"{label}/{frame}: no visible text measured")
                print(f"Measured {label}/{frame}: {len(pairs)} text runs", flush=True)
            except Exception as error:
                gaps.append(f"{label}/{frame}: measurement unavailable ({type(error).__name__})")

    try:
        with sync_playwright() as driver, driver.chromium.launch(headless=True) as browser:
            for width, theme in ((1440, "dark"), (1440, "light"), (390, "dark"), (390, "light")):
                for scenario in (
                    "empty",
                    "monitor",
                    "medication",
                    "evidence",
                    "question",
                    "patient",
                    "admin",
                ):
                    label = f"{width}-{theme}-{scenario}"
                    try:
                        with synthetic_browser(browser, scenario, width, theme) as page:
                            origin = page.url.split("/a")[0].split("/pp")[0]
                            if scenario == "patient":
                                page.get_by_role("tab", name="Settings", exact=True).click()
                                page.locator('#patient-stop[aria-checked="true"]').wait_for()
                                capture(page, label + "-enabled")
                                page.locator("#patient-stop").click()
                                page.locator('#patient-stop[aria-checked="false"]').wait_for()
                                capture(page, label + "-paused")
                                page.locator("#patient-stop").click()
                                page.locator("#patient-confirm").wait_for()
                                capture(page, label + "-resume-confirmation")
                                page.locator("#patient-quiet-end").fill(
                                    page.locator("#patient-quiet-start").input_value()
                                )
                                page.locator("#patient-quiet-form button").click()
                                capture(page, label + "-invalid-quiet-hours")
                                page.goto(origin + "/demo/patient")
                                page.locator('#content[aria-busy="false"]').wait_for()
                                page.locator("#patient-uploads h3").wait_for()
                                capture(page, label + "-demo-conversation-upload-reading")
                                continue
                            if scenario == "admin":
                                capture(page, label + "-applications")
                                page.goto(origin + "/demo/admin")
                                page.locator(".application-card").first.wait_for()
                                capture(page, label + "-demo-four-states")
                                page.get_by_role("button", name="Reject", exact=True).click()
                                capture(page, label + "-rejection-form")
                                page.get_by_label("Identity could not be verified").check()
                                page.get_by_role(
                                    "button", name="Confirm rejection", exact=True
                                ).click()
                                page.get_by_text("Rejected.", exact=True).wait_for()
                                capture(page, label + "-rejection-result")
                                continue
                            if scenario == "empty":
                                for name, path in (
                                    ("demo", "/demo"),
                                    ("patients", "/a"),
                                    ("inbox", "/a/inbox"),
                                    ("history", "/a/history"),
                                    ("preferences", "/a/preferences"),
                                ):
                                    page.goto(origin + path)
                                    page.locator('#content[aria-busy="false"]').wait_for()
                                    capture(page, label + "-" + name)
                                page.locator("#digest-time").fill("")
                                page.get_by_role("button", name="Save digest", exact=True).click()
                                capture(page, label + "-invalid-time")
                                page.locator("#digest-time").fill("19:30")
                                page.route("**/api/preferences", lambda route: None)
                                page.get_by_role("button", name="Save digest", exact=True).click()
                                capture(page, label + "-pending-digest")
                                page.unroute("**/api/preferences")
                                continue
                            page.goto(origin + "/a?filter=all")
                            page.locator('#content[aria-busy="false"]').wait_for()
                            page.locator(".patient-row").first.click()
                            page.locator(".patient-row.open + .detail a.primary").click()
                            page.locator("#what-to-do").wait_for()
                            for tab in ("Medicines", "Requests", "Documents", "History"):
                                page.get_by_role("tab", name=tab, exact=True).click()
                                capture(page, label + "-" + tab.lower())
                            if scenario == "monitor":
                                page.get_by_role("tab", name="Medicines", exact=True).click()
                                page.locator("[data-correct-fact]").first.click()
                                capture(page, label + "-correction-dialog")
                            elif scenario == "evidence":
                                page.get_by_role("tab", name="Documents", exact=True).click()
                                page.locator("[data-media]").first.click()
                                page.locator("dialog").wait_for()
                                capture(page, label + "-original-dialog")
                            elif scenario == "question":
                                page.goto(origin + "/a/inbox")
                                page.locator('#content[aria-busy="false"]').wait_for()
                                capture(page, label + "-populated-inbox")
                    except Exception as error:
                        gaps.append(f"{label}: scenario unavailable ({type(error).__name__})")
    except Exception as error:
        gaps.append(f"Browser session unavailable ({type(error).__name__})")
    # Group repeated runs and aurora endpoints into the worst observation per area.
    flagged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for pair in observations:
        if not math.isfinite(pair["score"]):
            gaps.append(f"{pair['scene']}: unmeasurable text run")
        elif pair["score"] < pair["floor"]:
            key = (pair["scene"], pair["element"], pair["context"], pair["foreground"])
            if key not in flagged or pair["score"] < flagged[key]["score"]:
                flagged[key] = pair
    lines = [
        "# Readability observations",
        "",
        "Synthetic doctor, patient and administrator pages, "
        "1440 × 900 and 390 × 844, dark and light, DPR 2.",
        "The approved sample controls the look. These observations change no product value.",
        "Scores use the fifth percentile across rendered backgrounds at both aurora endpoints; "
        "large text is compared with 3:1 and other text with 4.5:1. "
        "Native input values are included.",
        "Visible screenshots are saved before temporary session-only text masking. "
        "Gradient text records its worst stop. Closed details and off-screen clipped text "
        "are not evidence of visible readability.",
        "Approved private sample: "
        + (
            "present (read only)."
            if SAMPLE.exists()
            else "MISSING; this run measures the product only."
        ),
        f"Flagged areas: **{len(flagged)}**. Captured scenes: {len(scenes)}. "
        f"Coverage gaps: {len(gaps)}.",
        "",
        "| Area / state | Theme / width | Score | Reference | Screenshot |",
        "|---|---|---:|---:|---|",
    ]
    for pair in sorted(flagged.values(), key=lambda p: (p["scene"], p["score"])):
        area = (pair["element"] + " in " + pair["context"]).replace("|", "\\|")
        lines.append(
            f"| {pair['scene']}: `{area}` | {pair['theme']} / {pair['width']} | "
            f"{pair['score']:.3f}:1 | {pair['floor']}:1 | "
            f"[{pair['keyframe']}]({pair['screenshot']}) |"
        )
    lines += [
        "",
        "## Owner questions",
        "",
        "- Keyboard focus uses the sample glow. Keep it, or request a different treatment?",
        "- Fold: see the contract report's product and sample measurements.",
        "",
        "## Coverage",
        "",
        *["- " + scene for scene in scenes],
    ]
    if gaps:
        lines += ["", "## Unmeasured areas", "", *["- " + gap for gap in gaps]]
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n")
    (OUTPUT / "observations.json").write_text(json.dumps(observations, indent=2) + "\n")
    print(f"Readability report: {OUTPUT / 'report.md'}; {len(flagged)} flagged areas", flush=True)


if __name__ == "__main__":
    try:
        report()
    except Exception as error:
        # Even an unavailable measurement is an owner report, never a build gate.
        message = f"Readability report unavailable ({type(error).__name__}). No score gate ran.\n"
        try:
            OUTPUT.mkdir(parents=True, exist_ok=True)
            (OUTPUT / "report.md").write_text(message)
        except OSError:
            pass
        print(message, end="")
