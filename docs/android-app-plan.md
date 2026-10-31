# Plan: one Odysseus Android app with the relay built in

Status: **plan only, nothing built** (2026-10-20). Owner decisions are open (§10).

## Why

- Today the owner uses two things on the phone: the Odysseus PWA in a browser, and the
  `android-relay` app that carries recordings from the T-Watch Ultra (and syncs the T5
  PDA) to the servers. They are separate installs with separate settings and tokens.
- The goal is **one Android app** that looks and behaves like the Odysseus website, with
  every feature the website has, and also does everything the relay does.

## Decisions

- **Wrap the website, don't rewrite it.** The app is a native shell with a WebView on the
  Odysseus URL. Reasons:
  - Odysseus' `static/` is ~18 MB: 114 JS modules, ~123k lines (chat, agents, documents,
    email, notes, calendar, gallery, …). A native rewrite would have to be rebuilt feature
    by feature and chased as upstream changes daily.
  - The hub already ships a manifest, a service worker and standalone display mode, so
    the web UI is already designed to be used like an app.
  - "Same features and look" is only guaranteed if it is the same code.
- **The page can't be bundled in the APK.** `index.html` is templated per request with a
  CSP nonce (`{{CSP_NONCE}}`), the CSP is `connect-src 'self'`, and login is a
  `SameSite=lax` session cookie (`odysseus_session`). The WebView loads pages from the
  server over Tailscale. The UI therefore needs a connection to the hub.
- **The relay stays native.** BLE, the foreground service and uploads can't live in a
  WebView, and they must keep working with the UI closed or the hub unreachable.
- **Keep the relay's code and identity.** Same Kotlin packages, same app ID
  `org.barvis.sound.relay` (renaming breaks upgrades and paired watches; see AGENTS.md).
  Only the visible app label changes.
- **No Odysseus code in this repo.** The shell loads a URL and contains no copy of the
  hub's JS. This keeps the AGPL boundary of AGENTS.md ("Never copy Odysseus code").
- **Privacy is unchanged.** One configured host for the WebView (no navigation outside
  it), no Google services, no analytics, no crash reporting, HTTP redirects to other hosts
  not followed.

## Architecture

```
┌───────────────────────── Android app ─────────────────────────┐
│  Shell (Activity)                                              │
│   WebView ── Odysseus URL (Tailscale)  chat, agents, /modules/ │
│      ▲  origin-locked JS bridge                                │
│      │  (relay status, sync now, open settings)                │
│  Relay engine (existing, unchanged logic)                      │
│   RelayService (foreground, connectedDevice)                   │
│   DrainSession · PdaSync · GattClient · HttpUploader           │
└───────┬───────────────────────────────┬────────────────────────┘
        │ BLE                           │ HTTPS/HTTP
   T-Watch Ultra / T5                   ├─ Athena :8000  (token)
                                        └─ Odysseus :7000        (WebView session; PDA token)
```

Three layers:

1. **Shell.** One Activity hosting the WebView. Cookie persistence, file upload and
   download, back button, IME and insets, microphone permission for the voice recorder,
   share target, deep links (`/?ask=`), pull-to-refresh off. Launcher icon and splash
   from the Odysseus theme.
2. **Relay engine.** `protocol/`, `relay/`, `net/`, `ble/`, `service/`, `store/` moved in
   as they are, with their tests. The foreground service and boot receiver keep running
   with the UI closed.
3. **Bridge.** A small `@JavascriptInterface` (or `WebMessagePort`) limited to the
   configured origin. It exposes relay state (watch/T5 status, queue, last sync, last
   error) and a few commands (sync now, open native settings). It never exposes tokens.
   The hub side is a new file plus one small hook (`# Fork: …`) that shows a relay card
   on `/modules/sound`, listed in `FORK.md`. Without the bridge (plain browser) the card
   does not render.

Native screens kept from today's app: **Settings** (server URL, ingest token, Odysseus
URL and token, PDA options, boot start) and the **relay log**. These can't be web pages,
because they hold the tokens and work without the hub.

## Upstream merges

The wrapper does not add to fork merge cost: the shell loads whatever the hub serves, so
UI work and upstream syncs reach the app on the next page load with no APK rebuild. What
can still break is the **contract between shell and page**:

