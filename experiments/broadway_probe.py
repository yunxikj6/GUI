"""Independent Linux GTK 4 / Chromium experiment, separate from the workbench."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from urllib.request import urlopen


LABEL = "工作区图形实验 Broadway 4217"


def run_gtk():
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk

    app = Gtk.Application(
        application_id="org.example.WorkbenchProbe",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def activate(application):
        window = Gtk.ApplicationWindow(application=application, title="Broadway Probe")
        window.set_default_size(480, 180)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(14)
        box.set_margin_start(14)
        box.set_margin_end(14)
        label = Gtk.Label(label=LABEL)
        label.set_selectable(True)
        entry = Gtk.Entry()
        entry.connect("changed", lambda widget: emit({"text": widget.get_text()}))

        def enter(widget):
            emit({"enter": widget.get_text(), "selected": widget.get_selection_bounds()})

        entry.connect("activate", enter)
        box.append(label)
        box.append(entry)
        window.set_child(box)
        window.present()
        entry.grab_focus()
        emit({"gtk": [Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()]})

    app.connect("activate", activate)
    app.run([])


def wait_for(page, predicate):
    for _ in range(100):
        if predicate():
            return
        page.wait_for_timeout(50)
    raise TimeoutError("GTK or browser did not reach the expected state in 5 seconds")


def measure(browser, base, env, url):
    log = base / "gtk.jsonl"

    def read_log():
        return [json.loads(line) for line in log.read_text().splitlines()]

    page = browser.new_page(viewport={"width": 800, "height": 520})
    counters = {"fromServer": Counter(), "toServer": Counter()}
    sockets = []

    def observe_socket(ws):
        sockets.append(ws.url.rsplit("/", 1)[-1])

        def add(direction, data):
            counts = counters[direction]
            counts["frames"] += 1
            counts["bytes"] += len(data if isinstance(data, bytes) else data.encode())
            counts["binary" if isinstance(data, bytes) else "text"] += 1

        ws.on("framereceived", lambda data: add("fromServer", data))
        ws.on("framesent", lambda data: add("toServer", data))

    page.on("websocket", observe_socket)
    page.goto(url)
    with log.open("w") as writer:
        app = subprocess.Popen(
            [os.environ.get("GTK_PYTHON", "/usr/bin/python3"), Path(__file__).resolve(), "--gtk"],
            env=env, stdout=writer,
        )
    try:
        wait_for(page, lambda: read_log() and counters["fromServer"]["frames"])
        page.wait_for_timeout(600)
        dom = page.evaluate("""label => {
            const tags = {};
            for (const node of document.body.querySelectorAll('*')) {
                tags[node.tagName] = (tags[node.tagName] || 0) + 1;
            }
            return {tags, hasLabel: document.body.textContent.includes(label),
                    inputCount: document.querySelectorAll('input,textarea').length};
        }""", LABEL)
        before = page.screenshot()
        page.keyboard.type("Probe42", delay=50)
        wait_for(page, lambda: {"text": "Probe42"} in read_log())
        after = page.screenshot()
        page.keyboard.press("Control+a")
        selection = page.evaluate("window.getSelection().toString()")
        page.keyboard.press("Enter")
        wait_for(page, lambda: any(item.get("enter") == "Probe42" for item in read_log()))

        os.kill(app.pid, signal.SIGSTOP)
        os.waitpid(app.pid, os.WUNTRACED)
        last = read_log()
        page.keyboard.press("End")
        page.keyboard.type("Z")
        page.wait_for_timeout(350)
        frozen = read_log()
        os.kill(app.pid, signal.SIGCONT)
        wait_for(page, lambda: {"text": "Probe42Z"} in read_log())

        ascii_log = read_log()
        page.keyboard.insert_text("中文")
        page.wait_for_timeout(500)
        return {
            "browser": browser.version, "playwright": version("playwright"),
            "dom": dom, "browserSelection": selection, "pixelsChanged": before != after,
            "noGtkEventWhileStopped": frozen == last, "cdpInsertTextChangedGtk": read_log() != ascii_log,
            "gtkEvents": read_log(), "socketPaths": sockets, "wireFrames": counters,
        }
    finally:
        if app.poll() is None:
            os.kill(app.pid, signal.SIGCONT)
            app.terminate()
        app.wait(timeout=5)


def main():
    from playwright.sync_api import sync_playwright

    prefix = Path(os.environ.get("BROADWAY_PREFIX", "/usr")).resolve()
    lib = prefix / "lib/x86_64-linux-gnu"
    with tempfile.TemporaryDirectory(dir=os.environ.get("PROBE_WORK")) as temp:
        env = dict(
            os.environ, XDG_RUNTIME_DIR=temp, XDG_CACHE_HOME=temp, GTK_A11Y="none",
            GSETTINGS_BACKEND="memory", GDK_BACKEND="broadway", BROADWAY_DISPLAY=":57",
            LD_LIBRARY_PATH=str(lib), GI_TYPELIB_PATH=str(lib / "girepository-1.0"),
            XDG_DATA_DIRS=f"{prefix}/share:/usr/local/share:/usr/share",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}/"
        daemon = subprocess.Popen(
            [prefix / "bin/gtk4-broadwayd", "-a", "127.0.0.1", "-p", str(port), ":57"],
            env=env, stdout=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    with urlopen(url, timeout=1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise TimeoutError("Broadway daemon did not listen")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"))
                try:
                    result = measure(browser, Path(temp), env, url)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    browser.close()
        finally:
            daemon.terminate()
            daemon.wait(timeout=5)


if __name__ == "__main__":
    if "--gtk" in sys.argv:
        run_gtk()
    else:
        main()
