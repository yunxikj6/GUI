"""Consume original GNOME commands and verified native drawings from one paired source.

A trusted source must remove every PUT for each participating application before
sending bytes, including revisions whose pictures are unavailable. The default stream permits one
opaque application resource. An explicitly enabled producer and consumer can also
carry two overlapping, independently verified resource lifetimes.
"""
from dataclasses import dataclass, field
import hashlib
import struct

from cloud_render import frame_delta, gnome_commands as mesa, gnome_wallpaper as artwork
from cloud_render.wire import MAX_PAYLOAD
from experiments.application_surface import ApplicationSurfaces, Binding, Commit, SurfaceError

# Extension records use byte lengths. Ordinary packets retain Mesa vtest framing.
BIND, HOLD, DRAW, REUSE, DRAW_DELTA = range(0x534B5201, 0x534B5206)
_IDENTITY = struct.Struct('<Q32s32sI32sIII')
_POINT = struct.Struct('<QQ')
DRAW_PREFIX_BYTES = _POINT.size + mesa._TRANSFER.size + 32
_MAX_ASSET = MAX_PAYLOAD + 12
_MAX_HANDLES = 4096
_TEXTURE_CACHE_BYTES = 16 << 20


def binding_record(offset, binding):
    """Publish an authenticated witness's binding before the first application PUT."""
    body = _IDENTITY.pack(offset, binding.session, binding.executable_sha256, binding.pid,
                          binding.process_instance, binding.surface_id, binding.handle, binding.creation)
    return mesa._HEADER.pack(len(body), BIND) + body


def replacement_record(offset, serial, descriptor, *, digest=None, picture=None):
    """Replace one whole original PUT, including its header and original pixel body.

    No drawing is HOLD. A digest with a picture supplies an original CLRP.
    A digest on its own reuses only the last registered drawing of this lifetime.
    """
    body = _POINT.pack(offset, serial) + mesa._TRANSFER.pack(*descriptor)
    op = HOLD
    if digest is not None:
        if type(digest) is not bytes or len(digest) != 32:
            raise SurfaceError('invalid original drawing digest')
        op = REUSE
        body += digest
    if picture is not None:
        if op != REUSE or type(picture) is not bytes or not 0 < len(picture) <= _MAX_ASSET:
            raise SurfaceError('invalid original drawing')
        op = DRAW
        body += picture
    return mesa._HEADER.pack(len(body), op) + body


def _exact(source, size, *, eof=False):
    data = bytearray()
    while len(data) < size:
        part = source.read(size - len(data))
        if not part:
            if eof and not data:
                return b''
            raise SurfaceError('truncated drawing stream')
        if len(part) > size - len(data):
            raise SurfaceError('source returned more bytes than requested')
        data.extend(part)
    return bytes(data)


class _Packet:
    def __init__(self, source, header):
        self.source, self.header = source, header

    def read(self, size):
        if self.header:
            header, self.header = self.header, None
            return header
        return _exact(self.source, size)


@dataclass
class _Lifetime:
    descriptor: tuple
    creation: int
    received_put: bool = False


@dataclass
class _SurfaceHistory:
    binding: Binding
    serial: int = 0
    serial_digest: bytes | None = None
    digest: bytes | None = None

    def observe(self, serial, digest):
        if type(serial) is not int or serial <= 0 or serial < self.serial:
            raise SurfaceError('stale application commit')
        if serial != self.serial:
            self.serial = serial
            self.serial_digest = None
        if digest is not None:
            if type(digest) is not bytes or len(digest) != 32:
                raise SurfaceError('invalid original drawing digest')
            if self.serial_digest is not None and self.serial_digest != digest:
                raise SurfaceError('conflicting drawing for one application commit')
            self.serial_digest = digest


@dataclass
class _LocalSurface(_SurfaceHistory):
    commit: Commit | None = None
    picture: bytes | None = None
    textures: dict = field(default_factory=dict)