| Contract | Risk if upstream changes it |
|---|---|
| `odysseus_session` cookie, login flow, `SameSite`/`Secure` rules | login or session loss |
| CSP, framing headers (`frame-ancestors 'none'`) | blocked loads in the WebView |
| `/api/codex/calendar/events`, `/api/codex/todos` | PDA sync (exists today) |
| `/?ask=` and other deep-link parameters | share target and notification links |
| `window` globals the bridge card reads | relay card |

Mitigation: one smoke test of the contract (sandbox Odysseus on `127.0.0.1:7010`), and a
short checklist in the hub's `docs/fork/upstream-sync.md` to run after each upstream sync.

## Phases

| # | Work | Size |
|---|---|---|
| 0 | Owner answers §10; pick the repo layout | days |
| 1 | **Shell:** WebView on the configured URL, login, cookies, uploads/downloads, mic, back, IME, icon, splash. No relay change yet. | ~1 week |
| 2 | **Merge the relay:** move the module in, one Settings screen, one token store (Keystore AES-GCM as now), service and boot receiver intact, all current tests green. | ~1 week |
| 3 | **Bridge and relay card:** JS interface, origin lock, hub-side card on `/modules/sound`, status in the notification and in the page. | ~1 week |
| 4 | **Hardening:** battery-manager guidance, WebView killed while the service lives, HTTP vs HTTPS, release signing, CI (`assembleDebug test lint`, contract smoke test), release workflow. | ~1 week |
| 5 | **Optional native extras:** share target, widgets, biometric lock, ntfy-backed notifications, per-route icons. | open |

About 3–5 weeks for one developer; most of it is device testing.

## Risks

- **Secure context.** `getUserMedia` (the voice recorder) needs HTTPS. The relay allows
  cleartext `http://` for LAN/Tailscale hosts, but a WebView on plain HTTP loses the mic.
  A Tailscale HTTPS certificate becomes effectively required for the UI.
- **Process death.** Android may kill the WebView or the whole app; the foreground service
  must survive and the WebView must restore its session.
- **Vendor battery managers** (Xiaomi, Huawei, Samsung…) already affect the relay; a
  larger app does not make that easier.
- **Bridge security.** An exposed JS interface is reachable by any page in the WebView:
  lock navigation to the one host, load nothing third-party, expose no secrets.
- **Licence.** Distribution of a build that loads the AGPL hub's UI is the owner's call;
  the shell itself contains no hub code.
- **Offline.** The UI is unavailable without the hub. The relay is not affected.

## Tests

- Relay: the existing JVM tests move with the code and stay green (67+ today).
- Shell: unit tests for URL/origin locking, bridge allow-list and settings migration
  (existing installs keep their server URL and tokens).
- Contract smoke test against the sandbox Odysseus (login, cookie persists, `/modules/sound`
  loads, bridge call reaches the page).
- Checks as in AGENTS.md: `cd android-relay && ./gradlew assembleDebug test lint`.

## Server verification (owner) — for the PRs that implement this

Real-device steps, none can run in the sandbox:

1. Install over the existing relay APK: server URL and tokens survive, the watch stays
   paired.
2. Open the app: the Odysseus login appears, the session survives a restart of the app
   and a phone reboot.
3. Send a chat message, upload a file, use the voice recorder (over HTTPS).
4. Close the UI, drain the watch: chunks reach the server and are acked as before.
5. Open `/modules/sound`: the relay card shows the watch state and "sync now" works.
6. With Odysseus unreachable (phone off Tailscale, for example), the page fails to load
   and the relay keeps draining. (The server's Odysseus test instance is retired.)
7. Deploy only after verification: `scripts/fork_deploy.sh deploy` (hub side).

## 10. Open questions for the owner

1. **Repo layout.** Keep everything in `barvis-sound/android-relay` (the module owns the
   relay, the app ID stays) with the shell loading a configurable URL, or a new app in
   `SDarioosh/Barvis` that consumes the relay as a module? Recommended: the first.
2. **App label.** Keep `org.barvis.sound.relay` as the ID (recommended) and show
   "Odysseus" as the label?
3. **How native?** Pure WebView first (recommended), or native chat/widgets from the
   start?
4. **Tokens.** Keep the separate `ody_` token for PDA sync (recommended, already works),
   or switch to the WebView session?
5. **Distribution.** Sideloaded debug APK as now, or a signed release channel? Is a
   Tailscale HTTPS certificate available on the server?
