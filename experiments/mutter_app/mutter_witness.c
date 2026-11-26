/* Experimental source-only observer for a dedicated Mutter 46/Mesa vtest 0.
 * Use the dynamic loader's --preload on gnome-shell only. No library is
 * loaded into the original application. The public Wayland protocol logger
 * supplies peer identity and roles; Cogl supplies the actual uploaded texture.
 * A byte offset ties a PUT to the same unique outgoing socket in the archive.
 * The observer emits metadata and SHA-256 only, never a copy of a window.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <openssl/sha.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <unistd.h>

struct message {
    const char *name, *signature;
    const void **types;
};
union arg {
    int32_t i;
    uint32_t u;
    const char *s;
    void *o;
};
struct msg {
    void *resource;
    int opcode;
    const struct message *message;
    int argc;
    const union arg *args;
};
struct link {
    struct link *prev, *next;
};
struct listener {
    struct link link;
    void (*notify)(struct listener *, void *);
};
struct connection {
    struct listener gone;
    void *client;
    uint32_t seq;
};
#ifdef CW_POINTER_DIAGNOSTICS
static void say(const char *fmt, ...);
#endif
static struct connection connections[32];
static uint32_t next_connection;
static void client_gone(struct listener *l, void *d) {
    struct connection *c = (void *)l;
#ifdef CW_POINTER_DIAGNOSTICS
    say("POINTER_DISCONNECT %u\n", c->seq);
#endif
    c->client = NULL;
}
struct ownership {
    void *shm;
    int pid;
    uint32_t surface, buffer;
    uint64_t attach;
};
static struct ownership table[1024];
static __thread struct ownership active;
static uint64_t generation;
static __thread int awaiting_descriptor;
static __thread int other_shm;
static __thread void *shmptr;
static __thread void *pendingtex;
static void *attached_surface[1024];

static uint32_t attach_con[1024];
struct life {
    struct listener dead;
    void *resource, *shm;
};
static struct life lives[1024];
static void buffer_gone(struct listener *l, void *d) {
    struct life *t = (void *)l;
    for (int i = 0; i < 1024; i++) {
        if (table[i].shm == t->shm) {
            table[i] = (struct ownership){0};
            attached_surface[i] = NULL;
        }
    }
    t->resource = NULL;
    t->shm = NULL;
}
typedef void (*add_listener)(void *, struct listener *);
static uint32_t client_seq(void *c) {
    for (int i = 0; i < 32; i++)
        if (connections[i].client == c) return connections[i].seq;
    for (int i = 0; i < 32; i++)
        if (!connections[i].client) {
            connections[i].client = c;
            connections[i].seq = ++next_connection;
            connections[i].gone.notify = client_gone;
            add_listener add = (void *)dlsym(RTLD_NEXT, "wl_client_add_destroy_listener");
            add(c, &connections[i].gone);
            return connections[i].seq;
        }
    return 0;
}
static int out = -1, count;
static void say(const char *fmt, ...) {
    if (out < 0) return;
    char row[420];
    va_list a;
    va_start(a, fmt);
    int n = vsnprintf(row, sizeof row, fmt, a);
    va_end(a);
    if (n > 0 && n < (int)sizeof row) syscall(SYS_write, out, row, n);
}
#define FN(type, name) \
    static type name;  \
    if (!name) name = (void *)dlsym(RTLD_NEXT, #name)
typedef void *(*one)(void *);
typedef uint32_t (*idfn)(void *);
typedef const char *(*strfn)(void *);
typedef void (*creds)(void *, pid_t *, uid_t *, gid_t *);
static int vendor(pid_t pid) {
    struct stat candidate, installed;
    char path[80];
    snprintf(path, sizeof path, "/proc/%d/exe", pid);
    return stat(path, &candidate) == 0 && stat("/usr/lib/chatgpt/ChatGPT", &installed) == 0 &&
           candidate.st_ino == installed.st_ino && candidate.st_dev == installed.st_dev;
}
#ifdef CW_POINTER_DIAGNOSTICS
/* Optional source-only diagnostics for the supported Mutter 46 ABI. All functions
 * below are accessors. Nothing in the input or drawing path uses these records. */
