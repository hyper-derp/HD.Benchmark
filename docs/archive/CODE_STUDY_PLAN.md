# Hyper-DERP Code Study Plan

## Goal

Understand every line of the hot path and every design
decision well enough to:
- Explain any function's purpose and correctness to an
  interviewer
- Extract client.cc into an SDK with confidence
- Know where to look when something breaks at 50GbE
- Evaluate optimization proposals against actual code

## Codebase Map

| File | Lines | Complexity | Role |
|------|------:|:----------:|------|
| data_plane.cc | 2,234 | very high | io_uring hot path, forwarding, allocators |
| server.cc | 598 | medium | accept, HTTP upgrade, handshake |
| client.cc | 461 | low-medium | DERP client (future SDK) |
| control_plane.cc | 456 | medium | peer registry, watchers, control frames |
| metrics.cc | 307 | low | Prometheus endpoint |
| handshake.cc | 276 | low | NaCl key exchange |
| main.cc | 252 | low | CLI, entry point |
| http.cc | 247 | low | HTTP parser |
| ktls.cc | 224 | low | OpenSSL + kTLS setup |
| tun.cc | 178 | low | TUN device (optional) |
| bench.cc | 156 | low | latency recording, JSON output |
| protocol.cc | 99 | low | frame builders |
| **types.h** | **358** | **high** | **all data structures** |
| protocol.h | 209 | medium | wire format, frame types |
| client.h | 174 | low | client state |
| handshake.h | 157 | low | handshake types |
| server.h | 134 | low | server config |
| control_plane.h | 132 | low | control plane API |
| http.h | 94 | low | HTTP types |
| bench.h | 92 | low | latency recorder |
| ktls.h | 87 | low | kTLS API |
| data_plane.h | 69 | low | data plane API |
| tun.h | 55 | low | TUN API |
| metrics.h | 41 | low | metrics API |
| error.h | 34 | low | Error template |

~5,500 lines source, ~1,900 lines headers, ~2,500 lines
tests. Total ~10,000 lines. Readable in a week at depth.

Build: CMakeLists.txt, C++23, clang preferred, mold linker.
Debug builds use `-fno-omit-frame-pointer` (good for perf).

## Study Order

### Phase 1: Data Structures and Wire Format (Day 1 morning)

**Read first. Everything else depends on these.**

#### 1a: types.h — include/hyper_derp/types.h (358 lines)

This is the skeleton of the entire system. Every struct,
every constant, every design decision is visible here.

Read in this order:
1. **Tuning constants** (top of file) — kHtCapacity,
   kXferSpscSize, kFramePoolCount, kMaxWorkers,
   kMaxCqeBatch, SendPressureHigh/Low
2. **Peer struct** — hot/warm/cold field layout, why
   reassembly buffer is heap-allocated
3. **Route struct** — replicated routing table, atomic
   occupied field
4. **XferSpscRing** — the lock-free cross-shard ring,
   cache-line separated head/tail
5. **Worker struct** — io_uring state, hash table, fd_map,
   command/xfer inboxes, frame pool, slab allocator,
   send pressure state, stats
6. **Cmd and Xfer structs** — control plane → data plane
   commands, cross-shard transfer items
7. **Ctx** — top-level data plane context

Questions to answer after reading:
- Why is Peer split into hot (96B) and cold fields?
- What is the generation counter for? (hint: use-after-close)
- Why does XferSpscRing pad head and tail to separate
  cache lines?
- How many SPSC rings exist in an N-worker system? (N×N)
- What happens when a SPSC ring is full?
- Why are routes replicated per-worker instead of shared?
- What does SendPressureHigh(peer_count) compute and why
  is it adaptive?

#### 1b: protocol.h + protocol.cc (308 lines total)

The wire format. DERP frames are the language.

Read:
1. **Frame type enum** — SendPacket, RecvPacket, KeepAlive,
   PeerGone, PeerPresent, Ping, Pong, Health, etc.
