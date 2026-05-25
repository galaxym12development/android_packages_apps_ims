# Reverse Engineering — Samsung Exynos3830 Audio HAL

Goal: understand why `AudioRecord` with `VOICE_COMMUNICATION` source produces silence
during a SIP/VoLTE call on Samsung A21s (SM-A217F, Exynos 850).

## Binaries

| File | Source on device | Size |
|------|-----------------|------|
| `binaries/libaudioproxy.so` | `/vendor/lib/libaudioproxy.so` | ~64 KB |
| `binaries/audio.primary.universal3830.so` | `/vendor/lib/hw/audio.primary.universal3830.so` | ~68 KB |

Refresh from a connected device: `bash scripts/pull_binaries.sh`

## Device audio topology (A21s, Exynos3830-Madera)

Three ALSA cards:

| Card | Name | Role |
|------|------|------|
| 0 | Exynos3830-Madera (Cirrus Logic CS47L92) | Main card — real mic + modem uplinks |
| 1 | aboxvdma | Abox DSP virtual DMA |
| 2 | aboxdump | Abox DSP debug |

Key capture PCMs on card 0:

| PCM | ID | What it is |
|-----|----|------------|
| pcm12c | WDMA0 | **Real microphone** via Abox DSP — 48 kHz |
| pcm13c–pcm16c | WDMA1–4 | Additional real capture paths |
| pcm110c–pcm129c | calliope_10–29 | **Modem/baseband uplink** (CP audio) — produces silence for software IMS |

During a SIP call, `AudioRecord` should open a WDMA path. If a calliope path opens instead
(visible in `/proc/asound/card0/pcm110c/sub0/status`), the hardware is routing to the
modem uplink and the captured audio will be silent.

## Root cause: wrong ALSA device selection for capture

`AudioRecord` with source `VOICE_COMMUNICATION` produces silence because the Samsung
HAL opens the **modem uplink** capture device instead of the **real microphone**.

### Verified chain of causation

`verify_stream_offsets.py` confirms `audio.primary` does NOT set the ALSA device
number. The device selection happens inside `libaudioproxy.so` in two stages:

**Stage 1 — `proxy_create_capture_stream` (vaddr 0x9ee8):**
The inner TBH6 for `stream_type=11` sets `AUSAGE = 0x6e` (110) and writes it to
`stream+12` via `str r6, [r8, #12]` (multiple epilogue sites: 0xa1ee, 0xa320,
0xa34c, 0xa382).

**Stage 2 — `proxy_open_capture_stream` (vaddr 0xa9f0):**
After the primary gate passes (`proxy_mode ∈ [17..23]`), a secondary check at
`0xaa48–0xaa50` skips the TBB when `stream->sample_rate == 48000` (0xbb80):

```asm
0x00aa48:  ldr  r0, [r4, #0x1c]     ; r0 = stream->sample_rate
0x00aa4a:  movw r1, #0xbb80           ; r1 = 48000
0x00aa4e:  cmp  r0, r1
0x00aa50:  beq  0xaae0                ; if 48000, skip TBB + helper
```

Since `pcm_config_primary_capture` is **48 kHz**, this branch **always** fires for
standard capture. Execution jumps to `0xaae0`, bypassing the TBB. The AUSAGE from
stage 1 (110) remains in `stream+12` and reaches `pcm_open`.

**Stage 3 — `pcm_open`:**
`ldrd r6, r8, [r4, #8]` at `0xab0c` loads `(card=0, device=110)` into the registers
passed to `pcm_open(card=0, device=110, ...)`. The ALSA device opened is **pcm110c**
(`calliope_10`, the modem/baseband uplink). The real mic is on `pcm12c` (WDMA0).

### What the gate does and does not do

`libaudioproxy.so :: proxy_open_capture_stream` gates ALSA mixer path arming behind:

```
global_proxy->field_0x38  (proxy_mode)  ∈  [17 .. 23]
```

**Script-verified:** for `MODE_IN_COMMUNICATION` the Samsung main path already returns
**22** when aproxy sub-flags are zero. 22 is **inside** `[17..23]`, so the gate
**already passes** on stock HAL. The mixer IS armed. The silence is therefore **not**
caused by the gate failing — it is caused by the secondary gate skipping the TBB,
leaving AUSAGE=110 in place, which opens the wrong ALSA device.

