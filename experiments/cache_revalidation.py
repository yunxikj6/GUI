"""Measure a cached README preview while a read-only revalidation RPC is delayed."""
import argparse
import asyncio
import json
import os
import shutil
import statistics

from playwright.async_api import async_playwright


READ_OPS = {"file", "status", "tree", "tasks", "events", "diff"}
PREVIEW_READY = """name =>
    document.querySelector('#fileTitle').textContent === name &&
    document.querySelector('#preview .line-text') &&
    !document.querySelector('#fileSync').textContent"""
WATCH_CLICKS = """firstLine => {
    document.addEventListener('click', event => {
        if (event.target.closest('#tree button[data-file]')?.dataset.file !== 'README.md') return;
        const start = performance.now();
        const sample = {};
        window.cacheTiming = sample;
        const visible = () => document.querySelector('#fileTitle').textContent === 'README.md' &&
            document.querySelector('#preview .line-text')?.textContent === firstLine &&
            document.querySelector('#preview').getBoundingClientRect().height > 0;
        queueMicrotask(() => { sample.syncContent = visible(); });
        function checkFrame() {
            if (!visible()) {
                requestAnimationFrame(checkFrame);
                return;
            }
            sample.readyFrame = performance.now() - start;
            sample.networkStillPending = document.querySelector('#fileSync').textContent.includes('正在校验');
            requestAnimationFrame(() => { sample.nextFrame = performance.now() - start; });
        }
        requestAnimationFrame(checkFrame);
    }, true);
}"""


async def measure(page, port, samples, delay_ms):
    delay_reads = False
    delayed_count = 0
    unexpected = []

    async def intercept(route):
        nonlocal delayed_count
        request = route.request.post_data_json or {}
        op = request.get("op")
        if op not in READ_OPS:
            unexpected.append(op)
            await route.abort("blockedbyclient")
            return
        if delay_reads and op == "file" and request.get("path") == "README.md":
            delayed_count += 1
            await asyncio.sleep(delay_ms / 1000)
        await route.continue_()

    await page.route("**/api/rpc", intercept)
    await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
    target = page.locator('#tree button[data-file="README.md"]')
    alternate = page.locator('#tree button[data-file=".gitignore"]')
    await target.click()
    await page.wait_for_function(PREVIEW_READY, arg="README.md")
    first_line = await page.locator("#preview .line-text").first.text_content()
    await page.evaluate(WATCH_CLICKS, first_line)
    delay_reads = True
    rows = []
    for _ in range(samples):
        await alternate.click()
        await page.wait_for_function(PREVIEW_READY, arg=".gitignore")
        await target.click()
        await page.wait_for_function("() => window.cacheTiming?.nextFrame > 0")
        rows.append(await page.evaluate("() => window.cacheTiming"))
        await page.wait_for_function(PREVIEW_READY, arg="README.md")
    if unexpected or delayed_count != samples:
        raise RuntimeError(f"Unexpected RPCs: {unexpected}. Delayed {delayed_count} of {samples} reads.")
    return {
        "samples": rows,
        "count": delayed_count,
        "waitInjectedMs": delay_ms,
        "firstFrameMedian": statistics.median(row["readyFrame"] for row in rows),
        "secondFrameMedian": statistics.median(row["nextFrame"] for row in rows),
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--delay-ms", type=int, default=1200)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.samples <= 1000 or not 0 <= args.delay_ms <= 30000:
        parser.error("expected port 1–65535, samples 1–1000 and delay-ms 0–30000")
    executable = os.environ.get("CLOUD_WORKSPACE_TEST_CHROMIUM") or shutil.which("chromium") or shutil.which("google-chrome")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=executable, headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(60000)
            result = await measure(page, args.port, args.samples, args.delay_ms)
        finally:
            await browser.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
