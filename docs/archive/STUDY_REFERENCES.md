# Hyper-DERP Study References & Architecture Decisions

## Part 1: Documentation Links by Topic

### io_uring

The core of the data plane. HD uses advanced io_uring
features that most projects don't touch.

**Essential reading (start here):**
- Lord of the io_uring (tutorial):
  https://unixism.net/loti/
- liburing API reference (man pages):
  https://man7.org/linux/man-pages/man7/io_uring.7.html
- Kernel docs:
  https://docs.kernel.org/filesystems/fuse-io-uring.html

**Features used by HD and what to read for each:**

| Feature | Where in HD | What to read |
|---------|-------------|--------------|
| Basic submit/complete | data_plane.cc:1760 (CQE loop) | io_uring(7) man page |
| Provided buffer rings | data_plane.cc:2028 (setup), 1092 (consume) | `man io_uring_setup_buf_ring`, `man io_uring_buf_ring_add` |
| Multishot recv | data_plane.cc:715 (IORING_RECV_MULTISHOT) | `man io_uring_prep_recv` — look for IOSQE_BUFFER_SELECT |
| SEND_ZC | data_plane.cc:784 | `man io_uring_prep_send_zc` |
| DEFER_TASKRUN | data_plane.cc:1966 | `man io_uring_setup` — IORING_SETUP_DEFER_TASKRUN |
| SINGLE_ISSUER | data_plane.cc:1965 | `man io_uring_setup` — IORING_SETUP_SINGLE_ISSUER |
| SQPOLL | data_plane.cc:1940 | `man io_uring_setup` — IORING_SETUP_SQPOLL |
| Fixed file table | data_plane.cc:2008 | `man io_uring_register_files` |
| CQE notification (ZC) | data_plane.cc:1303 (IORING_CQE_F_NOTIF) | io_uring send_zc docs |

**Deep dive resources:**
- Jens Axboe's io_uring notes:
  https://kernel.dk/io_uring.pdf
- io_uring by example (practical patterns):
  https://github.com/shuveb/io_uring-by-example
- Provided buffers explained:
  https://lwn.net/Articles/815491/

**Key concepts to understand:**
1. SQE (submission queue entry) vs CQE (completion queue
   entry) — the async model
2. Why one io_uring per worker thread (not shared)
3. Provided buffer rings: kernel picks a buffer from your
   ring for each recv completion — zero-copy receive
4. Multishot: one SQE produces many CQEs (recv stays
   armed until error or cancel)
5. DEFER_TASKRUN: completions only processed when you
   call io_uring_submit() or io_uring_wait_cqe() —
   gives you control over when work happens
6. SINGLE_ISSUER: tells kernel only one thread submits,
   enables internal optimizations
7. SEND_ZC: kernel sends directly from your buffer
   without copying — but you can't reuse the buffer
   until IORING_CQE_F_NOTIF arrives

### kTLS (Kernel TLS)

kTLS moves TLS record encryption/decryption from
userspace (OpenSSL) into the kernel. After setup, the
socket fd does TLS transparently — read/write/sendmsg
operate on plaintext, kernel handles crypto.

**Essential reading:**
- Kernel kTLS documentation:
  https://docs.kernel.org/networking/tls.html
- kTLS offload explanation:
  https://docs.kernel.org/networking/tls-offload.html
- OpenSSL kTLS integration:
  https://www.openssl.org/docs/man3.0/man3/SSL_CTX_set_options.html
  (search for SSL_OP_ENABLE_KTLS)

**How HD uses kTLS** (ktls.cc):
1. OpenSSL does TLS handshake (normal SSL_accept)
2. After handshake, OpenSSL auto-installs kTLS via
   setsockopt(SOL_TLS) if SSL_OP_ENABLE_KTLS is set
3. HD verifies: BIO_get_ktls_send() / BIO_get_ktls_recv()
4. SSL object freed, fd retained — kernel now owns crypto
5. io_uring send/recv on the fd operates on plaintext

**Key concept:** After kTLS installation, the fd behaves
like a normal TCP socket from userspace's perspective.
The kernel encrypts on send and decrypts on recv. This
is why io_uring can use the fd directly — it doesn't know
or care that TLS is happening.