## proxy_mode computation — confirmed by RE of `audio.primary.universal3830.so`

Function at **vaddr 0x089b4** in `audio.primary.universal3830.so` reads
`hw_dev->field_0x114` (the Android `AudioManager` mode) and returns the Samsung internal
`proxy_mode` stored in `global_proxy->field_0x38`.

### Android mode → proxy_mode mapping

| Android mode | Value | proxy_mode | In [17..23]? | Mixer armed? |
|---|---|---|---|---|
| `MODE_IN_CALL` | 2 | **24–26** | NO | NO → mic silent |
| `MODE_IN_COMMUNICATION` (main path) | 3 | **22** (fallback) or 20–21–23 (conditional) | YES | YES |
| `MODE_IN_COMMUNICATION` (alternate, `field_0x100` set) | 3 | **37** | NO | NO → Telecom patch breaks mic |

### MODE_IN_CALL path (0x089cc–0x089e2)

**Note:** the actual code checks `MODE_IN_COMMUNICATION` (3) **first** at `0x089c8`.
The `cmp r2,#2` below is only reached when mode ≠ 3.

```asm
0x089c8: cmp  r2, #3
0x089ca: beq  0x08a1e          ; =3: MODE_IN_COMMUNICATION branch
0x089cc: cmp  r2, #2
0x089ce: bne  0x08a5c          ; ≠2: else branch (other modes)
0x089d0: ldr.w r1,[r1,#0xa0]
0x089d4: movs r0,#0x18         ; return 24
0x089d6: cmp  r1, #3
0x089da: movs r0,#0x19         ; if r1==3: return 25
0x089de: movs r0,#0x1a         ; if r1==4: return 26
0x089e2: pop  {r4,pc}          ; → 24–26, ABOVE [17..23], guard FAILS
```

### MODE_IN_COMMUNICATION path (0x08a1e–0x08a86)

Conditional returns for non-zero aproxy sub-flags, **but the fallback is 22**
(`0x16`), which is **always inside** `[17..23]`:

```asm
0x08a1e: ldr.w r1,[r0,#0xf4]     ; r1 = aproxy ptr
0x08a22: cbz  r1, #0x8a70        ; NULL → branch to 0x8a70 (returns 22)
0x08a24: ldrb r2,[r1,#0x5]       ; aproxy->field_0x5
0x08a28: IT NE → movs r0,#0x14   ; ≠0 → return 20 ✓
0x08a2e: ldrb.w r2,[r1,#0x39]    ; aproxy->field_0x39
0x08a32: cmp  r2, #1
0x08a34: bne  0x08a70
0x08a36: ldr  r2,[r1,#0x18]      ; aproxy->field_0x18
0x08a3c: IT NE → movs r0,#0x17   ; ≠0x10 → return 23 ✓
0x08a70: ldr.w r1,[r0,#0x108]    ; hw_dev->field_0x108
0x08a74: movs r0,#0x16           ; return 22 ✓  ← FALLBACK, inside [17..23]
0x08a7a: pop  {r4,pc}            ; field_0x108==0 → return 22
0x08a7c: ldr.w r1,[r1,#0xa0]
0x08a84: IT EQ → movs r0,#0x15   ; r1==1 → return 21 ✓
```

**Key consequence:** on stock HAL, for `MODE_IN_COMMUNICATION` with no modem
sub-flags, `proxy_mode_compute` returns **22**. The gate passes, the mixer IS armed,
but `calliope_10` still opens because the TBB selects AUSAGE=110. The silence is
**not** caused by the gate failing.

## ELF section maps

### libaudioproxy.so
| Section | vaddr | fileoff |
|---------|-------|---------|
| .text | 0x1000 | 0x0000 |
| .data | varies | vaddr − 0x3000 |

### audio.primary.universal3830.so
| Section | vaddr | fileoff |
|---------|-------|---------|
| .text | 0x7260 | 0x6260 |
| .got | 0x11704 | 0xf704 |

## Key libaudioproxy.so symbols

| Symbol | vaddr | size |
|--------|-------|------|
| `proxy_create_capture_stream` | 0x9ee8 | 1332 |
| `proxy_open_capture_stream` | 0xa9f0 | 1024 |

### pcm_config selection in `proxy_create_capture_stream`

