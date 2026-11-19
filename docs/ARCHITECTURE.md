# Architecture

Everything below was checked against the source (file names in backticks). The wire protocol is specified in `protocol/PROTOCOL.md`.

## Components
```mermaid
flowchart LR
  subgraph Phone["Android app dev.phonebridge (Kotlin, no XML layouts)"]
    UI["MainActivity + PcPanels.kt<br/>tabs: Home, Device, Files, Screen, Apps, Terminal, Ports, Servers, Security"]
    SVC["BridgeService (foreground service)<br/>message router"]
    NET["Connection.kt: pinned TLS + v4 handshake<br/>SecureSession.kt: AES-256-GCM"]
    FEAT["Sms/Mms, Calls, Media (files/photos), Video (MediaProjection/Camera2 + MediaCodec),<br/>UdpVideo, Clipboard, NotificationRelay, InputService (accessibility), ScreenSync/DisplayPower"]
    SEC["KeystoreVault, PcAccess (permission gate), SecurityPanel, PhoneAudit (signed log), Privacy"]
    UI --> SVC --> NET
    SVC --> FEAT
    SVC --> SEC
  end
  subgraph PC["Linux app phonebridge (Python, GTK4 + libadwaita)"]
    GUI["ui/main_window.py + 11 pages"]
    NT["net_thread.py: asyncio loop"]
    TR["transport.py ControlChannelServer<br/>secure_session.py / crypto.py"]
    CTRL["pc_control.py: shell, files, input, screen, docker, apps"]
    DATA["file_transfer.py, udp_video.py, phone_fs.py (FUSE)"]
    SS["screensync/*: adb display power, watchdog"]
    SECP["security/*: keystore, ledger, permissions, throttle, anchor"]
    GUI <--> NT --> TR
    TR --> CTRL
    TR --> DATA
    GUI --> SS
    TR --> SECP
  end
  NET == "TLS 1.3 (pinned self-signed cert) + per-direction AES-256-GCM" ==> TR
  NET -. "UDP video (encrypted datagrams)" .-> DATA
  NET -. "USB: adb reverse" .-> TR
  NET -. "optional self-hosted relay" .-> RELAY["relay_server.py"] -.-> TR
  SS -. "adb shell cmd display power-off/reset" .-> Phone
```

## Ports and discovery (from `transport.py`, `relay_server.py`, `mdns.py`)
Control/TLS server `28100` (`DEFAULT_PORT`), UDP discovery `28101` (`DISCOVERY_PORT`), relay default `28200`, mDNS service `_phonebridge._tcp`. Also BLE wake and Bluetooth RFCOMM for pairing. The server drops sources that are not loopback/USB/private/link-local/attached networks (`peer_policy.py`; `PHONEBRIDGE_ALLOW_PUBLIC=1` disables).

## Connection and pairing
```mermaid
sequenceDiagram
  participant PC
  participant Phone
  PC->>PC: show QR (PC pubkey, cert fingerprint) + 6-digit PIN, 60 s, single use
  Phone->>PC: find PC (QR scan / Bluetooth / LAN broadcast / USB tunnel)
  Phone->>PC: SPAKE2 (P-256) messages; PIN never sent
  PC-->>Phone: key confirmation; both show a confirmation code
  Note over PC,Phone: user approves; Ed25519 identities and TLS cert pin are stored
  Phone->>PC: later: TLS 1.3 connect, pinned cert
  Phone->>PC: signed ephemeral X25519 handshake, key confirmation
  PC-->>Phone: per-direction AES-256-GCM keys, replay window, in-band rekey
```
3 wrong PINs burn the code (`pairing.py`, `spake2.py`, `Pairing.kt`, `Spake2.kt`).

## Screen mirroring
```mermaid
sequenceDiagram
  participant PC
  participant Phone
  PC->>Phone: mirror.start (control channel)
  Phone->>Phone: Android consent dialog (MediaProjection), user taps Start now
  Phone->>PC: open second connection "<fp>:video" or UDP probe
  loop each encoded frame
    Phone->>PC: H.264 access unit (TCP frame or encrypted UDP datagram)
  end
  PC->>Phone: mirror.input (tap/swipe/keys) -> InputService (accessibility)
  PC->>Phone: mirror.stop
```
UDP video has loss-driven bitrate adaptation and keyframe requests; it falls back to TCP (`udp_video.py`, `UdpVideo.kt`, `Video.kt`).

## File transfer
```mermaid
sequenceDiagram
  participant Sender
  participant Receiver
  Sender->>Receiver: file.offer {id, name, size, resume_key} (control)
  Receiver->>Sender: file.accept {offset} or file.reject
  Note over Sender,Receiver: phone opens a separate TLS data connection "<fp>:file:<id>:<offset>"
  Sender->>Receiver: AES-GCM frames (<=256 KiB) from offset
  Sender->>Receiver: trailer SHA256:<hex>
  Receiver->>Sender: OK / BAD (partial discarded on BAD)
  Receiver->>Sender: file.saved
```

## Remote control of the PC (phone controls the PC)
Messages `pc.*` / `pcfiles.*` are handled in `pc_control.py` only if the phone's "Allow PC control" is on **and not expired** (default 60 min, `PHONEBRIDGE_PC_CONTROL_MINUTES`). It provides: system info, ports, services/Docker start-stop, app launch, files under `$HOME` (no dot-paths), clipboard, mouse/keyboard and screen view via the RemoteDesktop portal (`pc_remote.py`), and a PTY shell (`terminal.py`, environment without `PHONEBRIDGE_*`).

## Storage
PC: `~/.config/phonebridge/` (device JSON files, `audit.jsonl`, certs, settings); secrets in the desktop keyring or a 0600 file store. Phone: app-private storage, Android-Keystore-wrapped keys. No database. Details: `docs/SECURITY.md`.