struct actor_rectangle { int x, y, width, height; };
typedef int (*integer_getter)(void *);
typedef int (*type_test)(void *, unsigned long);
typedef unsigned long (*type_getter)(void);
typedef void (*rect_getter)(void *, struct actor_rectangle *);
typedef void (*point_getter)(void *, float *, float *);

static void note_placement(void *resource, uint64_t attach, uint32_t client, pid_t pid, uint32_t id) {
    FN(one, wl_resource_get_user_data);
    FN(one, meta_wayland_surface_get_resource);
    FN(one, meta_wayland_surface_get_window);
    FN(one, meta_wayland_surface_get_actor);
    FN(one, meta_window_get_wayland_surface);
    FN(integer_getter, meta_window_get_pid);
    FN(integer_getter, clutter_actor_is_mapped);
    FN(type_test, g_type_check_instance_is_a);
    FN(type_getter, meta_wayland_surface_get_type);
    FN(rect_getter, meta_window_get_buffer_rect);
    FN(rect_getter, meta_window_get_frame_rect);
    FN(point_getter, clutter_actor_get_transformed_position);
    FN(point_getter, clutter_actor_get_transformed_size);

    if (!wl_resource_get_user_data || !meta_wayland_surface_get_resource || !meta_wayland_surface_get_window ||
        !meta_wayland_surface_get_actor || !meta_window_get_wayland_surface || !meta_window_get_pid ||
        !clutter_actor_is_mapped || !g_type_check_instance_is_a || !meta_wayland_surface_get_type ||
        !meta_window_get_buffer_rect || !meta_window_get_frame_rect || !clutter_actor_get_transformed_position ||
        !clutter_actor_get_transformed_size) return;
    void *surface = wl_resource_get_user_data(resource);
    if (!surface || !g_type_check_instance_is_a(surface, meta_wayland_surface_get_type()) ||
        meta_wayland_surface_get_resource(surface) != resource) return;
    void *window = meta_wayland_surface_get_window(surface);
    void *actor = meta_wayland_surface_get_actor(surface);
    if (!window || meta_window_get_wayland_surface(window) != surface || meta_window_get_pid(window) != pid ||
        !actor || !clutter_actor_is_mapped(actor)) return;

    struct actor_rectangle buffer, frame;
    float x, y, width, height;
    meta_window_get_buffer_rect(window, &buffer);
    meta_window_get_frame_rect(window, &frame);
    clutter_actor_get_transformed_position(actor, &x, &y);
    clutter_actor_get_transformed_size(actor, &width, &height);
    say("PLACEMENT %u %d %u %lu %d %d %d %d %d %d %d %d %.3f %.3f %.3f %.3f\n",
        client, pid, id, attach, buffer.x, buffer.y, buffer.width, buffer.height,
        frame.x, frame.y, frame.width, frame.height, x, y, width, height);
}

static void note_pointer(const struct msg *m, const char *op, uint32_t client, pid_t pid, uint32_t id) {
    FN(idfn, wl_resource_get_id);
    FN(one, wl_resource_get_client);
    if (!strcmp(op, "enter") || !strcmp(op, "leave")) {
        if (m->argc < 2 || !m->args[1].o ||
            wl_resource_get_client(m->resource) != wl_resource_get_client(m->args[1].o)) return;
        uint32_t surface = wl_resource_get_id(m->args[1].o);
        if (!strcmp(op, "enter") && m->argc == 4)
            say("POINTER_ENTER %u %d %u %u %u %d %d\n", client, pid, id, surface,
                m->args[0].u, m->args[2].i, m->args[3].i);
        else if (!strcmp(op, "leave") && m->argc == 2)
            say("POINTER_LEAVE %u %d %u %u %u\n", client, pid, id, surface, m->args[0].u);
    } else if (!strcmp(op, "motion") && m->argc == 3) {
        say("POINTER_MOTION %u %d %u %u %d %d\n", client, pid, id,
            m->args[0].u, m->args[1].i, m->args[2].i);
    } else if (!strcmp(op, "button") && m->argc == 4) {
        say("POINTER_BUTTON %u %d %u %u %u %u %u\n", client, pid, id,
            m->args[0].u, m->args[1].u, m->args[2].u, m->args[3].u);
    }
}
#endif