`proxy_create_capture_stream` uses nested TBH (Table Branch Halfword) tables:

1. **Outer TBH1** at `0x9f20` (base `0x9f24`): indexed by `stream_type - 0xa`.
   - `stream_type=11` → `TBH1[1]` → target `0xa094`

2. **Inner TBH6** at `0xa0ac` (base `0xa0b0`): indexed by **`stream_type - 1`**
   (NOT `ausage_param - 1` as initially assumed). The code loads the stream struct
   at `[r8]`, subtracts 1, and uses that as the table index.
   - `stream_type=11` → index `10` → target `0xa30e` → `AUSAGE = 0x6e` (110)

3. **Epilogue mapping**:
   - Target `0xa30e` sets `AUSAGE=0x6e` then branches to **epilogue_E** (`0xa37a`)
   - epilogue_E loads the `pcm_config` pointer from a literal pool; when resolved
     via `.rel.dyn` (LIEF), the pointer is **`pcm_config_primary_capture`** at
     `GOT[0x10a48]` (48 kHz, 2 ch).

**Important:** The `ausage_param` (1 for MIC/VOICE_COMM, 2 for CAMCORDER, etc.) does
NOT influence the pcm_config for `stream_type=11`. All AudioSources that map to
`stream_type=11` hit the same `TBH6[10]` entry and the same pcm_config.

### `proxy_open_capture_stream` TBB switch (0xaa6e)

`proxy_open_capture_stream` contains a TBB at `0xaa6e` indexed by `stream_type - 1`.
For `stream_type=11` (index 10) the target `0xaaae` sets `AUSAGE = 0x6e` (110).

**However, the TBB is NEVER reached for standard capture.** The secondary gate at
`0xaa50` (`beq 0xaae0`) fires whenever `stream->sample_rate == 48000` (0xbb80) —
which is always true because `pcm_config_primary_capture` is 48 kHz. Execution
jumps straight to the local helper at `0xaae0`, **skipping the TBB entirely**.

Control flow when the primary gate passes:

```asm
0x00aa40:  ldr  r0,[r5,#0x38]       ; primary gate: proxy_mode
0x00aa42:  subs r0,#0x11
0x00aa44:  cmp  r0,#6
0x00aa46:  bhi  0xaae0                ; skip if proxy_mode outside [17..23]
0x00aa48:  ldr  r0, [r4, #0x1c]       ; r0 = stream->sample_rate
0x00aa4a:  movw r1, #0xbb80           ; r1 = 48000
0x00aa4e:  cmp  r0, r1
0x00aa50:  beq  0xaae0                ; ALWAYS taken for 48kHz → TBB SKIPPED
0x00aa5a:  blx #0xf170                ; (unreachable for standard capture)
0x00aa6e:  tbb [pc, r0]               ; (unreachable for standard capture)
0x00aabc:  str r7, [r4, #0xc]         ; (unreachable for standard capture)
...
0x00aadc:  bl  #0xa6f0                 ; local helper call
0x00ab0c:  ldrd r6, r8, [r4, #8]      ; loads stale AUSAGE=110 from stage 1
0x00ab92:  blx #0xf310                ; pcm_open(card=0, device=110, ...)
```

**Conclusion:** the TBB at `0xaa6e` is dead code for standard capture. The AUSAGE
that reaches `pcm_open` is the one written by `proxy_create_capture_stream` (110),
not the TBB result.

## AudioSource → stream parameters

`verify_audiosource_primary.py` disassembles the function that calls
`proxy_create_capture_stream` (at 0xa760) and finds **direct `cmp` instructions
against AudioSource values**. This function sets both `stream_type` and
`ausage_param` before creating the stream:

**Main path** (when `r5+276 != 2`):

| AudioSource | stream_type | ausage_param |
|-------------|-------------|--------------|
| MIC (1) | 11 | 1 |
| CAMCORDER (5) | 11 | 2 |
| VOICE_RECOGNITION (6) | 11 | 27 |
| VOICE_COMMUNICATION (7) | 11 | 1 |

**Special voice-call path** (when `r5+276 == 2`):

| AudioSource | stream_type | ausage_param |
|-------------|-------------|--------------|
| VOICE_DOWNLINK (3) | 12 | 25 |
| VOICE_UPLINK (2) | **24** | 26 |

