"""Browser acceptance against tests.preview_server; synthetic data only."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent


async def main():
    output = ROOT / ".docs" / "preview"
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=os.environ.get("DAFEIYU_BROWSER_EXECUTABLE"),
        )
        page = await browser.new_page(
            viewport={"width": 1440, "height": 1000}, device_scale_factor=1
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:8765/web")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=str(output / "login.png"), full_page=True)
        await page.locator("#access-token").fill("wrong-fixture")
        await page.locator("#login-button").click()
        await expect(page.locator("#login-error")).to_contain_text("管理口令不正确")
        await page.locator("#access-token").fill("admin-fixture")
        await page.locator("#login-button").click()
        await expect(page.locator("#room-title")).to_have_text("摸鱼研究所（演示）")
        await expect(page.locator(".message-row")).to_have_count(60)
        await page.screenshot(path=str(output / "desktop.png"), full_page=True)
        # Background refresh preserves the reader's position and the unsent draft.
        await page.locator("#messages").evaluate("(el)=>el.scrollTop=100")
        before = await page.locator("#messages").evaluate("(el)=>el.scrollTop")
        await page.locator("#refresh").click()
        await expect(page.locator("#refresh")).to_be_enabled()
        assert abs(await page.locator("#messages").evaluate("(el)=>el.scrollTop") - before) < 2
        await page.locator('[data-view="send"]').click()
        await page.locator("#message-draft").fill("尚未发送的虚构草稿")
        await page.locator("#refresh").click()
        await expect(page.locator("#refresh")).to_be_enabled()
        await expect(page.locator("#message-draft")).to_have_value("尚未发送的虚构草稿")
        await page.locator('[data-room="2"]').click()
        await expect(page.locator("#message-draft")).to_have_value("")
        await page.locator('[data-room="1"]').click()
        await expect(page.locator("#message-draft")).to_have_value("尚未发送的虚构草稿")
        await page.locator("#send-text").click()
        await expect(page.locator("#toast")).to_have_text("文字已发送")
        await expect(page.locator("#message-draft")).to_have_value("")
        await page.locator("#image-file").set_input_files(str(ROOT / "assets/memes/smug.png"))
        await expect(page.locator("#send-image")).to_be_enabled()
        await page.locator("#send-image").click()
        await expect(page.locator("#toast")).to_have_text("图片已发送")
        await expect(page.locator("#image-preview")).to_be_hidden()
        await expect(page.locator("#send-image")).to_be_disabled()
        await page.locator('[data-view="messages"]').click()
        await expect(page.locator("#messages")).to_contain_text("尚未发送的虚构草稿")
        await page.locator('[data-view="memories"]').click()
        await page.locator("#memory-kind").select_option("imagination")
        await expect(page.locator(".memory-item")).to_have_count(1)
        await expect(page.locator("#memories")).to_contain_text("梦里的事")
        await page.locator("#memory-kind").select_option("all")
        await page.screenshot(path=str(output / "memories.png"), full_page=True)

        # Slow old requests must not repaint a newly selected conversation.
        async def delayed(route):
            await asyncio.sleep(0.5)
            await route.continue_()

        await page.route("**/gm/messages?conversation_id=2&*", delayed)
        await page.locator('[data-room="2"]').click()
        await page.locator('[data-room="3"]').click()
        await page.wait_for_timeout(800)
        await expect(page.locator("#room-title")).to_have_text("周末聊天（演示）")
        await page.locator('[data-view="messages"]').click()
        await expect(page.locator("#messages")).to_contain_text("第 3 个演示会话")
        await expect(page.locator("#messages")).not_to_contain_text("第 2 个演示会话")
        await page.unroute("**/gm/messages?conversation_id=2&*", delayed)
        await page.locator('[data-room="1"]').click()
        await expect(page.locator(".message-row")).to_have_count(60)
        await page.locator("#older-messages").click()
        await expect(page.locator(".message-row")).to_have_count(92)
        # Every legacy operation remains reachable, with an explicit target.
        await page.locator('[data-action="speak"]').click()
        await expect(page.locator("#action-target")).to_contain_text("摸鱼研究所")
        await page.locator("#action-trigger").fill("虚构测试情境")
        await page.locator("#confirm-action").click()
        await expect(page.locator("#action-dialog")).not_to_be_visible()
        await expect(page.locator("#toast")).to_have_text("主动发言已发送")
        for kind in ["dream", "diary"]:
            await page.locator(f'[data-action="{kind}"]').click()
            await expect(page.locator("#mark-field")).to_be_visible()
            await page.locator("#confirm-action").click()
            await expect(page.locator("#action-dialog")).not_to_be_visible()
        await page.locator("#rename-room").click()
        await page.locator("#room-name").fill("测试会话名称（演示）")
        await page.locator("#confirm-action").click()
        await expect(page.locator("#room-title")).to_have_text("测试会话名称（演示）")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator("#messages").evaluate("(el)=>el.scrollTop=el.scrollHeight")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "mobile.png"), full_page=True)
        await page.locator("#toggle-info").click()
        await expect(page.locator(".inspector")).to_be_visible()
        await expect(page.locator('[data-action="dream"]')).to_be_visible()
        await page.locator("#close-inspector").click()
        await page.locator('[data-view="send"]').click()
        await page.locator("#message-draft").fill("手机端虚构草稿")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "mobile-send.png"), full_page=True)
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.locator("#logout").click()
        await expect(page.locator("#login")).to_be_visible()
        assert await page.evaluate('sessionStorage.getItem("dafeiyu-token")') is None
        assert not errors, errors
        await browser.close()
        print(
            json.dumps(
                {
                    "browser_checks": "passed",
                    "page_errors": errors,
                    "screenshots": 5,
                    "data": "synthetic; isolated database and fake integrations",
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
