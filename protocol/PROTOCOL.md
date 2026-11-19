# PhoneBridge Protocol v1

No accounts and no hosted cloud service, ever (an optional self-hosted relay, section 1g, is the only intermediary). Trust is established directly between one
Linux PC and one Android phone, and reconnects re-use that trust.

## 1. Pairing (one-time per device pair)

> The numbered steps below describe the original v1 flow and are kept for history. Current phones and the PC speak
> pairing v4 (SPAKE2, section 1j); the PIN is no longer used as an HKDF salt or sent as a token.

Goal: get both devices to agree on a shared secret and the PC's LAN address,
using Bluetooth as the out-of-band channel, without typing anything except a
PIN as a fallback to scanning a QR code.

1. **PC generates**, on first launch or when the user clicks "Pair a device":
   - An X25519 keypair (`pc_priv`, `pc_pub`), kept for the lifetime of the pairing.
   - A random 6-digit `pairing_token` (single use, expires in 5 minutes).
   - Its Bluetooth adapter address `bt_addr`.
   - Encodes `{v:1, bt_addr, token, pub: base64(pc_pub)}` as JSON and renders it
     as a QR code. The same 6-digit token is shown as text under the QR for
     manual entry if the phone can't scan (e.g. no camera access granted yet).

2. **Phone scans the QR** (or the user picks the PC from a Bluetooth device
   list and types the 6-digit token — see note below):
   - Opens an RFCOMM connection to `bt_addr` on the well-known PhoneBridge
     service UUID (`b43c1e00-35ee-4b8a-9e3f-6c3f2a1b9d10`).
   - **PC sends first**, immediately on accept: `{v:1, token, pub: base64(pc_pub), name: pc_name}`
     (the "offer" — same fields as the QR, sent again over the now-open BT link).
     A QR-scanning phone cross-checks this against what it scanned (guards
     against a different device answering at that address); this is also what
     makes a future PIN-only flow (pick PC from a BT device list, type the
     code, no camera) possible without protocol changes — that phone has no
     QR-derived pubkey, only this offer.
   - Phone replies `{v:1, token, pub: base64(phone_pub), name: phone_name}`.
   - PC verifies the token matches and hasn't expired; if not, aborts the pairing
     (this is the only check standing in for "login" — a short-lived, local,
     single-use token, never a password or account).

3. **Both sides derive a shared secret** via X25519 ECDH:
   `shared = X25519(my_priv, their_pub)`, then
   `session_key = HKDF-SHA256(shared, salt=token, info="phonebridge-v1")`.
   Both sides now compute `fingerprint = SHA256(pc_pub || phone_pub)[:16]`,
   displayed as a short hex string on both screens for the user to visually
   confirm matches (mirrors Phone Link / Bluetooth's "does this code match"
   step) — optional but recommended, skippable.

4. **PC sends its LAN info** over the still-open BT RFCOMM link, encrypted with
   `session_key` (AES-256-GCM): `{lan_ip, lan_port, cert_fingerprint}`.

5. **Both sides persist** a pairing record locally:
   - PC: `~/.config/phonebridge/devices/<phone_fingerprint>.json`
     `{name, phone_pub, session_key (wrapped), last_lan_ip}`
   - Phone: app-private storage, same shape, swapped.
   - No server, no account, nothing leaves the two devices.

## 1b. The video stream connection (screen mirroring, phase 6; webcam, phase 7)

The control connection (below) carries low-frequency JSON messages — notifications,
SMS, battery, `mirror.start`/`mirror.stop`/`mirror.input`. It cannot *also* carry the
continuous, unbounded stream of raw H.264 frames that screen mirroring or the webcam
feature produce: those have no JSON wrapper (see section 3's raw-frame framing) and no
predetermined end, so the receiver can't tell "is the next frame on the wire a JSON
control message, or a raw video frame?" if both share one socket. (A bounded file
transfer avoids this ambiguity because the single `file.offer` handler owns the socket
exclusively until the known byte count is read — screen mirroring has no such bound.)

So video gets its own, second connection, opened on demand only while mirroring/webcam
is active:

1. PC sends `mirror.start` (or `webcam.start`) over the normal control connection.
2. Phone opens a **second** TLS connection to the same `lan_ip:lan_port`, and instead of
   sending just its pairing fingerprint as the first line (see section 2), sends
   `"<fingerprint>:video\n"`. The PC recognizes the `:video` suffix and routes this
   connection's raw frames to whichever page is currently displaying mirrored video,
   instead of treating it as a second control connection for the same device.
3. Phone streams one `send_raw()` call per encoded access unit on this connection —
   each raw frame is already self-delimited by the normal 4-byte length prefix, so no
   further framing is needed.
4. PC sends `mirror.stop` over the *control* connection; phone stops capturing and
   closes the video connection.

Both `mirror.start`→video-connection and `webcam.start`→video-connection use the exact
same mechanism — only the Android-side capture source differs (MediaProjection vs.
Camera2).

## 1f. UDP video transport

Screen mirroring and the webcam prefer UDP over the TLS connection of section 1b: a lost packet costs one
frame instead of stalling every frame behind it. The PC listens on UDP on the **same port number** as the
control channel. Datagrams are at most 1200 bytes:

    fp(8) | dir(1) | epoch(3) | counter(8) | AES-256-GCM( type(1) | body ) + tag(16)