The special path maps `VOICE_UPLINK` to `stream_type=24`, which is outside
`[17..23]` and would skip mixer arming entirely. This path is only reachable
when the voice-call sub-flag (`r5+276 == 2`) is set; on stock HAL for a SIP
call the main path is taken.

**Conclusion:** all main-path sources (including `VOICE_COMMUNICATION`) map to
`stream_type=11` → `AUSAGE=0x6e` (110) → `pcm110c` (calliope_10, modem uplink).
Changing `AudioSource` alone cannot fix the silence.

## Dumps

- `dumps/proxy_create_capture_stream.asm` — raw disassembly text from r2 (rasm2), vaddr 0x9ee8

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/pull_binaries.sh` | Pull fresh .so files from connected device |
| `scripts/disasm_libaudioproxy.py` | Capstone-based Thumb-2 disassembly of libaudioproxy functions |
| `scripts/disasm_audio_primary.py` | Manual Thumb-2 decoder for audio.primary (no exported symbols) |
| `scripts/decode_tbh.py` | Decode Thumb-2 TBH (Table Branch Halfword) tables from ARM binaries |
| `scripts/resolve_got.py` | Resolve GOT entries in libaudioproxy.so by parsing ELF headers |
| `scripts/trace_capture_stream.py` | Trace `proxy_create_capture_stream` AUSAGE selection for a given (stream_type, ausage_param) |
| `scripts/verify_open_capture_stream.py` | Verify gate logic, TBB decode, and operation order in `proxy_open_capture_stream` |
| `scripts/verify_proxy_setters.py` | Verify `proxy_set_route` and `proxy_set_audiomode` field writes |
| `scripts/verify_patch_offsets.py` | Verify Patch A and Patch C file offsets and target bytes |
| `scripts/verify_audio_source_mapping.py` | Verify `voice_is_call_mode` PLT call and stream_type assignments in `audio.primary::update_capture_stream` |
| `scripts/verify_plt_calls.py` | Resolve PLT targets for all `bl`/`blx` inside `proxy_open_capture_stream` and `proxy_create_capture_stream` |
| `scripts/verify_audiosource_primary.py` | Decode AudioSource → (stream_type, ausage_param) in the function that calls `proxy_create_capture_stream` |
| `scripts/verify_stream_offsets.py` | Verify who writes `stream+8` (card) and `stream+12` (device/AUSAGE) and when |
| `scripts/patch_audio_primary.py` | Apply broad Patch B to `audio.primary.universal3830.so` (not recommended) |
| `scripts/patch_audio_primary_targeted.py` | Apply targeted Patch C to `audio.primary.universal3830.so` alternate path |
| `scripts/patch_libaudioproxy.py` | Apply old Patch A (NOP gate) to `libaudioproxy.so` (rejected, see above) |
| `scripts/restore_libaudioproxy.py` | Restore `libaudioproxy.so` from `.orig` backup |
| `scripts/patch_ausage_stream_type_11.py` | Apply **final conditional hook** (Patch F v2) to `libaudioproxy.so` |

```sh
pip install capstone
python3 scripts/disasm_libaudioproxy.py
python3 scripts/disasm_audio_primary.py
python3 scripts/disasm_audio_primary.py --func proxy_mode_compute
python3 scripts/disasm_audio_primary.py --vaddr 0x089b4 --size 0xe0
python3 scripts/decode_tbh.py binaries/libaudioproxy.so 0x9f4a 0x9f4c 16
python3 scripts/resolve_got.py 0x10a3e 0x10a42 0x10a46 0x10a48 0x10a54
python3 scripts/trace_capture_stream.py 11 1
python3 scripts/verify_open_capture_stream.py
python3 scripts/verify_proxy_setters.py
python3 scripts/verify_patch_offsets.py
python3 scripts/verify_audio_source_mapping.py
python3 scripts/verify_plt_calls.py
python3 scripts/verify_audiosource_primary.py
python3 scripts/verify_stream_offsets.py
```

## Who writes `global_proxy->field_0x38` (the gate)

Key fact: **`libaudioproxy.so` is NOT stripped** — it exports ~85 `proxy_*`
functions. `readelf --dyn-syms binaries/libaudioproxy.so` lists them all; the
updated `scripts/disasm_libaudioproxy.py --list` prints them with sizes.

### `proxy_set_route` (vaddr 0xbb84, 996 B) — the writer

Signature reconstructed from the prologue:

```
proxy_set_route(proxy*, r1=mode, r2=type, r3=delta)
```

The `field_0x38` / `field_0x3C` pair is written by two `strd` sites:

```asm
; "clear / no-route" sentinel — written on stop / reset
0x00bd0a:  movs r0,#0x24          ; 36
0x00bd0c:  movs r1,#0x26          ; 38
0x00bd0e:  cmp.w r8,#0xf          ; r8 = type
0x00bd12:  ite  hi
0x00bd14:    strdhi r1,r0,[sl,#0x44]   ; type >= 0x10 → slot 0x44 / 0x48
0x00bd18:    strdls r1,r0,[sl,#0x38]   ; type <  0x10 → slot 0x38 / 0x3C = (38,36)

; "install route" — writes the REAL mode
0x00bd0e… path reaching 0xbee6:
0x00beea:  strd r6,r8,[sl,#0x38]       ; proxy->0x38 = arg1 (mode),
                                       ; proxy->0x3C = arg2 (type)
```

Preceded by the transition-check at 0xbe1c:

```asm
0x00be1c:  ldr.w r1,[sl,#0x38]
0x00be20:  cmp  r1,#0x26               ; cleared sentinel?
0x00be24:  ldrne.w r2,[sl,#0x3c]
0x00be28:  cmpne r2,#0x24
0x00be2a:  bne  0xbeda                  ; not cleared → transition path (→ bl 0xbf68)
0x00be2c:  mov  r0,sl ; r1=r6 ; r2=r8 ; r3=r7
0x00be34:  bl   0xc24c                  ; install_route helper
0x00be38:  b    0xbee6                  ; → strd at 0xbeea
```

**So: `proxy->field_0x38 = arg1` of `proxy_set_route`**. The gate in
`proxy_open_capture_stream` (range [17..23]) is therefore satisfied iff the
caller passes `mode ∈ [17..23]` — which is precisely the Samsung proxy_mode
computed by audio.primary at 0x089b4.

### `proxy_set_audiomode` (vaddr 0xd66c, 228 B) — NOT the gate writer

```asm
0x00d670:  mov  r4,r1                   ; r4 = arg1 = Android audio_mode
0x00d672:  movw r1,#0x1f8c
0x00d676:  movt r1,#1                   ; r1 = 0x11f8c
0x00d67a:  mov  r5,r0                   ; r5 = arg0 = proxy
0x00d67c:  ldr  r0,[r0,r1]              ; r0 = proxy->[0x11f8c] (old mode)
0x00d67e:  add.w r8,r5,r1               ; r8 = &proxy->[0x11f8c]
…
0x00d6de:  str.w r4,[r8]                ; proxy->[0x11f8c] = new Android mode
```

So `proxy_set_audiomode` stores the **raw Android `audio_mode_t`** into a
different field deep inside the giant proxy struct (`.data` range 0x11c84–0x123f4
matches — `proxy` is actually based at the library image; see note below). This
field is later READ by audio.primary's proxy_mode_compute (via
`hw_dev->field_0x114`) to decide what Samsung proxy_mode to pass to
`proxy_set_route`. Patching this path alone does not arm the gate.

Note on the `proxy[0x11f8c]` pattern: this is actually PC-relative access to a
.data global (`global_proxy` pointer slot at ~0x11f8c in .data), not a field on
the argument. The first arg is effectively used as a zero base for that specific
access. Semantically it is "save current Android mode to the global singleton".

### Event chain during `set_mode`

1. Framework → `adev_set_mode(dev, MODE_IN_COMMUNICATION=3)`.
2. audio.primary sets `hw_dev->field_0x114 = 3` and calls
   `proxy_set_audiomode(global_proxy, 3)`.
3. audio.primary calls **0x089b4** (proxy_mode_compute) → returns **22**.
4. audio.primary calls `proxy_set_route(global_proxy, 22, type, delta)` →
   sets `global_proxy->field_0x38 = 22`.
5. Later, `proxy_open_capture_stream` checks `field_0x38 ∈ [17..23]` → passes.
6. TBB selects `AUSAGE=110` → `pcm_open(card=0, device=110)` → `calliope_10`.

The mic is silent because step 6 opens the modem uplink, not the real mic.

## Candidate fixes — evolution and final solution

Ordered from smallest / most targeted to most invasive. Each entry includes what
we tried and why it did or did not work.

### C — Patch `audio.primary.universal3830.so` alternate path

The alternate path at `0x089e4` returns **37** (`0x25`) for `MODE_IN_COMMUNICATION`.
A single byte changes that return to **22** (`0x16`), which is inside `[17..23]`:

```asm
0x08a9a:  movs r0, #0x25    ; returns 37 → gate FAILS
          ↓
0x08a9a:  movs r0, #0x16    ; returns 22 → gate PASSES
```

**Why it did NOT fix the mic:**
Controlled `audio_diag.sh` output shows `calliope_10` is RUNNING while ALL WDMA paths
are closed, both with and without Patch C. The gate passing/failing only affects
mixer arming; it does **not** change the AUSAGE/device selection in `proxy_create_capture_stream`.
Patch C does not touch the device selection, so AUSAGE remains 110 and the modem uplink
still opens.

See `scripts/patch_audio_primary_targeted.py`.

### A — Patch `libaudioproxy.so` to remove the capture gate (rejected)

In `proxy_open_capture_stream`:

```asm
0x00aa40:  ldr  r0,[r5,#0x38]
0x00aa42:  subs r0,#0x11
0x00aa44:  cmp  r0,#6
0x00aa46:  bhi  0xaae0               ; <-- 16-bit branch (2 B)
```

Overwrite the `bhi` at 0xaa46 with `BF00` (NOP16) — 2 bytes.

**Why it was REJECTED:**
Making the gate always fall through forces **all** non-cellular captures through
the mixer-arming path. After a call ends, `proxy_set_route` writes the clear sentinel,
but the gate no longer enforces it. This breaks Voice Recorder and video capture:
the mixer stays armed with stale post-call state and normal audio is interpreted as
call audio (routed to the earpiece speaker instead of the main speaker).

### B — Patch `audio.primary.universal3830.so` at 0x089b4 (broad, not recommended)

Force proxy_mode_compute to always return 20 for `field_0x114 ∈ {2, 3}`.

**Why it was REJECTED:**
This forces **all** Android modes (including `MODE_NORMAL`) into the
`MODE_IN_COMMUNICATION` handler, so the gate sees 20 even after a call ends.
Same breakage as Patch A — Voice Recorder and video capture are broken because
the mixer is permanently armed.

### D — Native helper calling the exported `proxy_set_route`

Because `proxy_set_route` is an exported dynsym, a tiny JNI helper could force
`field_0x38 = 20` before `AudioRecord.startRecording()`.

**Why it was NOT pursued:**
No binary patching is needed, but the side effects of `proxy_set_route`
(`audio_route_apply_path`, mixer writes in internal helpers) clash with the HAL's
own state machine. It is a runtime workaround, not a clean fix.

### F — Patch `libaudioproxy.so` AUSAGE at source (correct targeted fix)

`proxy_create_capture_stream` is where AUSAGE is actually set for standard (48 kHz)
capture. `proxy_open_capture_stream` has a secondary gate (`beq 0xaae0` at `0xaa50`)
that skips its TBB for 48 kHz captures — which is always true. Patching the TBB in
`proxy_open_capture_stream` is therefore **wrong** because the TBB is dead code.

**The correct patch is in `proxy_create_capture_stream` at vaddr `0xa30e`**
(inner TBH6 target for `stream_type=11`).

#### F v1 — unconditional AUSAGE=12 (rejected)

A single-byte change at `0xa30e`:

```asm
0x00a30e:  movs r6, #0x6e    ; AUSAGE = 110 → pcm110c (calliope_10, modem uplink)
          ↓
0x00a30e:  movs r6, #0x0c    ; AUSAGE = 12 → pcm12c (WDMA0, real mic)
```

**Why it was REJECTED:**
This changes AUSAGE for **all** AudioSources with `stream_type=11`:
`MIC`, `CAMCORDER`, `VOICE_RECOGNITION`, and `VOICE_COMMUNICATION`.
During testing, video recording (`CAMCORDER`) broke because it also opened `pcm12c`
instead of `pcm110c`. The Samsung DSP apparently does not feed mic audio to WDMA0
for `CAMCORDER` use, or the mixer configuration expected by Samsung for video
recording is only valid on the modem path.

#### F v2 — conditional hook (FINAL FIX)

Instead of changing the single byte unconditionally, we replace the `movs r6, #110`
+ `b.n` sequence at `0xa30e` with a **32-bit branch to a conditional hook** at
`0xbae4` (unused NOP padding after `proxy_init_route`). The hook checks
`ausage_param` (stored at `[r8, #4]`) and sets AUSAGE accordingly:

```asm
hook at 0xbae4:
    ldr.w r0, [r8, #4]     ; r0 = ausage_param
    cmp r0, #1             ; MIC / VOICE_COMMUNICATION?
    ite eq
    moveq r6, #12          ; AUSAGE = 12 → pcm12c (real mic)
    movne r6, #110         ; AUSAGE = 110 → pcm110c (stock)
    b.w 0xa37a             ; jump to epilogue
```

**File offsets:**
- Branch: `0x930e` (vaddr `0xa30e`) — `b.w 0xbae4` (4 bytes)
- Hook: `0xaae4` (vaddr `0xbae4`) — 16 bytes of hook code

**Why this is the correct fix:**
- `ausage_param == 1` (`MIC`, `VOICE_COMMUNICATION`) → `AUSAGE = 12` → `pcm12c` → real mic audio during SIP calls.
- `ausage_param == 2` (`CAMCORDER`) → `AUSAGE = 110` → `pcm110c` → stock Samsung path, video recording works.
- `ausage_param == 27` (`VOICE_RECOGNITION`) → `AUSAGE = 110` → stock path.
- Only `stream_type=11` is affected. `stream_type=12` (voice-call path) and playback are untouched.
- The primary gate (`field_0x38 ∈ [17..23]`) still functions normally.
- Does not change pcm_config (remains `pcm_config_primary_capture`, 48 kHz, 2 ch).

**Why NOT patch `proxy_open_capture_stream`:**
Nop'ing the secondary gate (the `beq 0xaae0` at `0xaa50`) forces ALL 48 kHz captures
through the TBB + local helper path. The local helper at `0xa6f0` is part of
`proxy_create_capture_stream`'s epilogue and performs cleanup (`free`, field zeroing).
Calling it from `proxy_open_capture_stream` corrupts stream state and breaks all
capture (Voice Recorder, incoming call audio, etc.).

See `scripts/patch_ausage_stream_type_11.py`.

## Open questions — all resolved

1. ~~What sets `aproxy->field_0x5`?~~ — Deprioritized. The root cause is device
   selection (AUSAGE), not the gate.
2. ~~**What does the pcm_config pointer for stream_type=11 resolve to?**~~ —
   **RESOLVED.** `pcm_config_primary_capture` (48 kHz, 2 ch).
3. ~~**Are ALSA mixer controls for WDMA0 actually armed during a SIP call?**~~ —
   **ANSWERED.** `audio_diag.sh` shows the mixer IS armed; the issue is that
   `calliope_10` opens instead of `WDMA0`.
4. ~~**Where is the capture device number set?**~~ — **RESOLVED.**
   `proxy_create_capture_stream` writes `AUSAGE=110` to `stream+12` via
   `[r8, #12]` at epilogue sites. `proxy_open_capture_stream` writes `card=0`
   at `0xaa56`. The secondary gate at `0xaa50` skips the TBB, so the AUSAGE
   from stage 1 reaches `ldrd r6, r8, [r4, #8]` at `0xab0c` and `pcm_open`.
5. ~~**Is Patch F safe for non-call capture?**~~ — **RESOLVED.** The original
   unconditional Patch F (AUSAGE=12 for all `stream_type=11`) **breaks** video
   recording because `CAMCORDER` also maps to `stream_type=11` and the Samsung
   DSP expects `pcm110c` for that use case. The final fix (Patch F v2, the
   conditional hook) only changes AUSAGE for `ausage_param==1` (`MIC` /
   `VOICE_COMMUNICATION`), leaving `CAMCORDER` and `VOICE_RECOGNITION` on the
   stock `pcm110c` path. Verified on device: SIP calls have mic audio, video
   recording works, and Voice Recorder works.
6. ~~**Does `proxy_open_playback_stream` have a similar gate?**~~ — **ANSWERED.**
   The `proxy_mode` gate is specific to capture (`proxy_open_capture_stream`).
   Playback paths are unaffected by any of the patches above.
