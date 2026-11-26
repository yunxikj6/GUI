"""Regressions requiring Chromium's real editor, focus changes and hit testing."""
import os
import shutil
import unittest

from cloud_render.service import _Focus
from cloud_render.wire import Click

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

BROWSER = os.environ.get('CLOUD_WORKSPACE_TEST_CHROMIUM') or shutil.which('chromium') or shutil.which('google-chrome')


@unittest.skipUnless(BROWSER and async_playwright, 'requires Chromium and Playwright')
class NativeEditingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch(executable_path=BROWSER, chromium_sandbox=True)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        await self.page.set_content('<textarea id="a"></textarea><input id="b"><button>Cancel</button>')
        await self.page.locator('#a').focus()
        self.focus = _Focus(self.page)
        await self.focus.remember(1)
        self.addAsyncCleanup(self.focus.close)

    async def test_edit_honors_original_veto_focus_transfer_and_undo(self):
        a = self.page.locator('#a')
        await a.evaluate("""a => a.addEventListener('beforeinput', e => e.preventDefault(), {once:true})""")
        self.assertTrue(await self.focus.commit('原厂控件处理这次输入'))
        self.assertEqual(await a.input_value(), '')
        await a.evaluate("""a => a.addEventListener('beforeinput', () => document.querySelector('#b').focus(),
                                                     {once:true})""")
        self.assertFalse(await self.focus.commit('不能进入另一字段'))
        self.assertEqual(await self.page.locator('#b').input_value(), '')
        await a.focus()
        await self.focus.remember(2)
        self.assertTrue(await self.focus.commit('编辑与撤销'))
        self.assertEqual(await a.input_value(), '编辑与撤销')
        await self.page.keyboard.press('Control+z')
        self.assertEqual(await a.input_value(), '')

    async def test_original_and_current_native_button_hits_must_agree(self):
        button = self.page.get_by_role('button')
        rect = await button.bounding_box()
        x = rect['x'] + rect['width'] / 2
        y = rect['y'] + rect['height'] / 2
        await self.focus.record_hits(1)
        self.assertTrue(await self.focus.hits_control(Click(1, x, y)))
        await button.evaluate("""b => {
            const r=b.getBoundingClientRect(), o=document.createElement('div');
            o.id='overlay'; o.style.cssText='position:fixed;inset:0;z-index:100';
            document.body.append(o);
        }""")
        self.assertFalse(await self.focus.hits_control(Click(1, x, y)))
        await self.focus.record_hits(2)
        await self.page.locator('#overlay').evaluate('e=>e.remove()')
        self.assertFalse(await self.focus.hits_control(Click(2, x, y)))
        await button.evaluate("b=>b.textContent='Submit'")
        self.assertFalse(await self.focus.hits_control(Click(1, x, y)))
