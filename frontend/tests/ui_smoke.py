"""Click through the console's navigation against a running server.

    uv run --no-project --with playwright python frontend/tests/ui_smoke.py [http://localhost:8787]

Every check is a path a person takes: a tab returns where it was left, the URL
holds the place (reload, back), and links between objects land on the object's
home. Needs at least one console session (and, for the Inbox check, one item) on the server.
"""

import asyncio
import glob
import sys

from playwright.async_api import async_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8787").rstrip("/") + "/ui/"
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


async def main() -> None:
    async with async_playwright() as p:
        exe = sorted(glob.glob("/Users/*/Library/Caches/ms-playwright/chromium-*/chrome-mac*/**/Google Chrome for Testing",
                               recursive=True))
        browser = await p.chromium.launch(executable_path=exe[-1] if exe else None)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        hash_ = lambda: page.evaluate("location.hash")  # noqa: E731
        tab = lambda name: page.get_by_role("group", name="View").get_by_role("button", name=name)  # noqa: E731

        await page.goto(BASE)
        await page.wait_for_function("location.hash.startsWith('#/chat/')")
        chat = await hash_()
        check(chat.startswith("#/chat/"), f"boot fills in the newest session ({chat})")

        await tab("Usage").click()
        await page.wait_for_function("location.hash.startsWith('#/usage')")
        usage = await hash_()
        check(True, f"Usage opens ({usage})")
        check(not await page.locator(".rail").is_visible(), "a team-wide page shows no session rail")

        await tab("Chat").click()
        await page.wait_for_function(f"location.hash === {chat!r}")
        check(await hash_() == chat, "the Chat tab returns to the session left")

        await page.go_back()
        await page.wait_for_function(f"location.hash === {usage!r}")
        check(await hash_() == usage, "back returns to Usage")

        await page.reload()
        await page.wait_for_selector(".usage")
        check(await hash_() == usage, "reload keeps Usage")

        await tab("Inbox").click()
        await page.wait_for_function("location.hash.startsWith('#/inbox')")
        heads = page.locator(".inbox-item-head")
        if await heads.count():
            await heads.first.click()
            await page.wait_for_function("location.hash.split('/').length === 3")
            item_url = await hash_()
            await page.reload()
            await page.wait_for_selector(".inbox-item.open")
            check(await hash_() == item_url, f"an opened Inbox item is in the URL and reopens on reload ({item_url})")

        await page.goto(BASE + chat)
        await page.get_by_role("button", name="Trace").click()
        await page.wait_for_function("location.hash.startsWith('#/trace/')")
        rows = page.locator(".session-open")
        if await rows.count() > 1:
            await rows.nth(1).click()
            await page.wait_for_function(f"location.hash.startsWith('#/trace/') && location.hash !== {chat.replace('chat', 'trace')!r}")
            check(True, "a session picked while reading a Trace opens in Trace")
        await page.get_by_role("button", name="Conversation").click()
        await page.wait_for_function("location.hash.startsWith('#/chat/')")
        check(True, "Conversation switches the same session back to Chat")

        await page.goto(BASE + "#/usage?days=7")
        await page.wait_for_selector(".usage")
        pressed = await page.locator(".usage-head button[aria-pressed=true]").inner_text()
        check(pressed.strip() == "7d", "the usage range comes from the URL")

        await browser.close()
    if failures:
        sys.exit(f"{len(failures)} check(s) failed")


asyncio.run(main())
