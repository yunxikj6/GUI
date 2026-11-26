import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// Only the exclusive source's private Unix socket carries committed text.
// The desktop continues to draw and edit its own St.Entry through Clutter.
export default class SearchInput extends Extension {
    enable() {
        const address = Gio.UnixSocketAddress.new(GLib.build_filenamev([GLib.get_user_runtime_dir(), 'text-input']));
        this._socket = new Gio.SocketClient().connect(address, null);
        this._input = new Gio.DataInputStream({base_stream: this._socket.get_input_stream()});
        this._output = this._socket.get_output_stream();
        this._cancel = new Gio.Cancellable();
        this._revision = 0;
        this._active = null;
        this._signals = [
            [global.stage, 'notify::key-focus'],
            [Main.overview, 'showing'],
            [Main.overview, 'hiding'],
            [Main.screenShield, 'locked-changed'],
            [Main.sessionMode, 'updated'],
        ].filter(([source]) => source).map(([source, name]) =>
            [source, source.connect(name, () => {
                this._focus();
                // Clutter can attach its input method after its key-focus signal.
                if (!this._idle)
                    this._idle = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                        this._idle = 0;
                        this._focus();
                        return GLib.SOURCE_REMOVE;
                    });
            })]);
        this._focus();
        this._read(this._cancel);
    }

    _focus() {
        const entry = Main.overview.searchEntry;
        const active = !!(Main.overview.visible && Main.overview.visibleTarget &&
            Main.actionMode === Shell.ActionMode.OVERVIEW && !Main.screenShield?.locked && !Main.sessionMode.isLocked &&
            entry?.mapped && entry.get_clutter_text().get_editable() &&
            global.stage.get_key_focus() === entry.get_clutter_text() &&
            Main.inputMethod.currentFocus);
        if (this._active !== active) {
            this._active = active;
            this._revision++;
            this._write(`F ${this._revision} ${Number(active)}\n`);
        }
        return active;
    }

    _write(line) {
        this._output.write_all(new TextEncoder().encode(line), this._cancel);
    }

    _commit(text) {
        // Clutter queues an input-method commit. Confirm the write on this
        // exact native buffer before letting the client send its next action.
        const buffer = Main.overview.searchEntry.get_clutter_text().get_buffer();
        return new Promise((resolve, reject) => {
            let signal, timer;
            const finish = applied => {
                if (signal)
                    buffer.disconnect(signal);
                if (timer)
                    GLib.source_remove(timer);
                this._abortCommit = null;
                if (applied)
                    resolve();
                else
                    reject(new Error('Unconfirmed native edit'));
            };
            this._abortCommit = () => finish(false);
            signal = buffer.connect('inserted-text', (_buffer, _position, inserted) => {
                finish(inserted === text);
            });
            timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1200, () => {
                timer = 0;
                finish(false);
                return GLib.SOURCE_REMOVE;
            });
            try {
                Main.inputMethod.commit(text);
            } catch {
                finish(false);
            }
        });
    }

    _selectAll() {
        const nativeText = Main.overview.searchEntry.get_clutter_text();
        nativeText.set_selection(0, -1);
        const length = nativeText.get_buffer().get_length();
        const position = nativeText.get_cursor_position();
        const bound = nativeText.get_selection_bound();
        return Math.abs((position < 0 ? length : position) - (bound < 0 ? length : bound)) === length;
    }

    _read(cancel) {
        this._input.read_line_async(GLib.PRIORITY_DEFAULT, cancel, async (stream, result) => {
            if (cancel.is_cancelled())
                return;
            try {
                const [line] = stream.read_line_finish_utf8(result);
                if (line === null)
                    return;
                const [revision, text] = JSON.parse(line);
                const focused = this._focus() && this._revision === revision;
                let applied = focused && typeof text === 'string' && text.length > 0 &&
                    !/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(text) &&
                    new TextEncoder().encode(text).length <= 4096;
                if (applied)
                    await this._commit(text);
                else if (focused && text === null)
                    applied = this._selectAll();
                this._write(`A ${Number(applied)}\n`);
                this._read(cancel);
            } catch {
                // Never log a request or exception containing user text.
                this._socket.close(null);
            }
        });
    }

    disable() {
        if (this._idle)
            GLib.source_remove(this._idle);
        this._abortCommit?.();
        this._cancel?.cancel();
        for (const [source, id] of this._signals ?? [])
            source.disconnect(id);
        this._socket?.close(null);
    }
}
