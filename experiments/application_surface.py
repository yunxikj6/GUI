"""Experimental substitution of a verified opaque application surface in a GNOME stream.

The authority callbacks must come from the paired application's trusted producer.
This module does not infer ownership from dimensions, pixels or a GPU handle.
"""
from dataclasses import dataclass
import hashlib
import io
from typing import Callable

from cloud_render.wire import Frame, MAX_PAYLOAD, encode_message, read_message


class SurfaceError(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    """Producer-verified association for one compositor resource lifetime."""
    session: bytes
    executable_sha256: bytes
    pid: int
    process_instance: bytes
    surface_id: int
    handle: int
    creation: int


@dataclass(frozen=True)
class Commit:
    binding: Binding
    serial: int
    picture_sha256: bytes


@dataclass
class _Target:
    binding: Binding
    width: int
    height: int
    last: Commit | None = None
    ready: bool = False


class ApplicationSurfaces:
    """Consume resources from a single ordered stream in a single render thread.

    observe_created/unreferenced must see *all* CREATE/UNREF records in order.
    For an application that participates in this protocol its trusted producer
    must register the resource before CREATE reaches the consumer. An unbound
    surface can never be replaced here. This class does not classify other
    desktop resources or confer a pixel-free guarantee on the whole stream.
    """

    def __init__(self, session: bytes, binding_for: Callable, commit_for: Callable,
                 read_picture: Callable, rasterize: Callable):
        if type(session) is not bytes or len(session) != 32:
            raise SurfaceError('invalid paired desktop session')
        self.session = session
        self.binding_for = binding_for
        self.commit_for = commit_for
        self.read_picture = read_picture
        self.rasterize = rasterize
        self.creation = 0
        self.targets = {}

    def observe_created(self, descriptor):
        handle, target, fmt, _, width, height, depth, layers, mip, samples = descriptor
        self.creation += 1
        if handle in self.targets:
            raise SurfaceError('duplicate live application resource')
        binding = self.binding_for(self.session, handle, self.creation)
        if binding is None:
            return
        self.bind_untouched_resource(binding, descriptor, self.creation)

    def bind_untouched_resource(self, binding, descriptor, creation):
        """The stream parser must verify the current CREATE and absence of prior uploads."""
        handle, target, fmt, _, width, height, depth, layers, mip, samples = descriptor
        if (handle in self.targets or not isinstance(binding, Binding) or binding.session != self.session
                or binding.handle != handle or binding.creation != creation
                or type(binding.executable_sha256) is not bytes or len(binding.executable_sha256) != 32
                or type(binding.process_instance) is not bytes or len(binding.process_instance) != 32
                or min(binding.pid, binding.surface_id) <= 0):
            raise SurfaceError('application binding does not match this resource lifetime')
        # The demonstrated bridge handles an opaque R8G8B8A8_UNORM 2D window.
        if ((target, fmt, depth, layers, mip, samples) != (2, 67, 1, 1, 0, 0)
                or not (0 < width <= 8192 and 0 < height <= 8192)
                or width * height * 4 > 32 << 20 or len(self.targets) >= 32):
            raise SurfaceError('unsupported application surface')
        self.targets[handle] = _Target(binding, width, height)

    def observe_unreferenced(self, handle):
        self.targets.pop(handle, None)

    def check_raw_upload(self, descriptor):
        if descriptor[0] in self.targets:
            self.targets[descriptor[0]].ready = False
            raise SurfaceError('a bound application resource received a raster upload')

    @property
    def can_present(self):
        return all(target.ready for target in self.targets.values())

    def _target(self, handle):
        target = self.targets.get(handle)
        if target is None:
            raise SurfaceError('no verified application resource')
        return target

    def wait_for_picture(self, handle):
        self._target(handle).ready = False

    def local_texture(self, descriptor, ticket):
        """Resolve one commit through trusted session state and rasterize locally.

        ticket is an opaque reference, never an assertion of provenance.
        read_picture must bound its fetch to its second argument. rasterize
        executes the supplied original Frame and returns row-major RGBA bytes.
        """
        handle, level, stride, layer_stride, x, y, z, width, height, depth, size = descriptor
        target = self._target(handle)
        target.ready = False
        if type(ticket) is not bytes or len(ticket) != 32:
            raise SurfaceError('invalid application commit reference')
        commit = self.commit_for(self.session, ticket)
        if (not isinstance(commit, Commit) or commit.binding != target.binding or commit.serial <= 0
                or type(commit.picture_sha256) is not bytes or len(commit.picture_sha256) != 32):
            raise SurfaceError('unverified application commit')
        if target.last and (commit.serial < target.last.serial or
                            (commit.serial == target.last.serial and commit != target.last)):
            raise SurfaceError('stale or conflicting application commit')
        # A small Wayland damage rectangle still refers to a full application
        # commit. Regenerate that full texture; an earlier unpaired commit may
        # have left pixels outside the damage rectangle unavailable.
        row = stride or width * 4
        if (level != 0 or z != 0 or depth != 1 or min(x, y) < 0 or min(width, height) <= 0
                or x + width > target.width or y + height > target.height or row < width * 4
                or not (row * (height - 1) + width * 4 <= size <= row * height)
                or size > 32 << 20 or layer_stride not in (0, size)):
            raise SurfaceError('unsupported application damage layout')
        width, height = target.width, target.height
        size = width * height * 4
        upper = MAX_PAYLOAD + 12
        wire = self.read_picture(commit.picture_sha256, upper)
        if type(wire) is not bytes or len(wire) > upper or hashlib.sha256(wire).digest() != commit.picture_sha256:
            raise SurfaceError('original drawing digest mismatch')
        raw = io.BytesIO(wire)
        frame = read_message(raw, expected=Frame)
        if frame is None or raw.read(1) or (frame.width, frame.height) != (width, height):
            raise SurfaceError('original drawing does not cover the bound surface')
        pixels = self.rasterize(frame)
        if type(pixels) is not bytes or len(pixels) != size or pixels[3::4].count(255) != width*height:
            raise SurfaceError('renderer returned an incomplete or translucent application texture')
        target.last = commit
        target.ready = True
        return (handle, 0, width * 4, 0, 0, 0, 0, width, height, 1, size), pixels


@dataclass(frozen=True)
class SurfaceStamp:
    """The commit counter of the identified and presentable Wayland surface."""
    binding: Binding
    serial: int


class CommitTracker:
    """A gap or reordering invalidates outstanding captures until a verified read."""

    def __init__(self, binding):
        self.binding = binding
        self.current = None
        self.high_water = 0
        self.generation = 0

    def synchronize(self, stamp):
        # Caller has read the complete current state through the source's
        # authenticated presentation barrier. A queued notification is insufficient.
        if stamp.binding != self.binding or stamp.serial < max(1, self.high_water):
            raise SurfaceError('invalid authoritative surface state')
        self.generation += 1
        self.current = stamp
        self.high_water = stamp.serial

    def notify(self, stamp):
        if stamp.binding != self.binding:
            return  # Other windows can update independently.
        previous = self.current
        self.high_water = max(self.high_water, stamp.serial)
        if previous is None or stamp.serial != previous.serial + 1:
            self.current = None
            self.generation += 1
        else:
            self.current = stamp

    def checkpoint(self):
        return (self.generation, self.current) if self.current else None


async def capture_stable(binding, read_checkpoint, capture_frame, *, attempts=3):
    """Bracket each original application draw capture with an authoritative read.

    read_checkpoint must fence/drain the actual source observer so both reads
    account for every intervening wl_surface.commit. The first value identifies
    the uninterrupted observation lifetime. A missing checkpoint, a changed
    surface, or a changed revision never authorizes a picture. Returning None
    keeps presentation pending until a new verified source revision is available.
    """
    if not 1 <= attempts <= 8:
        raise ValueError('invalid capture attempt limit')
    for _ in range(attempts):
        before = await read_checkpoint()
        if before is None or before[1].binding != binding:
            continue
        frame = await capture_frame()
        after = await read_checkpoint()
        if after != before:
            continue
        if not isinstance(frame, Frame):
            raise SurfaceError('source did not return an original drawing')
        wire = encode_message(frame)
        return Commit(binding, before[1].serial, hashlib.sha256(wire).digest()), wire
    return None
