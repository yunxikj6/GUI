// Loaded only by the marked, private Ubuntu GNOME 46 verification session.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const apps = new Set(['chatgpt.desktop', 'com.anthropic.Claude.desktop']);
const specification = `<node><interface name="org.cloudworkspace.AppFocus1">
    <method name="Read">
        <arg direction="out" type="s" name="desktopId"/>
        <arg direction="out" type="u" name="windowPid"/>
        <arg direction="out" type="u" name="stableSequence"/>
        <arg direction="out" type="u" name="shellPid"/>
    </method>
</interface></node>`;

export default class AppFocus extends Extension {
    enable() {
        // A second desktop started by a child of this Shell could inherit its
        // XDG search path. Only the compositor running on the named socket participates.
        const token = GLib.getenv('CLOUDWORKSPACE_VERIFY_ID') ?? '';
        const [, raw] = GLib.file_get_contents('/proc/self/cmdline');
        const args = new TextDecoder().decode(raw).split('\0');
        if (GLib.getenv('CLOUDWORKSPACE_VERIFY_APPS') !== '1' ||
            !/^[a-f0-9]{32}$/.test(token) || this.uuid !== `app-focus-${token}@cloudworkspace.local` ||
            !args.includes('--nested') || !args.includes('--wayland-display=' + this.metadata['workspace-display']))
            return;
        this._shellPid = new Gio.Credentials().get_unix_pid();
        this._object = Gio.DBusExportedObject.wrapJSObject(specification, this);
        this._object.export(Gio.DBus.session, '/org/cloudworkspace/AppFocus1');
    }

    Read() {
        const empty = ['', 0, 0, this._shellPid];
        if (Main.sessionMode.isLocked || Main.sessionMode.isGreeter || Main.overview.visible ||
            Main.modalCount > 0 || Main.actionMode !== Shell.ActionMode.NORMAL)
            return empty;
        const window = global.display.focus_window;
        if (!window || !window.has_focus() || window.get_client_type() !== Meta.WindowClientType.WAYLAND)
            return empty;
        const app = Shell.WindowTracker.get_default().get_window_app(window)?.get_id();
        if (!apps.has(app))
            return empty;
        const pid = window.get_pid();
        const sequence = window.get_stable_sequence();
        return pid > 0 && sequence > 0 ? [app, pid, sequence, this._shellPid] : empty;
    }

    disable() {
        this._object?.unexport();
        this._object = null;
    }
}
