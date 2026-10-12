"""Public driver readiness handshake before exposing the synchronous API."""
from contextlib import contextmanager


@contextmanager
def sync_playwright():
    from playwright.sync_api import sync_playwright as start_driver

    with start_driver() as playwright:
        # In Playwright 1.62, __enter__ can return before its init task finishes.
        # A local protocol round trip completes that task before a quick stop.
        # Creating/disposal of this request context does not send HTTP requests.
        context = playwright.request.new_context()
        context.dispose()
        yield playwright
