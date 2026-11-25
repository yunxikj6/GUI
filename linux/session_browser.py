"""Select registered browsers for a nested GNOME session without opening URLs."""
import sys

from gi.repository import Gio, GLib


def browser(app):
    return (isinstance(app, Gio.DesktopAppInfo) and not app.get_is_hidden() and
            "WebBrowser" in (app.get_categories() or "").split(";"))


def main(args):
    if args not in ([], ["--check"]):
        return 2
    defaults = GLib.KeyFile()
    available = True
    for scheme in ("http", "https"):
        mime = "x-scheme-handler/" + scheme
        selected = Gio.AppInfo.get_default_for_uri_scheme(scheme)
        if browser(selected):
            continue
        candidate = next((app for app in Gio.AppInfo.get_recommended_for_type(mime) if browser(app)), None)
        if candidate is None:
            available = False
        elif selected is None or selected.get_id() == "chatgpt.desktop":
            # Other handlers can be intentional, for example an organization's URL dispatch application.
            defaults.set_string_list("Default Applications", mime, [candidate.get_id()])
    if args:
        return 0 if available else 1
    sys.stdout.write(defaults.to_data()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