2. **Header format** — [1B type][4B length BE][payload]
3. **Key encoding** — 32-byte NaCl public keys
4. **Frame builders in protocol.cc** — BuildRecvPacket,
   BuildPeerGone, BuildPong, etc.

Questions:
- What's the max DERP frame size?
- What does RecvPacket contain that SendPacket doesn't?
  (the source key — receiver needs to know who sent it)
- Why are frame builders separate from the data plane?
  (stateless, testable, reusable in client)

### Phase 2: Connection Lifecycle (Day 1 afternoon)

**Follow a connection from accept() to data plane handoff.**

Read in execution order:

#### 2a: main.cc (252 lines)

- CLI argument parsing (worker count, ports, flags)
- ServerInit → ServerRun entry point
- Skim — not much depth here

#### 2b: server.cc (598 lines)

The accept loop. Read linearly:
1. **ServerInit** — allocate workers, init data plane
2. **ServerRun** — main accept loop, signal handling
3. **Per-connection flow**: accept → read HTTP → upgrade →
   TLS handshake → DERP handshake → DpAddPeer()
4. **Error handling** — what happens when handshake fails
5. **Rate limiting** — how bad clients are throttled

Questions:
- What thread does accept run on? (main thread, blocking)
- How does the server decide which worker gets a new peer?
  (FNV-1a hash of the peer's public key)
- What happens if DpAddPeer fails? (fd closed, client
  disconnected)
- Is there a limit on concurrent handshakes?

#### 2c: http.cc (247 lines)

- ParseHttpRequest — minimal HTTP/1.1 parser
- Case-insensitive header matching
- Upgrade response builder
- Probe endpoint (health check)

Questions:
- What headers does the parser actually check?
- How does it distinguish upgrade from probe from error?

#### 2d: ktls.cc (224 lines)

- OpenSSL context setup
- TLS handshake (blocking)
- kTLS auto-installation (setsockopt SOL_TLS)
- Verification: BIO_get_ktls_send / BIO_get_ktls_recv

Questions:
- What happens if kTLS installation fails? (fallback to
  userspace TLS, or error?)
- After kTLS is installed, does the fd still go through
  OpenSSL for read/write? (no — kernel handles it, the
  fd is a normal socket again)
- Why does kTLS setup happen in the accept thread, not
  the worker thread?

#### 2e: handshake.cc (276 lines)

- NaCl box key exchange
- ServerKey → ClientInfo → ServerInfo frame sequence
- Peer identity verification

Questions:
- What does the handshake prove? (client knows its own
  private key, server knows the client's public key)
- Is there mutual authentication? (server sends ServerKey,
  client sends ClientInfo encrypted to server's key)
- What prevents replay attacks?
- Could this handshake be reused for direct connections
  in the SDK?

### Phase 3: Data Plane Deep Dive (Day 2 + Day 3)

**The heart of the system. 2,234 lines. Take two days.**

#### 3a: Read top-down first pass (Day 2 morning)

Read data_plane.cc from top to bottom, skimming. Build
a mental map of the sections:

1. **Hash table operations** (~lines 87-135) — open
   addressing with linear probing, FNV-1a hash
2. **Route table operations** — replicated per-worker,
   atomic updates
3. **Slab allocator** (~lines 388-450) — SendItem pool,
   bump allocation with free list
4. **Frame pool** (~lines 450-592) — pre-allocated 2KB
   buffers, THP hints, SPSC return inboxes
5. **Recv handling** — multishot recv, provided buffer
   ring, frame reassembly, forwarding dispatch
6. **Send handling** — per-peer send queue, MSG_MORE
   coalescing, SEND_ZC for large frames
7. **Cross-shard forwarding** — SPSC enqueue, batched
   eventfd, ProcessXfer drain loop
8. **Backpressure** — SendPressureHigh/Low thresholds,
   recv pause/resume
9. **Worker main loop** (WorkerRun, ~line 1681) — CQE
   batch processing, busy-spin, timeout

Don't try to understand every detail. Map the territory.

#### 3b: Hash table + routing (Day 2 morning)

Go back and read carefully:
- HtInsert, HtLookup, HtRemove
- RouteInsert, RouteLookup, RouteRemove
- How generation counters prevent stale lookups

Questions:
- What's the load factor limit? What happens at capacity?
- How does tombstone handling work (if at all)?
- Why FNV-1a and not something faster? (32-byte keys,
  FNV is simple and sufficient — profiling proved hash
  is <0.5% of cycles)
- Why replicate routes instead of sharing with a lock?
  (eliminates all cross-thread synchronization in the
  forwarding path)

#### 3c: Allocators (Day 2 afternoon)

- Slab allocator: how SendItems are allocated and freed
- Frame pool: how 2KB buffers are managed
- SPSC return inboxes: how cross-shard buffers get
  returned to their owning worker
- FramePoolOwner: how a buffer's owner is determined

Questions:
- What happens when the frame pool is exhausted?
- Why THP (transparent huge pages) hints?
- How does the SPSC return inbox avoid the ABA problem?
- What was the Treiber stack that this replaced, and why
  was it replaced? (CAS retry loop under contention)

#### 3d: Recv path (Day 2 afternoon / Day 3 morning)

The most complex section. Read HandleRecvCqe carefully:
1. Multishot recv CQE arrives with buffer from provided
   buffer ring
2. Frame reassembly: partial frames accumulated in
   per-peer rbuf
3. Complete frame: parse type, extract destination key
4. Local delivery: HtLookup destination on same worker
5. Cross-shard: XferSpscRing enqueue + eventfd signal
6. What happens on recv error, ENOBUFS, connection close

Questions:
- How does multishot recv work? (one SQE, multiple CQEs,
  kernel provides buffers from the ring)
- What if a frame spans multiple recv completions?
  (reassembly buffer accumulates until complete)
- What triggers ENOBUFS? (provided buffer ring empty)
- How is ENOBUFS recovered? (re-arm the multishot recv)
- When is recv paused for backpressure? (send queue
  exceeds threshold)

#### 3e: Send path (Day 3 morning)

Read HandleSendCqe and FlushPendingSends:
1. Per-peer send queue (linked list of SendItems)
2. MSG_MORE coalescing: set on all but last item in batch
3. SEND_ZC for frames >4KB
4. ZC notification tracking (notif_map)
5. What happens when send fails (EAGAIN, EPIPE, etc.)

Questions:
- Why MSG_MORE instead of TCP_CORK?
- When is SEND_ZC used vs regular send? Why the 4KB
  threshold?
- How are ZC notifications tracked and drained?
- What does the send queue look like under pressure?

#### 3f: Cross-shard forwarding (Day 3 morning)

Read the full cross-shard path:
1. ForwardMsg → XferSpscRing enqueue
2. Batched eventfd signaling (one per dest per CQE batch)
3. ProcessXfer → drain all inboxes round-robin
4. Frame return via SPSC return inbox

Questions:
- What's the total latency added by cross-shard forwarding?
- What happens if the SPSC ring is full? (frame dropped,
  counted as xfer_drop)
- Why one eventfd per worker pair instead of one per
  worker?
- How does batched signaling reduce overhead? (one write()
  per destination per CQE batch, not per frame)

#### 3g: Backpressure + main loop (Day 3 afternoon)

Read the backpressure mechanism and WorkerRun:
1. SendPressureHigh/Low: when recv is paused/resumed
2. Deferred recv queue: recv_defer_buf
3. WorkerRun: CQE batch loop, busy-spin count, timeout
4. How all the pieces fit together in one iteration

Questions:
- What's the busy-spin threshold (256) and why?
- What does an iteration of the main loop look like
  at 3 Gbps? At 10 Gbps? How many CQEs per batch?
- How does backpressure propagate: send queue fills →
  recv paused → client TCP window closes → client
  slows down?
- The profiling showed this is all 2% of cycles. Where
  does the time actually go between iterations? (kernel
  — io_uring completion processing, TCP stack, kTLS)

### Phase 4: Control Plane (Day 4 morning)

#### control_plane.cc (456 lines)

Read linearly:
1. **CpInit** — hash table, epoll setup, pipe creation
2. **CpOnPeerConnect / CpOnPeerDisconnect** — registry ops
3. **CpProcessFrame** — dispatch non-transport frames
   (Ping/Pong, watchers, peer presence)
4. **CpRun** — epoll loop, reads from worker pipes

Questions:
- How does the control plane learn about new peers?
  (server.cc calls CpOnPeerConnect after handshake)
- What's the pipe format between workers and control
  plane? ([4B fd][1B type][4B len][payload])
- What is a watcher and how do watchers get notified?
- Could the blocking pipe write (WriteAllBlocking) in
  ForwardMsg block a worker? (yes — profiling candidate
  for 50GbE. Low priority: not visible in flame graph
  at current rates)

### Phase 5: Client Code — SDK Foundation (Day 4 afternoon)

#### client.cc + client.h (635 lines total)

This is the code you'll extract into a library.

Read:
1. **ClientConnect** — TCP connect, optional TLS, HTTP
   upgrade, DERP handshake
2. **ClientSend** — frame SendPacket to destination key
3. **ClientRecv** — read frame, parse RecvPacket
4. **Connection state** — what the client tracks
5. **Error handling** — reconnection? timeout?

Questions:
- What's the minimum API surface for an SDK?
  (connect, send, recv, close, peer callbacks)
- What state does the client hold? Can it be made
  thread-safe? (or is single-threaded + event loop
  better?)
- How would you add direct connection negotiation?
  (exchange candidates as DERP messages, attempt direct,
  promote transparent to application)
- What's missing for production use? (reconnection,
  keepalive, backpressure from application)

### Phase 6: Supporting Code + Tests (Day 5)

#### ktls.cc, tun.cc, metrics.cc, bench.cc

Skim these. Understand their APIs, don't memorize
internals. They're support code, not the core.

#### tests/ (~2,500 lines)

Read test_e2e.cc closely — it exercises the full stack
and shows expected behavior. Skim unit tests for
individual modules. The tests show you what the author
(Claude + you) considered important to verify.

Questions:
- What edge cases are tested?
- What's NOT tested that should be?
- How would you test the SDK's direct connection path?

## Key Cross-Cutting Questions

These span multiple files. Answer them after Phase 3:

1. **Trace a packet end-to-end**: client A sends 1400B to
   client B on a different worker. What happens at every
   step from send() to recv()? How many memcpys? How many
   syscalls? What's the expected latency?

2. **What happens when a peer disconnects mid-send?**
   Follow the generation counter through HtLookup,
   HandleSendCqe, cross-shard forwarding. How are stale
   references detected and cleaned up?

3. **What happens at exactly the backpressure threshold?**
   Trace the state transitions: send_pressure exceeds
   high → recv_paused = true → what happens to in-flight
   recvs? → when does recv resume? → is there hysteresis?

4. **What's the worst-case latency path?** A frame arrives
   on worker 0, destination is on worker 3, worker 3's
   send queue is near the backpressure threshold. What
   happens?

5. **Where could a 50GbE bottleneck appear that doesn't
   show at 10GbE?** The profiling showed 2% user code.
   At 5x the packet rate, does that 2% become 10%? What
   scales linearly and what scales worse than linearly?

## Time Estimate

| Phase | Focus | Time |
|-------|-------|------|
| 1 | Data structures + wire format | 3-4 hours |
| 2 | Connection lifecycle | 3-4 hours |
| 3 | Data plane deep dive | 10-12 hours |
| 4 | Control plane | 2-3 hours |
| 5 | Client / SDK foundation | 2-3 hours |
| 6 | Support code + tests | 2-3 hours |
| **Total** | | **~25 hours** |

Spread over 5 days, ~5 hours/day. Phase 3 is the mountain.

## How to Use This Instance During Study

Bring questions here when:
- Something doesn't make sense and you've stared at it
  for 10 minutes
- You think you found a bug or race condition
- You want to verify your understanding of a mechanism
- You want to discuss how a section affects the SDK design
- You hit one of the cross-cutting questions and want to
  work through it together
