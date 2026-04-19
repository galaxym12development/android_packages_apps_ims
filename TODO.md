# PhhIms — Issue Tracker

## 1. Incoming call drops after answering [ACTIVE]

**Symptom:** Phone rings, user presses answer, call timer starts, call drops ~2 s later.
Caller hears "not available".

**Root cause:** 100 Trying was sent with a randomly-generated To-tag (from `completeResponseHeaders()`),
and the 183 Session Progress was sent with a *different* randomly-generated To-tag (`localToTag`,
computed later in the spawned thread). The Mavenir P-CSCF stored the first tag as the dialog anchor;
when the caller's PRACK arrived referencing the 183 tag, the P-CSCF could not route it and timed out,
sending CANCEL `Reason: SIP;cause=480;text="CC_NOT_REACHABLE"` after ~4 s.

**Fix applied (needs test):**
- `SipMessage.kt` — `completeResponseHeaders()`: skip adding To-tag for 100 Trying (RFC 3261 §12.1.1 permits omitting it)
- `SipHandler.kt` — `waitPrack()`: also exit when `callStopped` is set (avoids thread leak on CANCEL)
- `SipHandler.kt` — `handleCancel()`: `notifyAll()` on `prAckWaitLock` after setting `callStopped`

**Logs:** `radio_imcomming_real.log` / `logcat_incomming_real.log` (2026-04-18 21:28)
**Calls are rare (~2-3/week)** — verify carefully before deploying.

---

## 2. IMS registration failure after late SIM unlock [FIXED - needs test]

**Symptom:** After reboot, if SIM PIN is not entered immediately (~5 min delay),
the IMS stack never registers. `*#*#4636#*#*` shows "Not Registered".
If unlocked immediately after boot → Registered.

**Root cause (primary): singleton PhhMmTelFeature in PhhImsService**
`PhhImsService.createMmTelFeature()` returned the same `PhhMmTelFeature` instance for all
slot IDs. The framework calls it for both slot 0 and slot 1, then calls
`addImsFeatureStatusCallback()` on the returned instance for each. `ImsFeature` stores only a
single callback field — the slot 0 call overwrites the slot 1 callback. For delayed PIN unlock,
`featureState = STATE_READY` fires after both callbacks are registered, so only slot 0's
ImsManager is notified; slot 1 never sees `STATE_READY` and `onFeatureReady()` is never called.
Same singleton bug existed for `getRegistration()`.

**Root cause (secondary): subId race in initialize()**
At boot with a PIN-locked SIM, `getSubscriptionId(slotId)` may return -1 at `initialize()` time.
`TelephonyManager.createForSubscriptionId(-1)` creates a manager bound to no real subscription,
so its `ServiceStateListener` never fires when the SIM is later unlocked.

**Fix 1 applied (`PhhMmTelFeature.kt:initialize()`):**
- Wrapped `registerTelephonyCallback()` in `OnSubscriptionsChangedListener` to defer until a
  valid subId is available — handles both immediate and delayed PIN unlock.

**Fix 2 applied (`PhhImsService.kt`):**
- `createMmTelFeature()`: replaced singleton with `val mmTelFeatures = mutableMapOf<Int,
  PhhMmTelFeature>()` using `getOrPut(slotId)` — each slot gets its own feature instance.
- `getRegistration()`: same per-slot map for `ImsRegistrationImplBase`.

**Logs:** `radio_reg.log` / `logcat_reg.log` (2026-04-19 01:11–01:14, 01:31–01:33)

---

## 3. Periodic re-registration failure [LATER]

**Symptom:** Every ~50 min the re-REGISTER fails; user must reboot before making a call.

**To investigate:** Capture radio log during a 50-min window.
Look for the re-REGISTER sequence, especially whether 401 challenge handling succeeds
and what `Expires` value the server grants.

---

## 4. Binary patch breaks Voice Recorder app [LATER]

**Symptom:** After NOP patch at `libaudioproxy.so:0x9a46` (unconditional mic arming),
the stock Voice Recorder app stops working. Video recording and VoIP calls still work.

**Context:** The patch makes `proxy_open_capture_stream` always arm the ALSA mixer path,
even for non-call capture streams. The Voice Recorder probably uses a path that relied on
the guard to select a different mixer config.

**Alternative fixes to try** (see `RE/README.md`):
- Patch the `proxy_mode` value written by the audio HAL for SIP calls instead of NOP-ing the gate
- Check if patching `audio.primary` to force proxy_mode ∈ [17..23] is safer for non-call capture

**Priority:** Low — Voice Recorder is rarely used; VoLTE calls are the goal.

---

## 5. CP (circuit-switched) fallback with `MODE_IN_COMMUNICATION` [LATER]

**Symptom / concern:** Our Telecomm patch changes `MODE_IN_CALL → MODE_IN_COMMUNICATION`.
CP fallback calls may break because the baseband expects `MODE_IN_CALL` for hardware voice routing.

**To test:**
- Verify whether a privileged app can set `MODE_IN_COMMUNICATION` without the Telecomm patch
  (and whether the framework overwrites it back to `MODE_IN_CALL`)
- Check if the binary NOP patch independently breaks CP fallback

**Priority:** Low — CP fallback is not the primary use case.

---

## 6. Release & community [POST-FIX]

Once incoming calls work reliably:

- [ ] Capture a demo video (incoming + outgoing call, audio both ways)
- [ ] Build a flashable OTA or installable APK with all patches
- [ ] Write install guide (device tree changes, permissions file, HAL patch steps)
- [ ] Create a log-collection script that censors IMSI, phone numbers, IP addresses,
      and SIP credentials before users share logs
- [ ] Alternatively, update SipHandler logging to censor at the source
- [ ] Post on XDA / LineageOS forum for SM-A217F / SM-A546B

---

## Notes on testing

- Calls are limited: plan what to log *before* the call. Always capture `adb logcat -b radio`.
- Questions about SIP specs or Samsung HAL internals can be routed to a larger model
  (write them in a `.md` file and note here).