- `fp` is the pairing fingerprint as raw bytes (selects the session key). `dir` is 0 phone->PC, 1 PC->phone.
  The GCM nonce is `dir|epoch|counter` (12 bytes); `epoch` is random per stream, `counter` increments per
  datagram; AAD is `fp`. Datagrams that do not authenticate are dropped silently.
- Types: `0 VIDEO`, `1 PROBE`, `2 PROBE_ACK`, `3 KEYREQ`, `4 REPORT`.
- **Probe:** before streaming the phone sends `PROBE(8 random bytes)` up to 4x (300 ms each); the PC echoes it
  as `PROBE_ACK`. No authentic ack -> the phone uses the TCP video connection instead.
- **VIDEO body:** `frame_id u32 | frag_idx u16 | frag_count u16 | flags u8 (1 = keyframe) | data`. One access
  unit (SPS/PPS prepended on keyframes) is split into fragments; `frame_id` increments per access unit.
- **Loss handling (PC):** frames are delivered in order. If a frame is incomplete when a later one finishes, it
  is dropped, no delta frames are delivered until the next keyframe (the last picture stays on screen instead
  of showing a corrupt one) and the PC sends `KEYREQ` (at most every 500 ms).
- **REPORT** (PC->phone, 1 Hz): `delivered u32 | lost u32` frames since the last report. The phone cuts its
  bitrate x0.7 above 5 % loss and raises it x1.15 after five clean reports.

Not WebRTC: there is no ICE/DTLS/SRTP, because both ends are already paired with a shared key and the PC is
reachable at a known address. Packets are paced in bursts of 8; there is no retransmission or FEC.

## 1g. Optional relay (away from the PC's network)