**Profiling context:** The flame graph showed 25% of cycles
in aes_gcm_enc/dec — that's kTLS doing AES-GCM in the
kernel via AES-NI instructions. Hardware TLS offload
(ConnectX-5/6) moves this to the NIC.

### NaCl / libsodium (DERP handshake crypto)

The DERP protocol uses NaCl public-key cryptography for
peer authentication. This is separate from TLS — TLS
secures the transport, NaCl authenticates DERP peers.

**Essential reading:**
- NaCl overview: https://nacl.cr.yp.to/
- libsodium docs (modern NaCl implementation):
  https://doc.libsodium.org/
- Specifically crypto_box (public-key authenticated
  encryption):
  https://doc.libsodium.org/public-key_cryptography/authenticated_encryption

**Functions used:**
- `crypto_box_keypair()` — generate public/private key
  pair (Curve25519)
- `crypto_box_easy()` — encrypt + authenticate a message
  to a recipient's public key
- `crypto_box_open_easy()` — decrypt + verify

**Handshake flow** (handshake.cc):
1. Server sends ServerKey (its public key, plaintext)
2. Client sends ClientInfo (its public key + mesh key,
   encrypted to server's public key via crypto_box)
3. Server decrypts, verifies, sends ServerInfo (encrypted
   to client's public key)
4. Both sides now know each other's public keys
5. Public key becomes the peer's identity in the relay

### OpenSSL

Used only for TLS handshake setup before kTLS takes over.
After kTLS installation, OpenSSL is not in the hot path.

**Essential reading:**
- OpenSSL TLS tutorial:
  https://wiki.openssl.org/index.php/Simple_TLS_Server
- SSL_CTX_new / SSL_new lifecycle:
  https://www.openssl.org/docs/man3.0/man3/SSL_CTX_new.html

**HD's OpenSSL usage** (ktls.cc):
- TLS 1.3 only (`TLS1_3_VERSION`)
- Ciphersuites restricted to AES-GCM (for kTLS compat)
- No client certificate verification (DERP handles auth)
- After handshake: check kTLS installed, set BIO_NOCLOSE,
  free SSL object, keep the fd

### Linux Kernel Interfaces

**epoll** (control_plane.cc):
- `man epoll` — https://man7.org/linux/man-pages/man7/epoll.7.html
- Used only in control plane to multiplex worker pipe reads
- Not used in data plane (io_uring replaces epoll there)

**eventfd** (data_plane.cc:1876):
- `man eventfd` — https://man7.org/linux/man-pages/man2/eventfd.2.html
- Used for cross-shard signaling: worker writes eventfd
  to wake destination worker's io_uring
- EFD_NONBLOCK: write never blocks (just accumulates)

**madvise / MADV_HUGEPAGE** (data_plane.cc:395, 450, 2024):
- `man madvise` — https://man7.org/linux/man-pages/man2/madvise.2.html
- Hints to kernel: use transparent huge pages for frame
  pool and provided buffer ring allocations
- Reduces TLB misses on large contiguous allocations

**setsockopt for TCP tuning:**
- TCP_NODELAY: disable Nagle (server.cc:77, client.cc:129)
  https://man7.org/linux/man-pages/man7/tcp.7.html
- SO_SNDBUF/SO_RCVBUF: socket buffer sizes (data_plane.cc:1547)
  https://man7.org/linux/man-pages/man7/socket.7.html
- MSG_MORE: tell kernel more data coming, don't send yet
  https://man7.org/linux/man-pages/man2/send.2.html
- MSG_NOSIGNAL: don't SIGPIPE on broken connection
  https://man7.org/linux/man-pages/man2/send.2.html

### Lock-Free Programming

The SPSC rings and atomic operations are the hardest
part of the codebase to reason about correctly.

**Essential reading:**
- Preshing on Programming — memory ordering series:
  https://preshing.com/20120913/acquire-and-release-semantics/
  https://preshing.com/20120612/an-introduction-to-lock-free-programming/
- C++ memory model:
  https://en.cppreference.com/w/cpp/atomic/memory_order
- SPSC ring buffer correctness:
  https://www.codeproject.com/Articles/43510/Lock-Free-Single-Producer-Single-Consumer-Circular

**HD's atomic patterns:**
- `__ATOMIC_ACQUIRE` on loads: ensures you see all writes
  that happened before the corresponding release store
- `__ATOMIC_RELEASE` on stores: ensures all your prior
  writes are visible to anyone who acquire-loads this
- Used for SPSC ring head/tail, route table occupied
  flags, generation counters
- GCC builtins (`__atomic_load_n`) instead of std::atomic
  in hot path — same codegen, avoids std::atomic wrapper
  overhead in debug builds

### C++23 Features

**std::expected** (error handling throughout):
- https://en.cppreference.com/w/cpp/utility/expected
- Replaces exceptions and error codes with a type-safe
  value-or-error return type
- HD uses `std::expected<T, Error<E>>` everywhere

**std::print / std::println** (main.cc, bench.cc):
- https://en.cppreference.com/w/cpp/io/print
- Type-safe printf replacement with std::format syntax

### DERP Protocol

The wire protocol HD implements.

**Tailscale DERP source (reference implementation):**
- Protocol definition:
  https://github.com/tailscale/tailscale/blob/main/derp/derp.go
- Server implementation:
  https://github.com/tailscale/tailscale/blob/main/derp/derp_server.go
- Client implementation:
  https://github.com/tailscale/tailscale/blob/main/derp/derphttp/derphttp_client.go

**Frame format:**
```
[1 byte: frame type][4 bytes: length (big-endian)][payload]
```

**Key frame types:**
- SendPacket: client → relay (dst key + data)
- RecvPacket: relay → client (src key + data)
- PeerPresent / PeerGone: presence notifications
- Ping / Pong: keepalive (32-byte payload echo)

---

## Part 2: Architectural Decisions

Every non-obvious design choice, why it was made, and
what the alternative was.

### D1: io_uring over epoll

**Decision:** Use io_uring for the entire data plane.

**Why:** epoll is notification-only — it tells you a fd is
readable, then you call read(). That's two syscalls per
event. io_uring batches submissions and completions: one
io_uring_enter() can submit 256 sends and reap 256 recv
completions. At 780K pps (8.7 Gbps / 1400B), the syscall
reduction is the difference between 1.6M syscalls/s (epoll)
and ~3K/s (io_uring batched).

**Alternative:** epoll + read/write. Simpler code, but
the syscall overhead dominates at high packet rates.
The Tailscale Go derper uses goroutines + epoll under
the hood — its 963K context switches and 0.64 IPC show
the cost.

**Evidence:** HD achieves 1.22 IPC with 3K context
switches. TS achieves 0.64 IPC with 963K context switches.
The io_uring batching model is why.

### D2: Sharded workers over goroutine-per-connection

**Decision:** N worker threads, each owning a disjoint
peer set, each with its own io_uring instance.

**Why:** Goroutine-per-connection (Go model) means each
connection has its own stack, scheduler time slice, and
potential for migration between OS threads. At 20 peers,
that's 20 goroutines contending for scheduler time, plus
GC pauses. Sharded workers: each thread owns its peers,
runs a tight poll loop, never context-switches.

**Alternative:** Thread-per-connection (C++ equivalent
of goroutines). Same problems: scheduling overhead,
cache thrashing from thread migration, lock contention
on shared routing state.

**Trade-off:** Cross-shard forwarding is more complex
(need SPSC rings + eventfd), but the perf data proves
it's worth it: forwarding is invisible in the flame
graph.

### D3: SPSC rings over MPSC or locks

**Decision:** One SPSC ring per (source, destination)
worker pair. N workers = N² rings.

**Why:** MPSC (multiple producer, single consumer) requires
CAS on the head pointer. Under contention, CAS retries
waste cycles. SPSC (single producer, single consumer)
needs only acquire/release ordering on head and tail —
no CAS, no retry, no contention, ever.

**Alternative:** Single MPSC ring per worker (previous
design). Workers contended on the CAS when multiple
sources forwarded to the same destination simultaneously.
At 8+ workers, this caused measurable regression (TS
beat HD at 8 vCPU).

**Cost:** N² rings × 16384 entries × sizeof(Xfer) memory.
At 4 workers that's 16 rings, at 8 workers 64 rings.
Memory cost is acceptable; the zero-contention guarantee
is worth it.

**Evidence:** 8 vCPU results before SPSC: TS 0.93x HD.
After SPSC: pending revalidation, but the 4w Haswell
results show clean scaling.

### D4: Replicated route tables over shared

**Decision:** Each worker has its own copy of the full
routing table. When a peer connects, all workers get
a route update.

**Why:** The route table is read on every forwarded
packet (RouteLookup to find which worker owns the
destination). A shared table would need either a lock
(unacceptable in the hot path) or a concurrent hash map
(complex, cache-unfriendly). Replication means each
worker reads its own table with zero synchronization.

**Cost:** Route updates are rare (connect/disconnect)
and go through the command pipe. Reads are on every
packet. Optimizing for the common case (read) at the
expense of the rare case (write) is the right trade.

**Synchronization:** Route entries have an atomic
`occupied` flag with release/acquire ordering. The
writer stores the route data, then release-stores
occupied=1. The reader acquire-loads occupied — if 1,
all prior writes (the route data) are guaranteed visible.

### D5: FNV-1a hash

**Decision:** FNV-1a on 32-byte NaCl public keys for
peer hash table lookup and worker assignment.

**Why:** Simple, fast, good distribution on key-like
inputs. The profiling proved it right: hash lookup
is not visible in the flame graph (<0.5% of cycles).

**Alternative:** CRC32 (hardware-accelerated on x86),
AES-NI single-round hash. Both are faster per-byte
but add complexity. Since the hash is <0.5% of cycles,
there's zero reason to change it.

**When to revisit:** Only at 50GbE if per-packet budget
drops to ~220ns and hash becomes a measurable fraction.
The profiling data says this is unlikely.

### D6: Slab allocator + frame pool

**Decision:** Pre-allocate all memory at startup. Two
custom allocators: slab for SendItem nodes, frame pool
for 2KB recv buffers.

**Why:** malloc/free in the hot path is catastrophic.
glibc malloc takes a lock, fragments the heap, triggers
mmap/munmap under pressure. At 780K pps, even 100ns
per malloc = 78ms/s of allocation overhead.

**Slab allocator:** Fixed-size SendItem objects. Bump
allocation from a pre-allocated region, free list for
reuse. Zero syscalls, zero fragmentation.

**Frame pool:** Pre-allocated 16384 × 2KB buffers per
worker with MADV_HUGEPAGE hint. Used as io_uring
provided buffers for multishot recv.

**Trade-off:** Memory is allocated upfront whether used
or not. 16384 × 2KB = 32 MB per worker. At 4 workers
that's 128 MB. Acceptable for a relay server.

### D7: Provided buffer rings for recv

**Decision:** Use io_uring provided buffer rings instead
of pre-posting recv SQEs with fixed buffers.

**Why:** Without provided buffers, you submit one recv
SQE per buffer, each pointing to a pre-allocated buffer.
When the kernel completes a recv, you consume one SQE
slot. With provided buffer rings, the kernel picks a
buffer from your ring at completion time — one multishot
recv SQE handles unlimited completions, kernel chooses
buffers autonomously.

**Benefit:** Drastically fewer SQE submissions. One
multishot recv SQE stays armed until error. Buffer
management is the kernel's problem.

**Recovery:** When the provided ring runs empty, recv
returns ENOBUFS. HD re-arms the multishot recv after
returning buffers to the ring.

### D8: MSG_MORE over TCP_CORK

**Decision:** Use MSG_MORE flag on individual sends
instead of TCP_CORK setsockopt around batches.

**Why:** TCP_CORK requires two setsockopt syscalls per
batch (cork, uncork). MSG_MORE is a per-send flag — set
it on every send except the last in a batch. Same
coalescing effect, zero extra syscalls.

**How it works in HD:** FlushPendingSends iterates the
per-peer send queue. Every send except the last gets
MSG_MORE. The kernel holds the data until either the
send without MSG_MORE arrives, or a timeout expires.

**Interaction with io_uring:** MSG_MORE is passed as
a flag to io_uring_prep_send. The kernel sees it when
processing the SQE.

### D9: SEND_ZC only for frames >4KB

**Decision:** Use zero-copy send for large frames,
regular send for WireGuard-MTU (1400B) frames.

**Why:** SEND_ZC avoids the kernel copying your data
into an SKB. But it has overhead: the kernel must pin
your pages and send a notification CQE when the NIC
is done with the buffer. For small frames (1400B), the
copy cost (~200ns) is less than the ZC notification
tracking cost. For large frames (>4KB), the copy cost
exceeds the notification cost.

**Notification tracking:** When SEND_ZC is used, HD
increments a per-peer inflight counter. When
IORING_CQE_F_NOTIF arrives, it decrements. The buffer
can't be reused until notification arrives.

### D10: kTLS over userspace TLS

**Decision:** Let the kernel handle TLS via kTLS, not
OpenSSL in userspace.

**Why:** With userspace TLS, every send requires:
encrypt in userspace → copy to kernel → send. With
kTLS, the kernel encrypts during sendmsg — one fewer
copy, and the crypto happens where the data already is
(kernel socket buffer). More importantly, io_uring can
operate on the fd directly — it doesn't need to call
back into OpenSSL for every send/recv.

**Cost:** As profiling proved, kTLS consumes 25% of
cycles on Haswell (software AES-GCM). But without kTLS,
io_uring can't do async send/recv through TLS at all —
you'd need a thread calling SSL_read/SSL_write, which
defeats the entire io_uring architecture.

**kTLS is architecturally mandatory for io_uring + TLS,
not just a performance optimization.**

### D11: Generation counters for stale reference detection

**Decision:** Every peer slot has a generation counter
that increments on connect and disconnect.

**Why:** When peer A disconnects and peer B connects
to the same hash table slot, any in-flight cross-shard
transfers addressed to A's fd must not be delivered to
B. The generation counter catches this: the Xfer struct
carries the generation at enqueue time. At dequeue,
if the slot's generation doesn't match, the transfer
is silently dropped.

**Without this:** Use-after-close bugs. A cross-shard
frame enqueued for peer A (fd=17, slot=42) arrives
after A disconnects and B connects to slot 42 with
fd=23. Without generation check, the frame would be
sent to B — a security violation (B receives A's data).

**Implementation:** `__atomic_load_n(&slot.gen,
__ATOMIC_ACQUIRE)` at dequeue. The generation is
bumped with `__ATOMIC_RELEASE` at disconnect. Acquire/
release guarantees the dequeue sees the disconnect.

### D12: Busy-spin before blocking

**Decision:** Spin 256 times checking for CQEs before
calling io_uring_wait_cqe_timeout.

**Why:** Under load, CQEs arrive continuously. Blocking
(which involves a syscall to wait) adds latency for the
next batch. Spinning keeps the thread hot on the CPU and
processes the next CQE with zero transition cost.

**When spinning wastes cycles:** At low load, the thread
spins 256 times for nothing, then blocks. At 3.5 GHz,
256 spins ≈ 1-2μs — negligible.

**When spinning is critical:** At high load (10G+), the
thread never reaches the block path. Every spin finds
CQEs ready. This is why HD's context switch count is
3,041 vs TS's 963K — the workers rarely enter the kernel.

**The 256 threshold:** Empirical. Too low: unnecessary
blocks under moderate load. Too high: wasted CPU at
idle. 256 is ~1μs of spinning, which is less than the
cost of one context switch (~5-10μs).

### D13: Batched eventfd signaling

**Decision:** Signal each destination worker at most once
per CQE batch, not once per cross-shard frame.

**Why:** eventfd write() is a syscall. If worker 0
forwards 100 frames to worker 1 in one CQE batch, 100
eventfd writes = 100 syscalls. Batching: accumulate a
bitmask of destination workers during the batch, write
each eventfd once at the end. 100 frames, 1 syscall.

**Implementation:** `pending_xfer_signal` bitmask in
Worker struct. Set bit j when enqueueing to worker j's
SPSC ring. After CQE batch, `__builtin_ctz()` to find
set bits, write eventfd for each.

### D14: Backpressure via recv pause

**Decision:** When a peer's send queue exceeds a
threshold, pause recv for that peer.

**Why:** If HD can recv faster than it can send (which
happens when the destination client's TCP window closes),
recv'd frames accumulate in memory. Without backpressure,
memory grows unbounded until OOM. Pausing recv causes
the source client's TCP window to close, which slows
the source — backpressure propagates end-to-end.

**Thresholds:** Adaptive based on peer count.
SendPressureHigh = min(32KB, peer_count × 512B). Resume
at 1/4 of high threshold. The gap between high and low
provides hysteresis — prevents rapid toggle.

**What profiling revealed:** The variance (CV 9.6%) was
blamed on backpressure oscillation, but it was actually
kTLS latency spikes triggering the backpressure
mechanism. Plain TCP showed 0.1% CV. The backpressure
logic is correct; kTLS is the source of instability.

### D15: DEFER_TASKRUN over SQPOLL on VMs

**Decision:** Default to DEFER_TASKRUN, SQPOLL optional.

**Why:** SQPOLL dedicates a kernel thread per io_uring
instance to poll the submission queue. On a 4 vCPU VM,
2 workers + 2 SQPOLL threads = all cores consumed by
io_uring, none left for accept/control/kernel.

DEFER_TASKRUN: completions are only processed when the
worker thread explicitly asks (io_uring_submit or
io_uring_wait_cqe). No dedicated kernel thread. The
worker thread does everything.

**When SQPOLL wins:** Bare metal with dedicated cores.
If you can pin SQPOLL threads to cores that aren't
doing anything else, submission latency drops to zero
(kernel thread is already polling).

**Graceful fallback:** HD tries DEFER_TASKRUN +
SINGLE_ISSUER first. If the kernel doesn't support
them, falls back to COOP_TASKRUN. If that fails, basic
io_uring. Each fallback is less efficient but still
functional.

### D16: Peer struct hot/cold split

**Decision:** Peer struct keeps hot fields (fd, rbuf_len,
occupied) in the first ~96 bytes. The reassembly buffer
(1540 bytes) is heap-allocated separately.

**Why:** The hot path touches fd and rbuf_len on every
recv CQE. If the reassembly buffer were inline in the
struct, Peer would be ~1636 bytes. A hash table of 4096
peers would be 6.5 MB — guaranteed L3 misses on every
lookup. At 96 bytes per slot, the table is 384 KB,
likely L2-resident.

**Profiling validation:** L1 miss rate for HD is 6.1%
(mostly kTLS). The peer struct fits comfortably.

### D17: Cache-line alignment for SPSC ring head/tail

**Decision:** `alignas(64)` on head and tail of
XferSpscRing.

**Why:** False sharing. Head is written by the producer,
tail by the consumer. If they share a cache line, every
write by either side invalidates the other's cache line.
On a modern CPU, a cache line invalidation costs ~40-70ns
(cross-core snoop). At 340K frames/s cross-shard, that's
13-24ms/s of stall per worker pair.

**With alignment:** Head and tail are on separate 64-byte
cache lines. Producer writes head, consumer writes tail,
neither invalidates the other. Zero false sharing.

### D18: GCC atomics over std::atomic in hot path

**Decision:** Use `__atomic_load_n()` / `__atomic_store_n()`
instead of `std::atomic<T>` in data_plane.cc.

**Why:** In debug builds (`-O0`), std::atomic operations
go through a non-inlined function call. GCC builtins are
always intrinsics — same codegen in debug and release.
Since the data plane is developed and debugged frequently,
keeping debug builds fast matters.

**In release builds:** Identical codegen. This is a
development ergonomics choice, not a performance choice.

---

## Part 3: Reading Order for External Docs

**Week 1 priority (read alongside code study):**

1. io_uring(7) man page — understand the basic model
2. Preshing's acquire/release article — required for
   SPSC ring reasoning
3. kTLS kernel docs — understand what happens after
   SSL_free() in ktls.cc
4. libsodium crypto_box docs — understand the handshake

**Week 2 (deepen understanding):**

5. Lord of the io_uring (full tutorial)
6. Provided buffer ring LWN article
7. DERP protocol source (Go reference implementation)
8. OpenSSL TLS setup docs

**Reference (look up as needed):**

9. man pages for epoll, eventfd, madvise, setsockopt
10. cppreference for std::expected, std::print
11. TCP_NODELAY / MSG_MORE / MSG_NOSIGNAL man pages
