"""Resolve private Mutter focus records to the two cloud processes selected by their owner."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    start_ticks: int

    @classmethod
    def from_pid(cls, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError('a live process ID is required')
        # comm can contain spaces and ')'. Start time is field 22 after pid and comm.
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return cls(pid, int(fields[19]))


@dataclass(frozen=True)
class Window:
    role: str
    process: Process
    connection: int
    surface: int
    toplevel: int


class WindowFocus:
    """One seat in a private GNOME session. Process identities come from its two launchers."""

    def __init__(self, *, app, browser):
        if app == browser:
            raise ValueError('the two original applications must be distinct processes')
        self.processes = {'app': app, 'browser': browser}
        self.xdg = {}
        self.tops = {}
        self.focus = None

    def record(self, line):
        fields = line.split()
        if len(fields) == 2 and fields[0] == 'DISCONNECT':
            connection = int(fields[1])
            self.xdg = {k: v for k, v in self.xdg.items() if k[0] != connection}
            self.tops = {k: v for k, v in self.tops.items() if k[0] != connection}
            if self.focus and self.focus[0] == connection:
                self.focus = None
            return
        if len(fields) != 7:
            raise ValueError('invalid native window record')
        event, role = fields[:2]
        pid, start, connection, resource, obj = map(int, fields[2:])
        if role not in self.processes or min(pid, start, connection, resource) <= 0:
            raise ValueError('invalid native window identity')
        if Process(pid, start) != self.processes[role]:
            if event == 'ENTER':
                self.focus = None
            return
        if (event in ('XDG', 'TOP') and obj <= 0) or (event not in ('XDG', 'TOP') and obj != 0):
            raise ValueError('invalid native window object')
        key = (connection, resource)
        if event == 'XDG':
            self.xdg[connection, obj] = (role, resource)
        elif event == 'TOP':
            if key in self.xdg:
                self.tops[connection, obj] = key
        elif event == 'ENTER':
            self.focus = key
        elif event in ('LEAVE', 'DROP_SURFACE'):
            if self.focus == key:
                self.focus = None
            if event == 'DROP_SURFACE':
                for xdg in [k for k, v in self.xdg.items() if k[0] == connection and v == (role, resource)]:
                    self._drop_xdg(xdg)
        elif event == 'DROP_XDG':
            self._drop_xdg(key)
        elif event == 'DROP_TOP':
            xdg = self.tops.pop(key, None)
            if xdg is not None and self.focus == (connection, self.xdg[xdg][1]):
                self.focus = None
        else:
            raise ValueError('unknown native window event')

    def _drop_xdg(self, key):
        role_and_surface = self.xdg.pop(key, None)
        if role_and_surface and self.focus == (key[0], role_and_surface[1]):
            self.focus = None
        self.tops = {top: xdg for top, xdg in self.tops.items() if xdg != key}

    @property
    def focused(self):
        """Only return an explicitly focused, extant and unique toplevel of that process."""
        if not self.focus:
            return None
        found = None
        counts = {'app': 0, 'browser': 0}
        for (connection, top), xdg in self.tops.items():
            role, surface = self.xdg[xdg]
            counts[role] += 1
            if (connection, surface) == self.focus:
                found = Window(role, self.processes[role], connection, surface, top)
        return found if found and counts[found.role] == 1 else None