Off by default; PhoneBridge never needs it on one network. If you run `python3 -m phonebridge.relay_server`
(default port 28200) on a machine both devices can reach, and enter its address in the PC app (Device tab → "Use
away from home", or `~/.config/phonebridge/relay.json` = `{"host", "port"}`, or `$PHONEBRIDGE_RELAY=host[:port]`),
a phone that cannot reach the PC directly connects through it. No accounts, nothing stored; the relay forwards bytes.

- **Room:** `HMAC-SHA256(session_key, "phonebridge-relay-v1")` as hex, first 32 characters. Only the two paired
  devices can compute it.
- **PC:** keeps 3 idle outbound connections ("slots") per paired phone, each starting `PBRELAY pc <room>\n`.
- **Phone:** sends `PBRELAY phone <room>\n`. The relay takes a live slot, sends `GO\n` to the PC, the PC answers
  `READY\n` and starts a TLS *server* handshake on that socket; only then does the relay answer the phone `OK\n` and
  copy bytes both ways. No waiting slot: `NOPC\n`. The phone then runs the normal pinned-TLS client handshake through
  the tunnel and everything in sections 2-3 (control, file data and TCP video connections) is unchanged. A
  replacement slot is parked as soon as one is taken, since a phone opens several connections.
- **Config push:** while connected, the PC sends `relay.config {host, port}` (empty host = off) so the phone knows
  the relay without typing it. The phone tries direct -> mDNS -> UDP discovery -> relay.
- **Not relayed:** UDP video (the phone falls back to TCP video), mDNS/BLE wake. The PC does not record the
  relay's address as the phone's LAN address.
- **What the relay learns:** that a room was used, when, from which IPs, and how many bytes. It can drop or delay
  traffic. It cannot read or alter it: TLS is pinned to the PC's certificate and every frame is AES-GCM under the
  pairing key. Limits: 8 slots per room, 10,000 rooms. Rooms are unguessable (128 bits) but not rate-limited per IP —
  firewall it or run it privately. There is no TLS on the relay's own port and no authentication of the room name
  beyond secrecy; put it on a host you control.

## 1c. File transfer v2 (one data connection per file)

Files no longer travel on the control connection (that blocked every other message for the
length of a transfer). The sender announces the file on the control channel; the phone then
opens a **separate TLS connection per file** (the PC is always the server):

1. Sender -> receiver, control: `file.offer {transfer_id, filename, size, mime, resume_key, data_conn: true, ...}`.
   `resume_key` is stable for the same file (name+size+mtime) so an interrupted transfer can continue.
2. Receiver answers on control: `file.accept {transfer_id, offset}` (phone->PC uploads) or just
   opens the data connection (PC->phone), or `file.reject {transfer_id, reason}` (e.g. not enough
   free space). `offset` = bytes it already holds from a previous attempt (`.pb-<resume_key>.part`).
3. Phone opens a TLS connection and sends `"<fingerprint>:file:<transfer_id>:<offset>\n"`, then one
   frame `PBFILE:<transfer_id>` — if that frame doesn't decrypt with the session key the PC drops
   the connection before streaming anything.
4. The sender streams AES-GCM frames of up to 256 KiB from `offset`, then one trailer frame
   `SHA256:<hex of the whole file>`; the receiver replies with one frame `OK` or `BAD`
   (on `BAD` the partial file is discarded). Frames are capped at 8 MiB everywhere.
5. The receiver saves `file.saved {transfer_id, filename, path}` (PC->phone).

Several transfers can run at once and the control channel stays free while they do.

## 1d. Discovery

- **mDNS / DNS-SD:** the PC advertises `_phonebridge._tcp` (Avahi) with TXT `cert=<first 16 hex of the TLS
  cert fingerprint>`; the phone (NsdManager) tries it before UDP broadcast.
- **UDP broadcast (28101):** the phone sends `{"phonebridge":"discover","tag":HMAC(session_key,
  "phonebridge-discover-v2|<unix//300>")[:32 hex]}`. The pairing fingerprint is no longer broadcast; the PC
  answers only if the tag verifies for a paired phone (current, previous or next 5-minute bucket).
- **BLE wake beacon:** the PC advertises the PhoneBridge service UUID over BLE (BlueZ); a disconnected phone
  scans for it (low power) and reconnects immediately. It carries no identity or data.

## 1e. Authentication of the control connection

The `<fingerprint>\n` line is not a secret. The PC therefore waits (15 s) for **one frame that decrypts with
the session key** before it replaces an existing connection, remembers the phone's address or tells the UI.
Phone-controlling-PC features (`pc.*`, `pcfiles.*`: shell, files, remote input, screen view, clipboard read)
are refused with `pc.status {denied:true}` unless the user enabled **Allow PC control** for that phone on the
PC (off by default). The phone pings every 30 s; the idle timeout is 100 s (PC) / 90 s (phone).

## 1h. Protocol v4: device identities, forward secrecy, key rotation

**Identities.** Every device has a long-term Ed25519 key pair. The public keys are exchanged at the very end of
pairing (pairing protocol v3): the PC's `id_pub` rides in the `lan_info` frame and the phone answers with
`{"id_pub": ...}`, both sealed under the freshly derived pairing key (so a man in the middle who doesn't know the PIN
cannot substitute his own). Private keys never leave their device: PC = desktop keyring; phone = wrapped by an
Android-Keystore AES key.

**Connection handshake** (replaces "first frame is any message sealed with the pairing key"). After TLS and the ident
line `<fp>\n`, with `K_hs = HKDF(pairing_key, info="phonebridge-v4|handshake")`:

1. phone -> PC: `AES-GCM(K_hs, aad="phonebridge-v4|hs|phone>pc")` of
   `{"v":4,"eph":b64(X25519 pub),"n":b64(16 random),"sig":b64(Ed25519_phone("phonebridge-v4|client|fp|tls_cert_sha256|eph|n"))}`
2. PC -> phone (aad `...|pc>phone`): `{"v":4,"eph","n","sig":Ed25519_pc("phonebridge-v4|server|fp|tls_cert_sha256|c_eph|c_n|eph|n")}`
3. `master = HKDF(X25519(eph_c, eph_s), salt=pairing_key, info="phonebridge-v4|master|" || SHA256(server_signed_msg))`;
   `c2s`/`s2c` = HKDF(master, info "phonebridge-v4|c2s" / "...|s2c"); `exporter` = HKDF(master, info "phonebridge-v4|exporter").

Ephemeral keys are discarded, so later compromise of the pairing key and both identity keys does not reveal recorded
sessions (forward secrecy). The pairing key still gates the handshake; the signatures mean the pairing key alone can't
impersonate either device; they cover the PC's TLS certificate hash (channel binding). Frames then use the per-direction
keys with the v3 direction AAD. A pairing without `identity_pub`, a v3-style first frame or a bad signature is refused.

**Derived keys.** File data connections use `HKDF(exporter, "phonebridge-v4|data|file|<transfer_id>")`, the video
connection `...|data|video`, UDP video `HKDF(exporter, "phonebridge-v4|udp")`. A data connection or UDP datagram is only
accepted while the control session that owns the exporter is live.

**Rotation.** The PC starts a rekey after 1,000,000 frames or one hour: `rekey {eph}` (PC->phone), `rekey.ack {eph}`
(phone->PC, last frame under the old phone->PC key; the phone then sends with the new key), `rekey.done` (PC->phone, last
frame under the old PC->phone key; the PC then sends with the new key). New master = `HKDF(X25519(e1,e2), salt=old master,
info="phonebridge-v4|rekey|<generation>")`. The exporter is unchanged for the session.

## 1i. Session lifecycle: key confirmation, termination, identity rotation, cancelling, USB

* **Key confirmation.** After the v4 handshake reply the PC does not count the session (or drop an existing one for the same
  phone) until the phone's first frame decrypts under the new keys. A replayed recorded hello therefore cannot displace the
  real phone. The Android app's first frame is `device.info`.
* **Termination.** `session.end {reason}` is an authenticated close (`closed`, `revoked`, `disconnect_all`, `unpaired`,
  `emergency`, `app_stopped`). A link that ends without one is recorded as *abrupt* (reset or truncation).
* **Identity rotation.** `identity.rotate {new_pub, sig}` where `sig = Ed25519_old("phonebridge-v4|rotate|<fp>|<new_pub>")`
  is accepted in either direction; the receiver replies `identity.rotated {new_pub}`. The PC keeps a rotated phone's previous
  key acceptable until the phone signs in with the new one, and signs a phone's handshake with the PC key that phone last
  acknowledged (so an offline phone is never locked out); a PC rotation notice is sent right after such a handshake.
* **Cancelling transfers.** `file.cancel {transfer_id}` (either direction) stops the stream, closes the data connection and
  deletes the receiver's partial download. Partial downloads are private (PC: 0600; phone: app-private storage, copied into
  the shared folder only after the SHA-256 check) and partials older than 7 days are purged.