class LiveSurfaceStream:
    """File-like adapter for Win64 replay. Call read on the renderer thread.

    The paired transport authenticates the producer. authorize additionally
    checks each source-attested process/surface lifetime against the application
    this session requested. The adapter accepts no network URLs, paths or commands.
    """
    def __init__(self, source, *, session, authorize, rasterize, wallpaper=None, picture_delta=False,
                 wallpaper_x99=False, multiple_surfaces=False):
        self.source = source
        self.session = session
        self.authorize = authorize
        self.rasterize = rasterize
        self.offset = self.creation = 0
        self.handles = {}
        self._surfaces = {}
        self._surface_limit = 2 if multiple_surfaces else 1
        self._view_epoch = 0
        self._rendering = None
        self._buffer = memoryview(b'')
        self.picture_delta = picture_delta
        self.wallpaper = wallpaper
        self.wallpaper_x99 = wallpaper_x99
        self.artwork_references = 0
        self._resource_tail = None
        self._failed = False
        self.guard = ApplicationSurfaces(session, lambda *_: None, self._commit_for,
                                         self._read_asset, self._render_current)
        self.drawings = self.reuses = self.holds = 0

    def _only_surface(self):
        surfaces = tuple(self._surfaces.values())
        return surfaces[0] if len(surfaces) == 1 else None

    @property
    def binding(self):
        # A legacy input caller must never infer keyboard focus from the last PUT.
        surface = self._only_surface()
        return surface.binding if surface is not None else None

    @property
    def digest(self):
        surface = self._only_surface()
        return surface.digest if surface is not None else None

    @property
    def picture(self):
        surface = self._only_surface()
        return surface.picture if surface is not None else None

    @property
    def commit(self):
        if self._surface_limit == 1:
            surface = self._only_surface()
            return surface.commit if surface is not None else None
        # A click belongs to the whole displayed desktop. Opening and closing a
        # second surface invalidates a press even if the first picture is unchanged.
        return self._view_epoch, tuple(s.commit for s in tuple(self._surfaces.values()))

    def _commit_for(self, session, ticket):
        surface = self._rendering
        if surface is not None and session == self.session and ticket == surface.digest:
            return surface.commit

    def _render_current(self, frame):
        surface = self._rendering
        textures = surface.textures
        pixels = textures.pop(surface.digest, None)
        if pixels is None:
            pixels = self.rasterize(frame)
        if type(pixels) is bytes and len(pixels) == frame.width * frame.height * 4:
            textures[surface.digest] = pixels
            # Each lifetime keeps its own current texture and may cache a second
            # native caret phase within the budget. Neither can seed another surface.
            while len(textures) > 1 and (len(textures) > 2 or sum(map(len, textures.values())) > _TEXTURE_CACHE_BYTES):
                del textures[next(iter(textures))]
        return pixels

    def _read_asset(self, digest, limit):
        surface = self._rendering
        if surface is None or surface.digest != digest or surface.picture is None or len(surface.picture) > limit:
            raise SurfaceError('missing original drawing in this resource lifetime')
        return surface.picture

    @property
    def can_present(self):
        # Win32 input samples readiness concurrently with the graphics reader.
        targets = tuple(self.guard.targets.values())
        return not self._failed and all(target.ready for target in targets)

    def _register(self, length):
        if length != _IDENTITY.size or len(self._surfaces) >= self._surface_limit:
            raise SurfaceError('unexpected application binding')
        offset, session, exe, pid, instance, surface, handle, generation = _IDENTITY.unpack(
            _exact(self.source, length))
        current = self.handles.get(handle)
        binding = Binding(session, exe, pid, instance, surface, handle, generation)
        if (offset != self.offset or session != self.session or current is None or current.received_put
                or current.creation != generation or self.authorize(binding) is not True):
            raise SurfaceError('unverified or previously uploaded application resource')
        self.guard.bind_untouched_resource(binding, current.descriptor, current.creation)
        self._surfaces[handle] = _LocalSurface(binding)
        self._view_epoch += 1

    def _replacement(self, length, op):
        fixed = _POINT.size + mesa._TRANSFER.size
        if ((op == HOLD and length != fixed) or (op == REUSE and length != fixed + 32)
                or (op == DRAW_DELTA and not self.picture_delta)
                or (op in (DRAW, DRAW_DELTA) and not DRAW_PREFIX_BYTES < length <= DRAW_PREFIX_BYTES + _MAX_ASSET)):
            raise SurfaceError('invalid drawing record length')
        offset, serial = _POINT.unpack(_exact(self.source, _POINT.size))
        desc = mesa._TRANSFER.unpack(_exact(self.source, mesa._TRANSFER.size))
        surface = self._surfaces.get(desc[0])
        if offset != self.offset or surface is None:
            raise SurfaceError('missing, stale or reordered surface commit')
        target = self.handles[desc[0]].descriptor
        _, level, stride, layer_stride, x, y, z, w, h, depth, size = desc
        row = stride or w * 4
        if (level or z or depth != 1 or not w or not h or x+w > target[4] or y+h > target[5]
                or row < w*4 or not row*(h-1)+w*4 <= size <= min(row*h, 32 << 20)
                or layer_stride not in (0, size)):
            raise SurfaceError('invalid original PUT geometry')
        self.offset += mesa._HEADER.size + mesa._TRANSFER.size + size
        self.guard.wait_for_picture(desc[0])
        digest = None if op == HOLD else _exact(self.source, 32)
        surface.observe(serial, digest)
        if op == HOLD:
            self._view_epoch += 1
            self.holds += 1
            return b''
        if op in (DRAW, DRAW_DELTA):
            picture = _exact(self.source, length - DRAW_PREFIX_BYTES)
            if op == DRAW_DELTA:
                try:
                    picture = frame_delta.decode_frame(picture, surface.picture or b'')
                except frame_delta.DeltaError as exc:
                    raise SurfaceError('invalid original drawing delta') from exc
            if hashlib.sha256(picture).digest() != digest:
                raise SurfaceError('original drawing digest mismatch')
            surface.picture, surface.digest = picture, digest
            self.drawings += 1
        else:
            if surface.picture is None or surface.digest != digest:
                raise SurfaceError('no previous drawing for this lifetime')
            self.reuses += 1
        surface.commit = Commit(surface.binding, serial, digest)
        # ApplicationSurfaces resolves all three callbacks synchronously. Scope
        # each lookup to this descriptor even when both windows use the same SHA.
        self._rendering = surface
        try:
            layout, pixels = self.guard.local_texture(desc, digest)
        finally:
            self._rendering = None
        body = mesa._TRANSFER.pack(*layout) + pixels
        return mesa._HEADER.pack(len(body)//4, mesa.PUT) + body

    def _checked_upload(self, desc, length):
        self.guard.check_raw_upload(desc)
        current = self.handles.get(desc[0])
        if current is None or desc[-1] > 32 << 20 or length != 11 + (desc[-1] + 3)//4:
            raise SurfaceError('invalid original resource upload')
        current.received_put = True
        return current

    def _artwork_upload(self, length):
        if (self.wallpaper is None or self.artwork_references >= artwork.MAX_REFERENCES or
                length != (mesa._TRANSFER.size + 32)//4):
            raise SurfaceError('unnegotiated or invalid original desktop artwork')
        body = _exact(self.source, 4 * length)
        desc = mesa._TRANSFER.unpack_from(body)
        current = self._checked_upload(desc, 11 + (desc[-1] + 3)//4)
        if tuple(current.descriptor[4:6]) != (artwork.WIDTH, artwork.HEIGHT):
            raise SurfaceError('artwork does not match the original resource size')
        if self.artwork_references == 0 and (type(self.wallpaper) is not bytes or
                len(self.wallpaper) != artwork.PIXELS * 4 or hashlib.sha256(self.wallpaper).digest() != artwork.RGBX_SHA256):
            raise SurfaceError('local original desktop artwork differs from its source')
        head, asset = artwork.expand_reference(body, self.wallpaper, x99=self.wallpaper_x99)
        # A later Wayland binding names an offset in the original source stream.
        # The local GPU sees the exact PUT while the session cache keeps the small reference.
        self.offset += len(head) + len(asset)
        self.artwork_references += 1
        self._resource_tail = memoryview(asset)
        return head

    def _packet(self):
        header = _exact(self.source, mesa._HEADER.size, eof=True)
        if not header:
            return None
        length, op = mesa._HEADER.unpack(header)
        if op == BIND:
            self._register(length)
            return b''
        if op in (HOLD, DRAW, DRAW_DELTA, REUSE):
            return self._replacement(length, op)
        if op == artwork.REFERENCE:
            return self._artwork_upload(length)
        if op == mesa.PUT:
            descriptor = _exact(self.source, mesa._TRANSFER.size)
            desc = mesa._TRANSFER.unpack(descriptor)
            self._checked_upload(desc, length)
            body = descriptor + _exact(self.source, desc[-1])
        else:
            _, op, body = mesa._request(_Packet(self.source, header))
        if op == mesa.CREATE:
            desc = mesa._RESOURCE.unpack(body)
            if desc[0] in self.handles or len(self.handles) >= _MAX_HANDLES:
                raise SurfaceError('invalid or excessive live resources')
            self.creation += 1
            self.handles[desc[0]] = _Lifetime(desc, self.creation)
        elif op == mesa.UNREF:
            handle, = mesa._WORD.unpack(body)
            if self.handles.pop(handle, None) is None:
                raise SurfaceError('unreference of an unknown resource')
            self.guard.observe_unreferenced(handle)
            if self._surfaces.pop(handle, None) is not None:
                self._view_epoch += 1
        self.offset += len(header) + len(body)
        return header + body

    def read(self, size):
        if self._failed or size < 0:
            raise SurfaceError('unavailable or unbounded application read')
        if not size:
            return b''
        try:
            while not self._buffer:
                if self._resource_tail is not None:
                    self._buffer, self._resource_tail = self._resource_tail, None
                    break
                packet = self._packet()
                if packet is None:
                    return b''
                self._buffer = memoryview(packet)
            data = self._buffer[:size].tobytes()
            self._buffer = self._buffer[len(data):]
            return data
        except Exception:
            self._failed = True
            self._surfaces.clear()
            raise


def replay_live(source, on_scanout, *, session, authorize, rasterize, runtime_dir=None, multiple_surfaces=False):
    """Present only authoritative current scanouts through the existing PE engine."""
    from thin_client.virgl_windows import replay
    client = LiveSurfaceStream(source, session=session, authorize=authorize, rasterize=rasterize,
                               multiple_surfaces=multiple_surfaces)
    counts = {'presented': 0, 'held': 0}
    def show(w, h, pixels):
        if client.can_present:
            counts['presented'] += 1
            on_scanout(w, h, pixels)
        else:
            counts['held'] += 1
    replay(client, show, runtime_dir=runtime_dir)
    return dict(counts, pictures=client.drawings, reused=client.reuses, pending=client.holds)


# An explicit negative result from a source observer which has fenced this PUT.
# An absent, delayed or incomplete observation is not an ordinary desktop asset.
DESKTOP = object()


@dataclass(frozen=True)
class WitnessedPut:
    binding: Binding
    serial: int
    picture_sha256: bytes | None


def stream_witnessed(source, sink, *, inspect_put, fetch_picture, multiple_surfaces=False):
    """Strip the witnessed application's raw bytes before the first outgoing write.

    inspect_put(offset, ordinal, descriptor, prior CREATE ordinal, source_pixels)
    may block for a trusted source witness. It must supply DESKTOP only after the
    observer has established that the transfer is unrelated to this app. None or
    an exception stops the stream. Raw PUT data stays at the source. The source
    and sink must be buffered binary streams. No complete session is buffered.
    """
    offset = ordinal = creation = removed = pending = sent_pictures = 0
    handles = {}
    surfaces = {}
    session = None
    surface_limit = 2 if multiple_surfaces else 1
    while (packet := mesa._request(source)) is not None:
        header, op, body = packet
        ordinal += 1
        if op == mesa.CREATE:
            descriptor = mesa._RESOURCE.unpack(body)
            handle = descriptor[0]
            if handle in handles or len(handles) >= _MAX_HANDLES:
                raise SurfaceError('invalid or excessive source resource lifetimes')
            creation += 1
            handles[handle] = _Lifetime(descriptor, creation)
        elif op == mesa.UNREF:
            handle, = mesa._WORD.unpack(body)
            if handles.pop(handle, None) is None:
                raise SurfaceError('unknown source resource')
            surfaces.pop(handle, None)
        outgoing = header + body
        if op == mesa.PUT:
            descriptor = mesa._TRANSFER.unpack_from(body)
            current = handles.get(descriptor[0])
            if current is None:
                raise SurfaceError('source upload has no live resource')
            witness = inspect_put(offset, ordinal, descriptor, current.creation, memoryview(body)[mesa._TRANSFER.size:])
            surface = surfaces.get(descriptor[0])
            if witness is DESKTOP:
                if surface is not None:
                    raise SurfaceError('missing witness for a bound application PUT')
                current.received_put = True
            elif isinstance(witness, WitnessedPut):
                binding = witness.binding
                if ((binding.handle, binding.creation) != (descriptor[0], current.creation) or current.received_put
                        or (session is not None and session != binding.session)
                        or (surface is not None and surface.binding != binding)):
                    raise SurfaceError('source application changed or prior upload already escaped')
                prefix = b''
                if surface is None:
                    if len(surfaces) >= surface_limit:
                        raise SurfaceError('unexpected concurrent application binding')
                    session = binding.session
                    surface = surfaces[binding.handle] = _SurfaceHistory(binding)
                    prefix = binding_record(offset, binding)
                digest = witness.picture_sha256
                surface.observe(witness.serial, digest)
                picture = None
                if digest is None:
                    pending += 1
                elif digest != surface.digest:
                    picture = fetch_picture(digest, _MAX_ASSET)
                    if (type(picture) is not bytes or len(picture) > _MAX_ASSET or
                            hashlib.sha256(picture).digest() != digest):
                        raise SurfaceError('source drawing failed integrity verification')
                    sent_pictures += 1
                    surface.digest = digest
                outgoing = prefix + replacement_record(offset, witness.serial, descriptor,
                                                        digest=digest, picture=picture)
                removed += descriptor[-1]
            else:
                raise SurfaceError('source observer has not classified this PUT')
        if sink.write(outgoing) != len(outgoing):
            raise SurfaceError('incomplete private transport write')
        if op in (mesa.SUBMIT, mesa.GET):
            sink.flush()
        offset += len(header) + len(body)
    sink.flush()
    return dict(originalBytes=offset, removedApplicationBytes=removed,
                drawings=sent_pictures, pending=pending)
