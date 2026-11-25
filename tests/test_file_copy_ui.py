"""Read the native Chromium clipboard after selecting text in the file preview."""
from test_browser import BrowserFixture, expect


class FileCopyAcceptance(BrowserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = "  head\tA  \r\n\r\n\r\tbody  \r  tail  "
        (cls.project / "copy.txt").write_bytes(cls.source.encode())

    def test_drag_copy_preserves_empty_lines_tabs_and_edge_spaces(self):
        page = self.page
        self.context.grant_permissions(["clipboard-read"], origin=self.origin)
        page.locator('[data-file="copy.txt"]').click()
        rows = page.locator("#preview .code-row")
        expect(rows).to_have_count(5)
        heights = rows.evaluate_all("items => items.map(row => row.getBoundingClientRect().height)")
        self.assertGreater(heights[0], 0)
        self.assertEqual(heights, [heights[0]] * 5)

        # Measure the glyph positions. Mouse events create the browser's actual selection.
        def point(row, offset):
            return row.locator(".line-text").evaluate("""(span, offset) => {
                const range = document.createRange();
                range.setStart(span.firstChild, offset);
                range.collapse(true);
                const box = range.getBoundingClientRect();
                return [box.x + 0.1, box.y + box.height / 2];
            }""", offset)

        expected = self.source.replace("\r\n", "\n").replace("\r", "\n")
        start = point(rows.first, 0)
        end = point(rows.last, len("  tail  "))
        page.mouse.move(*start)
        page.mouse.down()
        page.mouse.move(*end, steps=12)
        page.mouse.up()
        page.keyboard.press("Control+c")
        self.assertEqual(page.evaluate("navigator.clipboard.readText()"), expected)