* **USB.** `adb reverse tcp:<port> tcp:<port>` makes the phone's `127.0.0.1:<port>` reach the PC's control port over the cable
  only. The phone tries it first; data connections follow the control connection's route; UDP video is not used over USB.
* **Permission denials.** `permission.denied {feature, type}` is the reply to a request the other side's permission manager
  refused.

## 1j. Pairing v4: SPAKE2 PIN verification and the USB route

Pairing protocol v4 replaces the HMAC "proof of the PIN" (which a rogue device on the LAN could test PIN guesses against
offline) with **SPAKE2** (RFC 9382 construction; ciphersuite P-256 / SHA-256 / HKDF / HMAC; `M`/`N` from the RFC). The PIN
(also carried in the QR code) is the password: `w = scrypt(PIN, "phonebridge-spake2-v1", N=2^14, r=8, p=1, 48 bytes) mod (n-1) + 1`.
Roles: **A = phone**, **B = PC**. All messages are single JSON lines (same transports as before: Wi-Fi TCP, Bluetooth RFCOMM,
and USB, below).

1. PC -> phone: `{"v":4,"pub":b64(X25519),"name":..,"nonce":b64,"pake":"spake2-p256"}`.
2. phone -> PC: `{"v":4,"pub":b64(X25519),"name":..,"platform":..,"spake":b64(X)}`, `X = x*G + w*M` (65-byte SEC1).
3. PC -> phone: `{"spake":b64(Y)}`, `Y = y*G + w*N`. Both sides compute the transcript
   `TT = len8(Context) || len8("phonebridge-phone") || len8("phonebridge-pc") || len8(X) || len8(Y) || len8(Z) || len8(V) || len8(w)`
   (`len8` = 8-byte little-endian length; `Context = "phonebridge-pair-v4|<pc_pub b64>|<phone_pub b64>|<nonce>"`, so both X25519
   keys, and the QR-pinned PC key, are bound to the PIN exchange). `Ke || Ka = SHA-256(TT)` (16 + 16 bytes),
   `KcA || KcB = HKDF(Ka, "ConfirmationKeys", 32)`.
4. phone -> PC: `{"confirm": hex(HMAC(KcA, Y))}`. The PC checks it; a wrong value is a **wrong PIN**: it is counted toward the 3
   allowed failures per code, and the PC answers `{"ok":false,"error":..}`. A correct one consumes the code (single use).
5. PC -> phone: `{"confirm": hex(HMAC(KcB, X))}`. The phone checks it before it believes anything else the PC says.
6. The user compares the 6-digit confirmation code on both screens and allows the phone on the PC; PC -> phone `{"ok":true,"name":..}`.
7. `pairing_key = HKDF(X25519(my_priv, their_pub), salt=Ke, info="phonebridge-v4|pairing")`; fingerprint and the sealed
   `lan_info` / `id_pub` frames are unchanged.

The phone proves the PIN first, so a rogue "PC" learns at most whether its single guess was right per exchange the user
starts, and a rogue "phone" gets nothing without a correct confirmation. Recorded exchanges cannot be tested against PIN
guesses offline. A pairing v3 hello (HMAC proof) is refused with "update PhoneBridge on the phone".

**USB route.** While the PC's Pair dialog is open and a phone is attached with USB debugging authorised, the PC runs
`adb reverse tcp:28102 tcp:28102`; the phone tries `127.0.0.1:<pport>` first (no probe: the PC accepts a single connection),
and falls back to the QR's LAN addresses, discovery or Bluetooth if nothing answers. The exchange is identical; the PC
removes the tunnel when the dialog closes. Nothing is exposed on any network.

## 2. Reconnection

- If the phone is on the same LAN as the last known `lan_ip`: connect directly
  over **TLS** to `lan_ip:lan_port`. The TLS cert is self-signed; the client
  pins it against the stored `cert_fingerprint` from pairing (no CA, no
  account — trust-on-first-use, verified every reconnect).
- If no Wi-Fi match (different network, Wi-Fi off): fall back to **Bluetooth
  RFCOMM** using the stored `bt_addr`/paired BT bond. Lower bandwidth — the
  client marks itself "connected via Bluetooth" and disables
  video/photo/file-heavy pages in the UI until Wi-Fi is available.
- Every message on either transport is encrypted with `session_key`
  (AES-256-GCM, random 12-byte nonce prefixed per message) regardless of
  transport, so Bluetooth is not a weaker trust path, only a lower-bandwidth one.

## 3. Framing

All messages (both transports) use the same frame:

```
[4 bytes: big-endian uint32 length N][N bytes: AES-GCM ciphertext]
plaintext = nonce(12) || ciphertext || tag(16)   (handled by AESGCM.encrypt)
```

