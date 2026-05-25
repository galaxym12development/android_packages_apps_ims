# PhhIms — Issue Tracker

## 1. Incoming call drops after answering [FIX COMMITTED - needs test]

**Symptom:** Phone rings, user presses answer, call timer starts, call drops ~2 s later.
Caller hears "not available".

**Root cause:** 100 Trying was sent with a randomly-generated To-tag (from `completeResponseHeaders()`),
and the 183 Session Progress was sent with a *different* randomly-generated To-tag (`localToTag`,
computed later in the spawned thread). The Mavenir P-CSCF stored the first tag as the dialog anchor;
when the caller's PRACK arrived referencing the 183 tag, the P-CSCF could not route it and timed out,
sending CANCEL `Reason: SIP;cause=480;text="CC_NOT_REACHABLE"` after ~4 s.

**Fix committed (`b0af852`):**
- `SipMessage.kt` — `completeResponseHeaders()`: skip adding To-tag for 100 Trying. RFC 3261
  §8.2.6.2 actually *forbids* it when the request had no To-tag — adding one was an outright
  RFC violation, not just a Mavenir-specific quirk.
- `SipHandler.kt` — `waitPrack()`: exit when `callStopped` is set (avoids thread leak on CANCEL).
- `SipHandler.kt` — `handleCancel()`: `notifyAll()` on `prAckWaitLock` after setting `callStopped`
  so `waitPrack` wakes immediately instead of polling for up to 1 s.

**Logs:** `radio_imcomming_real.log` / `logcat_incomming_real.log` (2026-04-18 21:28)
**Calls are rare (~2-3/week)** — verify on next incoming call. Watch for: no PRACK timeout,
no `cause=480 CC_NOT_REACHABLE` CANCEL, dialog established with the same To-tag in 183/180/200.

---

## 2. IMS registration failure after late SIM unlock [FIXED - verified]

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

**Root cause (secondary): per-subId TelephonyCallback dies on SIM PIN→READY**
The original code registered a `TelephonyCallback.ServiceStateListener` on a `TelephonyManager`
created via `createForSubscriptionId(subId)`. When the SIM transitions PIN-locked → READY, the
framework rebuilds the per-subscription state for that `subId` and silently drops our callback.
Diagnostic logs confirmed `onServiceStateChanged` fired twice with `STATE_OUT_OF_SERVICE`, then
never again — even after the framework broadcast `mVoiceRegState=0(IN_SERVICE)` for the same subId.

**Fix committed (`b3431da`):**
- `PhhImsService.kt` — replaced singleton with per-slot maps (`mmTelFeatures` and
  `imsRegistrations`) using `getOrPut(slotId)`. Each slot gets its own feature instance and
  its own `mImsFeatureStatusCallback` slot.
- `PhhImsBroadcastReceiver.kt` — alarm handler iterates the map so periodic re-REGISTER fires
  for every active slot.
- `PhhMmTelFeature.kt:initialize()` — dropped the `ServiceStateListener` entirely. Use
  `OnSubscriptionsChangedListener` (lives on `SubscriptionManager`, not on a per-subId object,
  so it survives the subscription rebuild) and gate `STATE_READY` on `simOperator` being
  non-empty — that's the exact field `SipHandler` dereferences at construction. The network-up
  wait happens later in `SipHandler.getVolteNetwork()`.

**Diagnostic logs:** `radio_reg.log` / `logcat_reg.log` (2026-04-19 10:24) captured the
secondary failure mode — `ServiceStateListener` fired twice with `STATE_OUT_OF_SERVICE` then
silently stopped after `mVoiceRegState=0(IN_SERVICE)` was broadcast. Confirmed working on
device after switching to the `OnSubscriptionsChangedListener` + `simOperator` gate.

---

## 3. Periodic re-registration failure [LATER]

**Symptom:** Every ~50 min the re-REGISTER fails; user must reboot before making a call.

**To investigate:** Capture radio log during a 50-min window.
Look for the re-REGISTER sequence, especially whether 401 challenge handling succeeds
and what `Expires` value the server grants.

---

## 4. Binary patch breaks Voice Recorder / video recording [FIXED]

**Symptom (old Patch A):** After NOP patch at `libaudioproxy.so:0x9a46` (unconditional mic
arming), the stock Voice Recorder app and video recording stopped working. VoIP calls worked.

**Root cause of breakage:** Patch A removed the gate entirely, so ALL 48 kHz captures took
the mixer-arming path even when the framework was in `MODE_NORMAL`. This corrupted mixer
state for non-call captures.

**Fix committed (Patch F v2 — conditional hook):**
Instead of NOP-ing the gate, the final patch is a **conditional hook** in
`proxy_create_capture_stream` at `0xa30e`. It checks `ausage_param`:
- `ausage_param == 1` (`MIC` / `VOICE_COMMUNICATION`) → `AUSAGE = 12` → `pcm12c` (real mic)
- `ausage_param == 2` (`CAMCORDER`) → `AUSAGE = 110` → `pcm110c` (stock path)
- `ausage_param == 27` (`VOICE_RECOGNITION`) → `AUSAGE = 110` → stock path

**Verified on device 2026-05-25:**
- SIP call with `VOICE_COMMUNICATION` → `pcm12c` open, audio works both ways
- Video recording (`CAMCORDER`) → works (stays on stock `pcm110c` path)
- Voice Recorder (`MIC`) → works (also gets `pcm12c`, which is correct for general mic use)


---

## 5. CP (circuit-switched) fallback with `MODE_IN_COMMUNICATION` [LATER]

**Symptom / concern:** Our Telecomm patch changes `MODE_IN_CALL → MODE_IN_COMMUNICATION`.
CP fallback calls may break because the baseband expects `MODE_IN_CALL` for hardware voice routing.

**To test:**
- Verify whether a privileged app can set `MODE_IN_COMMUNICATION` without the Telecomm patch
  (and whether the framework overwrites it back to `MODE_IN_CALL`)
- Check if the conditional HAL patch (Patch F v2) independently affects CP fallback
  (it should not — the hook only touches `stream_type=11` capture, not the voice-call
  path which uses `stream_type=12` / `stream_type=24`)

**Priority:** Low — CP fallback is not the primary use case.

---

## 6. Release & community [POST-FIX]

Once incoming calls work reliably and the HAL patch is persistent:

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