static void observe(void *data, int direction, const struct msg *m) {
#ifndef CW_POINTER_DIAGNOSTICS
    if (direction != 0) return;
#endif
    if (!m || !m->resource || !m->message || !m->message->name) return;
    FN(strfn, wl_resource_get_class);
    FN(one, wl_resource_get_client);
    FN(creds, wl_client_get_credentials);
    FN(idfn, wl_resource_get_id);
    FN(one, wl_shm_buffer_get);
    const char *type = wl_resource_get_class(m->resource), *op = m->message->name;
#ifdef CW_POINTER_DIAGNOSTICS
    if (direction != 0 && (direction != 1 || strcmp(type, "wl_pointer"))) return;
#endif
    if (strcmp(type, "wl_surface") && strcmp(type, "xdg_wm_base") && strcmp(type, "xdg_surface") &&
        strcmp(type, "xdg_toplevel") && strcmp(type, "zxdg_toplevel_decoration_v1") &&
        strcmp(type, "wl_pointer"))
        return;
    void *client = wl_resource_get_client(m->resource);
    pid_t pid = 0;
    uid_t uid = (uid_t)-1;
    wl_client_get_credentials(client, &pid, &uid, NULL);
    if (uid != geteuid() || !vendor(pid)) return;
    uint32_t seq = client_seq(client), id = wl_resource_get_id(m->resource);
    if (!seq) return;
#ifdef CW_POINTER_DIAGNOSTICS
    if (direction == 1) {
        note_pointer(m, op, seq, pid, id);
        return;
    }
    if (!strcmp(type, "wl_pointer") && !strcmp(op, "release"))
        say("POINTER_RELEASE %u %d %u\n", seq, pid, id);
    if (!strcmp(type, "wl_surface") && !strcmp(op, "destroy"))
        say("POINTER_SURFACE_GONE %u %d %u\n", seq, pid, id);
#endif
    if (!strcmp(type, "xdg_wm_base") && !strcmp(op, "get_xdg_surface") && m->argc == 2) {
        say("XDG_SURFACE %u %d %u %u\n", seq, pid, m->args[0].u, wl_resource_get_id(m->args[1].o));
        return;
    }
    if (!strcmp(type, "xdg_surface") && !strcmp(op, "get_toplevel") && m->argc == 1) {
        say("TOPLEVEL %u %u %u\n", seq, id, m->args[0].u);
        return;
    }
    if (!strcmp(type, "xdg_toplevel") && !strcmp(op, "set_app_id") && m->argc == 1) {
        const char *app = m->args[0].s;
        say("APP_ID %u %u %s\n", seq, id, app && strlen(app) < 80 ? app : "other");
        return;
    }
    if (!strcmp(type, "zxdg_toplevel_decoration_v1") && !strcmp(op, "set_mode")) {
        say("DECORATION %u %u %u\n", seq, id, m->args[0].u);
        return;
    }
    if (!strcmp(type, "wl_pointer") && !strcmp(op, "set_cursor") && m->argc >= 2 && m->args[1].o) {
        say("CURSOR %u %u\n", seq, wl_resource_get_id(m->args[1].o));
        return;
    }
    if (strcmp(type, "wl_surface")) return;
    if (!strcmp(op, "commit")) {
        for (int j = 0; j < count; j++)
            if (attached_surface[j] == m->resource && attach_con[j] == seq) {
                say("COMMIT %u %d %u %lu\n", seq, pid, id, table[j].attach);
#ifdef CW_POINTER_DIAGNOSTICS
                note_placement(m->resource, table[j].attach, seq, pid, id);
#endif
                attached_surface[j] = NULL;
            }
        return;
    }
    if (strcmp(op, "attach") && strcmp(op, "destroy")) return;
    // A second attach before commit supersedes the first pending buffer.
    for (int i = 0; i < count; ++i) {
        if (attached_surface[i] == m->resource && attach_con[i] == seq) attached_surface[i] = NULL;
    }
    if (strcmp(op, "attach") || m->argc < 1 || !m->args[0].o) return;
    void *buf = m->args[0].o, *shm = wl_shm_buffer_get(buf);
    if (!shm) return;
    int live = 0;
    for (int i = 0; i < 1024; i++)
        if (lives[i].resource == buf) {
            live = 1;
            break;
        }
    if (!live) {
        for (int i = 0; i < 1024; i++)
            if (!lives[i].resource) {
                lives[i].resource = buf;
                lives[i].shm = shm;
                lives[i].dead.notify = buffer_gone;
                add_listener add = (void *)dlsym(RTLD_NEXT, "wl_resource_add_destroy_listener");
                add(buf, &lives[i].dead);
                live = 1;
                break;
            }
    }
    if (!live) return;
    int j = 0;
    while (j < count && table[j].shm != shm) ++j;
    if (j == count) {
        j = 0;
        while (j < count && table[j].shm) ++j;
        if (j == 1024) return;
        if (j == count) ++count;
    }
    table[j] = (struct ownership){shm, pid, wl_resource_get_id(m->resource),
                                  wl_resource_get_id(buf), ++generation};
    attached_surface[j] = m->resource;
    attach_con[j] = seq;
    struct stat fi;
    if (stat("/usr/lib/chatgpt/ChatGPT", &fi)) return;
    char path[64], row[1024];
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    unsigned long long start = 0;
    FILE *st = fopen(path, "r");
    if (st) {
        if (fgets(row, sizeof row, st)) {
            char *cp = strrchr(row, ')');
            char *save = NULL;
            char *t = cp ? strtok_r(cp + 2, " ", &save) : NULL;
            int field = 3;
            while (t && field < 22) {
                t = strtok_r(NULL, " ", &save);
                field++;
            }
            if (t) start = strtoull(t, NULL, 10);
        }
        fclose(st);
    }
    say("PEER %u %d %llu %lu %lu %u %u %lu\n", seq, pid, start, (unsigned long)fi.st_dev,
        (unsigned long)fi.st_ino, table[j].surface, table[j].buffer, table[j].attach);
    say("ATTACH %d %u %u %lu\n", pid, table[j].surface, table[j].buffer, table[j].attach);
}
typedef void *(*createfn)(void);
typedef void *(*logger)(void *, void (*)(void *, int, const struct msg *), void *);
void *wl_display_create(void) {
    FN(createfn, wl_display_create);
    FN(logger, wl_display_add_protocol_logger);
    void *d = wl_display_create();
    const char *log = getenv("CW_MUTTER_WITNESS");
    if (log && out < 0) out = open(log, O_WRONLY | O_CLOEXEC | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (d) wl_display_add_protocol_logger(d, observe, NULL);
    return d;
}
typedef void (*enterfn)(void *);
typedef int (*intfn)(void *);
typedef void (*finishfn)(void);
static void fence(void) {
    static void *lib;
    static finishfn finish;
    if (!lib) {
        lib = dlopen("libGL.so.1", RTLD_NOW | RTLD_LOCAL);
        if (lib) finish = dlsym(lib, "glFinish");
    }
    if (finish) finish();
}
void wl_shm_buffer_begin_access(void *s) {
    FN(enterfn, wl_shm_buffer_begin_access);
    fence();  // flush shell-originated work before attributing this SHM access
    wl_shm_buffer_begin_access(s);
    other_shm = 1;
    for (int i = 0; i < count; i++)
        if (table[i].shm == s) {
            other_shm = 0;
            shmptr = NULL;
            pendingtex = NULL;
            active = table[i];
            say("ACCESS %d %u %u %lu %ld\n", active.pid, active.surface, active.buffer,
                active.attach, syscall(SYS_gettid));
            return;
        }
    active = (struct ownership){0};
}
void wl_shm_buffer_end_access(void *s) {
    FN(enterfn, wl_shm_buffer_end_access);
    fence();
    other_shm = 0;
    if (active.shm && active.shm == s) {
        awaiting_descriptor = 0;
        if (pendingtex) {
            typedef int (*getgl)(void *, unsigned *, unsigned *);
            FN(getgl, cogl_texture_get_gl_texture);
            unsigned glid = 0, target = 0;
            if (cogl_texture_get_gl_texture(pendingtex, &glid, &target))
                say("GL %lu %u %u\n", active.attach, glid, target);
        }
        say("END %lu\n", active.attach);
        active = (struct ownership){0};
    }
    wl_shm_buffer_end_access(s);
}
void *wl_shm_buffer_get_data(void *s) {
    FN(one, wl_shm_buffer_get_data);
    void *p = wl_shm_buffer_get_data(s);
    if (!active.shm || s != active.shm) return p;
    shmptr = p;
    FN(intfn, wl_shm_buffer_get_stride);
    FN(intfn, wl_shm_buffer_get_height);
    int stride = wl_shm_buffer_get_stride(s), h = wl_shm_buffer_get_height(s);
    if (p && stride > 0 && h > 0 && (uint64_t)stride * h <= 12 * 1024 * 1024) {
        unsigned char d[32];
        char hex[65];
        SHA256(p, (size_t)stride * h, d);
        for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", d[i]);
        say("SHM %lu %d %d %s\n", active.attach, stride, h, hex);
    }
    return p;
}
static pthread_mutex_t wire_lock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t byte_cursor;
static __thread uint64_t pending_offset;

static int source_socket(int fd) {
    const char *source = getenv("VTEST_SOCKET_NAME");
    if (!source || *source != '/') return 0;
    struct sockaddr_un peer = {0};
    socklen_t size = sizeof peer;
    return !getpeername(fd, (struct sockaddr *)&peer, &size) && peer.sun_family == AF_UNIX &&
           peer.sun_path[sizeof(peer.sun_path) - 1] == 0 && !strcmp(peer.sun_path, source);
}
ssize_t write(int fd, const void *buf, size_t count) {
    int saved = errno;
    if (!source_socket(fd)) {
        errno = saved;
        return syscall(SYS_write, fd, buf, count);
    }
    pthread_mutex_lock(&wire_lock);
    uint64_t offset = byte_cursor;
    errno = saved;
    ssize_t n = syscall(SYS_write, fd, buf, count);
    saved = errno;
    if (n > 0) byte_cursor += (uint64_t)n;
    if (n == (ssize_t)count && count >= 8 && count <= 44) {
        const uint32_t *a = buf;
        if (count == 8 && a[1] == 5) {
            awaiting_descriptor = 1;
            pending_offset = offset;
        }
        if (count == 44 && awaiting_descriptor && pending_offset + 8 == offset) {
            if (active.shm) {
                say("WIRE %lu %" PRIu64 " %u %u %u %u %u %u\n", active.attach, pending_offset, a[0],
                    a[4], a[5], a[7], a[8], a[10]);
            } else {
                // A true PUT must resolve at its precise offset in the relay's
                // independent parser. Other client SHM must not be sent as shell paint.
                say("ORIGIN %" PRIu64 " %s %u %u %u %u %u %u\n", pending_offset,
                    other_shm ? "UNCLASSIFIED_SHM" : "SHELL", a[0], a[4], a[5], a[7], a[8], a[10]);
            }
            awaiting_descriptor = 0;
        }
    }
    pthread_mutex_unlock(&wire_lock);
    errno = saved;
    return n;
}
typedef int (*regionfn)(void *, int, int, int, int, const unsigned char *, int, int, int, void *);
int _cogl_texture_set_region(void *t, int w, int h, int format, int stride,
                             const unsigned char *data, int x, int y, int level, void *error) {
    FN(regionfn, _cogl_texture_set_region);
    if (active.shm && shmptr && x >= 0 && y >= 0 && stride > 0 &&
        (uintptr_t)data - (uintptr_t)shmptr ==
            (uint64_t)(unsigned)x * 4 + (uint64_t)(unsigned)y * (unsigned)stride) {
        pendingtex = t;
        say("COGL_REGION %lu %p %d %d %d %d\n", active.attach, t, x, y, w, h);
    }
    return _cogl_texture_set_region(t, w, h, format, stride, data, x, y, level, error);
}
void *cogl_texture_2d_new_from_bitmap(void *b) {
    FN(one, cogl_texture_2d_new_from_bitmap);
    void *t = cogl_texture_2d_new_from_bitmap(b);
    if (active.shm) {
        pendingtex = t;
        say("COGL_CREATE %lu %p\n", active.attach, t);
    }
    return t;
}