**Direction binding (v3).** Every frame is sealed with AES-GCM *associated data* naming its direction:
`"phonebridge-v3|pc>phone"` for frames the PC sends and `"phonebridge-v3|phone>pc"` for frames the phone sends.
A receiver opens only the peer's label, so a frame reflected back at its sender fails authentication. This applies
to the control channel, file and video data connections and the Bluetooth pairing blob (not UDP video, which has
its own AAD). Control envelopes also carry `seq`: the PC rejects a `seq` it has already seen or that is more than
256 behind the newest, so a recorded frame can't be replayed within a connection. v2 peers (no AAD) are not
compatible: upgrade the phone and PC together.

Decrypted plaintext is UTF-8 JSON for control messages, or raw bytes for
binary chunks (file/photo/video), distinguished by an envelope:

```json
{"ch": "control|file|video", "type": "...", "seq": 0, "payload": {...}}
```

Binary payloads (file bytes, video frames) are base64 inside `payload.data`
for the control channel's small cases, or sent as separate framed messages
with `ch` set to `file`/`video` and `payload` holding raw bytes directly
(no base64) when size matters — see `protocol/schema.json` `channels` section.

## 4. Message types (control channel)

| type | direction | payload |
|---|---|---|
| `device.info` | both | `{name, os, battery_pct, charging, signal_pct, wifi_ssid}` |
| `notification.posted` | phone→pc | `{id, app, title, text, icon(base64), actions:[{id,label}]}` |
| `notification.removed` | phone→pc | `{id}` |
| `notification.action` | pc→phone | `{id, action_id, reply_text?}` |
| `clipboard.update` | both | `{text}` |
| `sms.list_threads` | pc→phone | `{}` (request) |
| `sms.thread_list` | phone→pc | `{threads:[{id,address,last_text,ts,unread}]}` |
| `sms.get_thread` | pc→phone | `{thread_id}` (request) |
| `sms.thread_messages` | phone→pc | `{thread_id, messages:[{id,from_me,text,ts}]}` |
| `sms.send` | pc→phone | `{number, text}` — `number`, not a thread id: SmsManager sends to an address, so the PC always resolves the thread's `address` first |
| `sms.new` | phone→pc | `{thread_id, address, message:{id,from_me,text,ts}}` |
| `call.ringing` | phone→pc | `{caller_name, number}` |
| `call.state` | phone→pc | `{state: ringing|active|ended}` |
| `call.action` | pc→phone | `{action: answer|decline|mute|unmute}` — `decline` is best-effort: Android locks down programmatic call-rejection to the default dialer/MODIFY_PHONE_STATE on most versions, so a non-default-dialer app may simply be unable to comply (see android/.../telephony/CallRelay.kt) |
| `file.offer` | both | `{transfer_id, filename, size, mime, photo_id?}` — `photo_id` is set only when this transfer is carrying a photo (see below), so the receiver can route it to the Photos page instead of generic downloads |
| `file.accept` / `file.reject` | both | `{transfer_id}` (unused so far — transfers are currently auto-accepted; reserved for a future "ask before saving" prompt) |
| `file.chunk` | both | binary frame, `ch=file` |
| `photos.list` | pc→phone | `{}` (request) |
| `photos.list_result` | phone→pc | `{photos:[{id,taken_ts,thumb(base64 jpeg)}]}` |
| `photos.get` | pc→phone | `{id}` (request) — answered with a `file.offer`(+chunks) carrying that photo's `photo_id` |
| `photo.new` | phone→pc | `{id, taken_ts}`, sent when the phone notices a brand-new photo, immediately followed by a `file.offer`(+chunks) auto-push carrying the same `photo_id` |
| `mirror.start` / `mirror.stop` | pc→phone | `{}`; `mirror.start` may carry `{app}` — see section 4f |
| `mirror.frame` | phone→pc | binary frame, `ch=video` |
| `mirror.input` | pc→phone | `{kind: tap|swipe|key|text, ...}` — `tap`:`{x,y}`, `swipe`:`{x1,y1,x2,y2,duration_ms}` (x/y are **normalized [0,1]** fractions of screen size, not pixels — the PC can't assume it knows this phone's resolution ahead of a session), `key`:`{key: back\|home\|recents}`, `text`:`{text}` (best-effort: only works if some app currently has an editable field focused and exposed to accessibility — there's no general "type anywhere" API without also being the active IME) |
| `webcam.start` / `webcam.stop` | pc→phone | `{}` |
| `webcam.frame` | phone→pc | binary frame, `ch=video` |
| `apps.list_request` | pc→phone | `{}` (request) |
| `apps.list` | phone→pc | `{apps:[{pkg,label,icon(base64)}]}` |
| `apps.launch` | pc→phone | `{pkg}` — approximates "app mirroring": launches the app on the phone; actually *seeing* it requires separately starting screen mirroring (phase 6) — no API exposes isolated per-app window capture to a normal third-party app |

### 4b. Messages added with the v1.0 Android app (all additive)

| type | direction | payload |
|---|---|---|
| `device.info` (extended) | phone→pc | adds `model, android_version, battery_temp_c, battery_health, carrier, network_type, signal_level (0-4), signal_dbm, sim_state, on_wifi, wifi_rssi, wifi_signal_pct, wifi_link_mbps, storage_total, storage_free, ram_total, ram_free` |
| `device.info_request` | pc→phone | `{}` — phone answers with `device.info` |
| `notifications.sync` | pc→phone | `{}` — phone answers with `notifications.all` |
| `notifications.all` | phone→pc | `{notifications:[<notification.posted payload>…]}` — the full active set, replaces the PC's list |
| `notifications.unavailable` | phone→pc | `{}` — notification access not granted |
| `notification.posted` (extended) | phone→pc | adds `pkg, ts, ongoing, clearable`; `actions[].id` is `action:<index>`, `actions[].is_reply` marks inline-reply actions |
| `notification.action` (extended) | pc→phone | `action_id`: `dismiss`, `dismiss_all`, `open`, `reply` (+`reply_text`), or `action:<index>` |
| `sms.thread_list` (extended) | phone→pc | threads add `name` (contact), `count` |
| `sms.thread_messages` (extended) | phone→pc | adds `address, name`; messages add `status: ok\|sending\|failed` |
| `sms.sent` | phone→pc | `{thread_id, address}` after an `sms.send` |
| `calls.history_request` | pc→phone | `{limit}` |
| `calls.history` | phone→pc | `{calls:[{number, name, type: incoming\|outgoing\|missed\|rejected\|blocked\|voicemail, ts, duration}]}` — also pushed after every call ends |
| `contacts.list_request` | pc→phone | `{}` |
| `contacts.list` | phone→pc | `{contacts:[{id, name, starred, numbers:[{number, label}]}]}` |
| `call.dial` | pc→phone | `{number}` — placed via TelecomManager; audio stays on the phone |
| `call.action` (extended) | pc→phone | adds `end`, `speaker_on`, `speaker_off` |
| `call.state` (extended) | phone→pc | adds `number, caller_name, direction` |
| `photos.list` (extended) | pc→phone | `{offset, limit}` — paged, newest first |
| `photos.list_result` (extended) | phone→pc | adds `offset, total`; photos add `name, size` |
| `files.list` | pc→phone | `{path}` — relative to the phone's shared storage root |
| `files.list_result` | phone→pc | `{path, entries:[{name, is_dir, size, mtime}], free, total, error?}` |
| `files.get` | pc→phone | `{path}` — answered with `file.offer` (+chunks) carrying `source_path` |
| `file.offer` (extended) | pc→phone | optional `dest_path` (phone folder to save into, default `Download/PhoneBridge`) |
| `file.saved` | phone→pc | `{transfer_id, filename, path}` once a PC→phone file is written |
| `webcam.start` (extended) | pc→phone | `{facing: front\|back}` |
| `webcam.switch` | pc→phone | `{facing}` — restarts the stream on the other camera |
| `video.started` | phone→pc | `{kind: mirror\|webcam, width, height, rotation, facing?, input_available?}` |
| `video.stopped` | phone→pc | `{kind}` |
| `mirror.status` / `webcam.status` | phone→pc | `{state, message}` — consent pending/denied, missing permission, camera errors |
| `mirror.input` (extended) | pc→phone | adds `kind: long_press {x,y}` and keys `notifications, quick_settings, power, lock, screenshot, backspace, enter` |
| `apps.list` (extended) | phone→pc | apps add `system` |

**Paging.** `sms.list_threads`, `sms.get_thread`, `calls.history_request`,
`contacts.list_request` and `photos.list` take `{offset, limit}` (PC defaults: 20 / 20 / 20 /
50 / 50) and their answers carry `{offset, total, has_more}`; offset 0 replaces the PC's
list, later offsets append ("Load more"). `sms.get_thread` pages backwards from the newest
message; each page is returned oldest-first. While building any of these the phone sends
`load.progress {what, done, total}` (`what`: sms.threads, sms.thread, calls.history,
contacts, files, photos, apps) for the PC's progress bars.

**LAN discovery (UDP 28101).** If the stored `lan_ip` stops answering (new DHCP lease), the
phone broadcasts `{"phonebridge":"discover","fp":"<fingerprint>"}`; the PC replies
`{"phonebridge":"here","port":28100}` only for fingerprints it has a pairing record for,
and the phone connects to the replying address (TLS pinning still applies).

**Wi-Fi first pairing.** While the Pair dialog is open the PC also listens on TCP
28102 and runs the identical section-1 handshake there (same offer/hello/ack lines, same
encrypted lan_info frame; `lan_ip` is the address the phone reached). The QR payload adds
`lan: [ipv4…]` and `pport: 28102`; `bt_addr` may be null when the PC has no Bluetooth.
With only the PIN, the phone broadcasts `{"phonebridge":"pair_discover"}` to UDP 28101 and
the PC answers `{"phonebridge":"pairing_here","name","port"}` only while its dialog is
open. Bluetooth RFCOMM is the fallback; the phone turns Bluetooth on itself if needed.
Re-pairing removes the PC's older records with the same phone name.

**Bluetooth pairing channel.** The PC listens on RFCOMM channel 4 without an SDP record,
so the phone connects to channel 4 directly (UUID lookup is only a fallback).

**Unpaired / liveness.** A connection from an unknown fingerprint gets the plain bytes
`PBUNPAIRED\n` before close, so the phone shows "pair again" instead of retrying. The
phone sends `ping` every 15 s (PC answers `pong`); either side drops a link that has been
silent for ~45-50 s, and the phone reconnects at once when its network changes.

### 4c. Phone controls the PC (the phone app's Device/Files/Screen/Clipboard/Apps/Mouse/Keyboard/Ports tabs)

| type | direction | payload |
|---|---|---|
| `pc.info_request` → `pc.info` | phone→pc → pc→phone | `{interfaces (real adapters, as in pc.ports), hostname, user, os, kernel, arch, desktop, session, cpu_model, cpu_cores, cpu_pct, load_avg, ram_total, ram_free, swap_*, disk_total, disk_free, home_*, uptime_s, ips, battery_pct?, battery_status?, wifi_ssid?, wifi_signal_pct?}` |
| `pc.ports_request` → `pc.ports` | | `{ports:[{proto, port, address, service, process, exposed}], interfaces:[{name, kind: wifi\|ethernet\|virtual, mac, state, addresses:[{family, address, prefix}]}]}` |
| `pc.servers_request` → `pc.servers` | | `{docker: {available, error?, containers:[{id, name, image, state, status, ports, project}]}, services:[{name, unit, active, state, description}], programs:[{process, ports:["8080/tcp"]}]}` |
| `pc.server_action` | phone→pc | `{kind: "docker", id, action: start\|stop\|restart}` — answered with `pc.status {message, ok}` and a fresh `pc.servers`; system services are view-only (they need the PC's admin password) |
| `pc.apps_request` → `pc.apps` | | `{apps:[{id (desktop file name), name, comment, icon (base64 png)}]}` |
| `pc.app_launch` | phone→pc | `{id}` — answered with `pc.status {message}` |
| `pcfiles.list` → `pc.files` | | `{path}` relative to the PC user's home (nothing outside it) → `{path, entries:[{name, is_dir, size, mtime}], free, total, error?}` |
| `pcfiles.get` | phone→pc | `{path}` — answered with `file.offer`(+chunks, `from_pc: true`); the phone saves to Download/PhoneBridge |
| `pc.clipboard_get` | phone→pc | `{}` — answered with `clipboard.update {text, requested: true}` |
| `pc.input` | phone→pc | `{kind: move {dx,dy} \| click {button: left\|right\|middle} \| button {button, down} \| scroll {dy, dx?} \| tap_abs {x, y in [0,1] of the viewed window, button?} \| abs {x, y, down?} (drag) \| text {text} \| key {key (X keysym name or one char), mods: [Control_L, Alt_L, Shift_L, Super_L]}}` |
| `pc.screen_start` / `pc.screen_stop` | phone→pc | `{}` — shares the whole screen. PC answers `pc.screen_started {index, count, kind, width, height}` then streams `pc.screen_frame {size}` envelopes, each immediately followed by one raw frame holding a JPEG (≈8 fps, ≤1280 px wide; frames are dropped, never queued, if the phone falls behind) |
| `pc.term_open` | phone→pc | `{cols, rows, new?}` — starts the user's shell in a PTY (TERM=dumb, no pager), or re-attaches and replays recent output (`pc.term_output {data, replay: true}`); `new: true` starts a fresh shell |
| `pc.term_input` / `pc.term_resize` / `pc.term_close` | phone→pc | `{data}` raw keystrokes (e.g. `\r`, `\u007f`, `\u0003`, `\u001b[A`) / `{cols, rows}` / `{}` |
| `pc.term_output` / `pc.term_exit` | pc→phone | `{data}` (batched every ~30 ms) / `{code}` |
| `pc.remote_status` | pc→phone | `{state: waiting\|ready\|no_input\|denied, message}` — `no_input`: the screen is shared but GNOME's "Allow remote interaction" switch was off, so touch/typing can't work; the next `pc.screen_start` asks again |

Input and screen go through xdg-desktop-portal's RemoteDesktop portal (with a ScreenCast
stream in the same session) because Wayland allows nothing else: the first use shows
GNOME's "allow remote control / share screen" dialog on the PC; the portal restore token
is kept in `~/.config/phonebridge/remote_desktop_token` so later sessions don't ask again.

This table will grow as later phases add features, but the envelope and crypto
layer below it never changes — every later phase is additive.


### 4d. Messages added in v1.1 (all additive)

| type | direction | payload |
|---|---|---|
| `file.offer` (changed) | both | adds `resume_key`, `data_conn: true`; no raw chunks follow on the control channel — see section 1c |
| `file.accept` / `file.reject` | receiver->sender | `{transfer_id, offset}` / `{transfer_id, reason}` — now used |
| `clipboard.update` (extended) | both | image form: `{image: base64 PNG, mime}` (max 4 MiB); the text form is unchanged |
| `sms.thread_messages` (extended) | phone->pc | MMS rows are merged by time: `{mms: true, text, attachments:[{mime, name, thumb(base64 jpeg)}]}`; `sms_count` = SMS rows in the page (the PC pages by SMS rows only) |
| `hotspot.request` | pc->phone | `{action: start_local \| stop_local \| open_settings}` |
| `hotspot.state` | phone->pc | `{on, kind: local_only \| tether, ssid?, password?, tether_on?, error?, message?}` — local-only hotspot = private Wi-Fi with no internet; `open_settings` shows a notification that opens the phone's hotspot screen |
| `pc.status` (extended) | pc->phone | `{denied: true, message}` when "Allow PC control" is off for this phone |

### 4e. Sending MMS

| type | direction | payload |
|---|---|---|
| `sms.send_mms` | pc->phone | `{request_id, numbers: [str], text, attachments: [{mime, name, data(base64)}]}`; raw attachments total <= 4 MiB (control frames are capped at 8 MiB) |
| `sms.mms_status` | phone->pc | `{request_id, state: sending \| sent \| failed, message}` |

The phone shrinks pictures to JPEG so the whole message stays under 300 KB (what every carrier accepts), builds an
`M-Send.req` PDU (`MmsPdu.kt`, multipart/mixed, From = insert-address-token) in its cache and passes it to
`SmsManager.sendMultimediaMessage()` through a FileProvider URI. `sent` means the carrier accepted it.
Limits: Android lets only the default SMS app write to the message database, so the phone's own Messages app
does not list MMS sent this way (the PC keeps them in its open conversation until it is closed); no SMIL part, so
a few picky MMSCs may show the pictures without a slideshow layout; needs mobile data; group MMS = several
`numbers` (not tested).

### 4f. Per-app streaming

`mirror.start` accepts an optional `{app: "<package>"}`. The phone then streams only that app: it creates a private
virtual display (`DisplayManager.createVirtualDisplay`, flags PUBLIC | OWN_CONTENT_ONLY — no screen-capture consent,
nothing appears on the phone's own screen), feeds it to the same H.264 encoder and transports as whole-screen
mirroring (sections 1b / 1f), and starts the app on it with `ActivityOptions.setLaunchDisplayId` (needs "Display over
other apps"). `video.started` echoes `app`. Mirroring and app streaming are mutually exclusive; `mirror.stop` ends either.

Input (`mirror.input`) is routed to that display: taps/swipes via `GestureDescription.Builder.setDisplayId` (Android 12L+;
on older versions the app is view-only and `mirror.status {state: app_limit}` says so), text into the focused field of
the app's window. Back / Home / Recents / notification-shade keys have no per-display equivalent for an accessibility
service, so they answer `mirror.status {state: app_limit, message}` instead.

Best effort, not guaranteed: apps that opt out of resizing or multi-display (some games, banking apps with
FLAG_SECURE) show a black picture or refuse to start; the app opens a new task, so an already-running instance of a
single-instance app may stay on the phone's screen.

### 4g. iPhone

An iPhone client (not part of this repository any more) used the same protocol with these differences:

- **Pairing is Wi-Fi only** (section 1, "Wi-Fi first"): iOS exposes no Bluetooth RFCOMM. The phone's pairing hello
  carries `platform: "ios"` (absent = `android`); the PC stores it in the device record and treats anything else as
  `android`. The pairing handshake is otherwise byte-identical.
- **Supported messages:** `device.info` (battery, name, `platform`), `ping`/`pong`, `clipboard.update` (text),
  `file.offer`/`file.accept`/`file.reject`/`file.saved` with the data connections of section 1c, `link.disconnect`.
  `files.list` is answered with an empty `files.list_result` carrying an `error` that says iPhones can't browse files.
  Everything else (SMS, calls, notifications, photos, mirroring, webcam, apps, `pc.*`) is ignored by the iPhone.
- **PC UI:** for a `platform: ios` device the sidebar shows only Device and Files, and the PC does not send the
  SMS/calls/contacts/notification requests it sends to an Android phone on connect.
- **Lifetime:** iOS suspends the app in the background, so the control connection exists only while the app is open
  (it reconnects on return). Clipboard text is read when the app becomes active.

Call audio on the PC needs no protocol: the PC connects the phone's Bluetooth audio and switches it to the
HFP headset profile for the length of the call (`call_audio.py`). MMS can be viewed (section 4d) and sent (section 4e).


## 6. Screen sync (`sync.command` / `sync.ack`)

Session-level commands layered on mirroring (section 1b) to open each app from the other, confirm frames and manage the phone's
display. Envelope `payload` of `sync.command`: `{cmd, session_id, command_id, ts, origin, args}` (ids `[A-Za-z0-9_-]{8,64}`,
`ts` integer ms, `origin` `user|auto`); reply `sync.ack {command_id, ok, result, reason}`. Commands: `OPEN_PHONE_BRIDGE`,
`OPEN_PHONE_BRIDGE_VIEWER`, `START_MIRRORING`, `STOP_MIRRORING`, `DISPLAY_OFF`, `DISPLAY_ON`, `MIRROR_READY`, `CONNECTION_STATUS`.
Receivers validate cmd allow-list per direction, argument schema, freshness (±5 min), `command_id` uniqueness and session
binding; `sync.*` is governed by the **screen** permission. Full description: `docs/SCREEN_SYNC.md`.
