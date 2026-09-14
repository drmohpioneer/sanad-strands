"""PDF page images and authorized original downloads in a real synthetic browser."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from playwright.sync_api import expect
from providers.test_documents import synthetic_pdf
from store import evidence_fixtures as f

from browser.conftest import RenderedApp


@pytest.mark.parametrize("width", [1440, 390])
def test_pdf_document_pages_and_download(rendered: RenderedApp, width: int) -> None:
    world, page = rendered.world, rendered.page
    page.set_viewport_size({"width": width, "height": 1000})
    f.mission(world)
    data = synthetic_pdf(2)
    _, _, storage = f.providers(world, *([f.lab()] * 4), data=data)
    world.app.state.media_store = storage
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(f.upload, world).result() == "accepted"
    rendered.detail()
    page.goto(f"{rendered.origin}/a/patients/{world.patient_scope.patient_id}#tab=documents")
    page.locator('#content[aria-busy="false"]').wait_for()
    article = (
        page.locator("article").filter(has=page.locator('a[data-media="application/pdf"]')).first
    )
    expect(article.get_by_alt_text("Page 1", exact=True)).to_be_visible()
    expect(article.get_by_alt_text("Page 2", exact=True)).to_be_visible()
    article.locator('a[data-media="image/png"]').first.click()
    expect(page.locator(".lightbox img")).to_be_visible()
    expect(page.locator(".lightbox img")).to_have_js_property("naturalWidth", 1334)
    page.screenshot(path=f"lane/runs/30-lightbox-{width}.png")
    page.get_by_role("button", name="Close original document").click()
    original = article.locator('a[data-media="application/pdf"]')
    href = original.get_attribute("href")
    assert href
    response = page.request.get(rendered.origin + href)
    assert response.status == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.body() == data
    assert (
        len(
            page.request.get(
                f"{rendered.origin}/api/patients/{world.patient_scope.patient_id}/evidence"
            ).json()
        )
        == 1
    )

    from store.account_fixtures import PATIENT

    from sanad.media.upload import UploadIngress
    from sanad.web.routes import upload_router

    ingress = UploadIngress(world.runtime, world.login, storage)
    world.app.include_router(upload_router(ingress, world.app.state.web_settings))
    rendered.login(PATIENT)
    page.goto(rendered.origin + "/pp")
    page.locator('#content[aria-busy="false"]').wait_for()
    expect(page.locator("#patient-file")).to_have_attribute("accept", "image/*,application/pdf")
    page.locator("#patient-file").set_input_files(
        {"name": "synthetic.pdf", "mimeType": "application/pdf", "buffer": data}
    )
    with page.expect_response("**/api/patient/uploads") as received:
        page.locator('#patient-upload-form button[type="submit"]').click()
    assert received.value.status == 202
    assert len(page.request.get(rendered.origin + "/api/patient/uploads").json()["items"]) == 2
    page.locator("#patient-file").set_input_files(
        {"name": "large.pdf", "mimeType": "application/pdf", "buffer": bytes(20_000_001)}
    )
    page.locator('#patient-upload-form button[type="submit"]').click()
    from sanad.presentation.patient_browser import CATALOG

    locale = page.locator("html").get_attribute("lang")
    assert locale in {"en", "ar"}
    expect(page.locator("#patient-action-result")).to_have_text(
        CATALOG["patient_browser.document_too_large"]["ar" if locale == "ar" else "en"]
    )
