# Samsung A21s IMS Audio Fix — Exynos3830 Audio HAL

Goal: fix `AudioRecord` with `VOICE_COMMUNICATION` source producing silence
during SIP/VoLTE calls on Samsung A21s (SM-A217F, Exynos 850).

**The fix:** modify `mixer_paths.xml` to replace `route-apcall-mic` with
`route-ap-record` in the `communication-*-mic` paths. No binary patching needed.

**Why the RE was necessary:** the root cause is not obvious from the mixer paths
alone. It required understanding that the ABox DSP firmware interprets mixer
configurations as routing commands, and `route-apcall-mic` tells the DSP to send
mic audio to the modem path — which is a dead end during software SIP calls.

## Binaries

| File | Source on device | Size | Note |
|------|-----------------|------|------|
| `binaries/libaudioproxy.so` | `/vendor/lib/libaudioproxy.so` | ~64 KB | Samsung proxy layer |
| `binaries/audio.primary.universal3830.so` | `/vendor/lib/hw/audio.primary.universal3830.so` | ~68 KB | Android Audio HAL |

These were RE'd to understand the audio pipeline. The fix does not require
patching either binary.

Refresh stock files from a connected device: `bash scripts/pull_binaries.sh`

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
| pcm12c | WDMA0 | Real microphone via Abox DSP — 48 kHz |
| pcm13c–pcm15c | WDMA1–3 | Additional real capture paths |
| pcm16c | WDMA4 | Primary capture path used by Samsung's `media-mic` / `communication-handset-mic` |
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

**Updated finding (2026-05-27):** The mixer path, not AUSAGE, is the root cause.
Stock AUSAGE=110 opens pcm110c correctly. During SIP calls, `communication-handset-mic`
applies `route-apcall-mic` which tells the ABox DSP to route mic audio to VSS_TXADAPTER
(modem path). With no modem call, pcm110c gets zeros. Replacing `route-apcall-mic` with
`route-ap-record` (the normal capture path) in `communication-handset-mic` makes pcm110c
produce real mic audio (315,120 frames confirmed during a SIP call). See "Test findings"
below.

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
| `scripts/patch_ausage_stream_type_11.py` | Apply conditional hook (Patch F v2) to `libaudioproxy.so` — **deprecated**, Fix G is correct |
| `scripts/patch_mixer_paths.py` | **Apply the correct fix:** patch `mixer_paths.xml` to replace `route-apcall-mic` with `route-ap-record` in communication capture paths |

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

## Tools built

- `/data/local/tmp/tinycap` — direct ALSA capture (bypasses Android AudioFlinger)
- `/data/local/tmp/tinymix` — ALSA mixer control inspection

Both compiled from AOSP tinyalsa using `aarch64-linux-gnu-gcc -static -D__unused=`.

## Test scripts

| Script | Purpose |
|--------|---------|
| `test_ims_patch_f_v2.sh` | Comprehensive IMS audio test: push patched binary, reboot, dial voicemail, collect logs, restore stock |
| `test_ausage16_mixer_mod.sh` | Patch AUSAGE=16 + modify `mixer_paths.xml` on device to replace `route-apcall-mic` with `route-ap-record` in `communication-handset-mic` |
| `test_mixer_only.sh` | **Confirmed working fix:** modify `mixer_paths.xml` only (no libaudioproxy patch), reboot, dial voicemail, collect logs, restore stock |

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

### G — Modify `mixer_paths.xml` (most promising approach)

**Hypothesis:** the root cause is the mixer path, not the ALSA device number.

During a SIP call, the HAL applies `communication-handset-mic` which contains
`route-apcall-mic`. This path routes mic audio through VSS_TXADAPTER → TXSE
(modem path), potentially causing the ABox DSP to stop feeding mic audio to
calliope_10. Result: pcm110c returns zeros.

**Proposed fix:** replace `route-apcall-mic` with `route-ap-record` (the normal
capture path) in `communication-handset-mic`, `communication-speaker-mic`, and
`communication-headset-mic`.

- `route-apcall-mic`: UAIF0 → NSRC4 → SIFM4 → WDMA4 → VSS_TXADAPTER → TXSE → vpcmindai0
- `route-ap-record`: UAIF0 → NSRC4 → SIFM4 → WDMA4 → VPCMIN_DAI0/2

The difference is the VSS adapter/TXSE routing. `route-apcall-mic` may tell the
ABox DSP "this is a modem call" and the DSP routes mic audio accordingly.
For SIP calls with no modem, that audio may go nowhere.

**Initial tests (2026-05-27, two runs):**
- Stock `libaudioproxy.so` (AUSAGE=110, pcm110c)
- Modified `mixer_paths.xml`: `communication-handset-mic` uses `route-ap-record`
- During SIP call: pcm110c showed `hw_ptr: 315120` and `hw_ptr: 646800` —
  real mic audio flowing in both runs.
- No `pcm_read` errors, no `Read Fail`
- Mic PGAs powered up correctly and stayed up
- Device booted normally with modified mixer_paths.xml, no audio crashes

**Needs further validation:** normal capture, video recording, VoLTE calls,
speaker-mic input path.

**Why this might be safe for cellular calls:**
Cellular/VoLTE calls use separate `incall-*` paths (`route-cp-tx`, `route-cp-callrec`).
The `communication-*` paths are only for AP (software) calls. Modifying them does
not affect the modem audio path.

See `RE/scripts/patch_mixer_paths.py`.

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

### F — Patch `libaudioproxy.so` AUSAGE at source (DEPRECATED — non-viable)

**DEPRECATED.** Test data from May 27 confirms WDMA4 (pcm16c) never produces
data for direct ALSA capture on this device (`hw_ptr=0` even in stock normal mode).
The correct fix is mixer_paths.xml modification (Fix G), not AUSAGE patching.

The following is kept for historical context:

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
    moveq r6, #16          ; AUSAGE = 16 → pcm16c (WDMA4, real mic)
    movne r6, #110         ; AUSAGE = 110 → pcm110c (stock)
    b.w 0xa37a             ; jump to epilogue
```

**File offsets:**
- Branch: `0x930e` (vaddr `0xa30e`) — `b.w 0xbae4` (4 bytes)
- Hook: `0xaae4` (vaddr `0xbae4`) — 16 bytes of hook code

**Why this is the correct fix:**
- `ausage_param == 1` (`MIC`, `VOICE_COMMUNICATION`) → `AUSAGE = 16` → `pcm16c` (WDMA4, real mic).
- `ausage_param == 2` (`CAMCORDER`) → `AUSAGE = 110` → `pcm110c` → stock Samsung path, video recording works.
- `ausage_param == 27` (`VOICE_RECOGNITION`) → `AUSAGE = 110` → stock path.
- Only `stream_type=11` is affected. `stream_type=12` (voice-call path) and playback are untouched.
- The primary gate (`field_0x38 ∈ [17..23]`) still functions normally.
- Does not change pcm_config (remains `pcm_config_primary_capture`, 48 kHz, 2 ch).

**Caveat (updated 2026-05-27):** Test data from May 26 confirms AUSAGE=16 opens
WDMA4 and DAPM correctly powers up NSRC4/WDMA4. The May 27 test with
`route-ap-record` kept the mic PGAs up, but **pcm16c still produced zero frames**
(`hw_ptr = 0`). Kernel inspection shows pcm16c is already held open by the audio
server in stock normal mode with `hw_ptr = 0` — WDMA4 is not a usable direct
ALSA capture path on this device. See "Test findings" below for the full log
analysis. Patch F v2 is therefore non-viable.

**Why NOT patch `proxy_open_capture_stream`:**
Nop'ing the secondary gate (the `beq 0xaae0` at `0xaa50`) forces ALL 48 kHz captures
through the TBB + local helper path. The local helper at `0xa6f0` is part of
`proxy_create_capture_stream`'s epilogue and performs cleanup (`free`, field zeroing).
Calling it from `proxy_open_capture_stream` corrupts stream state and breaks all
capture (Voice Recorder, incoming call audio, etc.).

See `scripts/patch_ausage_stream_type_11.py`.

## `mixer_paths.xml` analysis

Pulled from `/vendor/etc/mixer_paths.xml` on the device. Key finding: **Samsung's
standard microphone capture paths route to WDMA4, not WDMA0.**

Standard capture paths:

| Path | WDMA target | Used by |
|------|-------------|---------|
| `route-ap-record` → `route-nsrc4-to-wdma4` | **WDMA4** | `media-mic`, `recording-mic`, `media-headset-mic` |
| `route-apcall-mic` → `route-nsrc4-to-wdma4` | **WDMA4** | `communication-handset-mic`, `communication-speaker-mic`, `communication-headset-mic` |
| `route-nsrc0-to-wdma0` | WDMA0 | `media-speaker-headset`, `media-speaker-bt-sco-headset`, `call_forwarding_primary` |

**WDMA0 is NOT used in any standalone microphone capture path.** It only appears
in combo output+capture paths and call forwarding.

This means the `media-mic` mixer path that the HAL applies during normal
`AudioRecord` capture configures **WDMA4 source selectors**, not WDMA0.
However, the stock HAL opens **pcm110c** (calliope_10), not WDMA4. This
suggests the stock HAL does not rely on the `mixer_paths.xml` WDMA4 paths for
normal capture; instead, mic audio reaches pcm110c through ABox DSP internal
routing that is independent of the ALSA mixer controls.

During a SIP call (`MODE_IN_COMMUNICATION`), `adev_set_route` fires for both
output (`primary_out-adev_set_route-2`) and input
(`primary_in-adev_set_route-3: routes to device(handset-mic) for
usage(communication)`). The `communication-handset-mic` path is therefore
applied.

## Open questions

1. ~~What sets `aproxy->field_0x5`?~~ — Deprioritized. The root cause is device
   selection (AUSAGE), not the gate.
2. ~~**What does the pcm_config pointer for stream_type=11 resolve to?**~~ —
   **RESOLVED.** `pcm_config_primary_capture` (48 kHz, 2 ch).
3. ~~**Are ALSA mixer controls for WDMA0 actually armed during a SIP call?**~~ —
   **ANSWERED.** `mixer_paths.xml` shows the standard mic paths route to **WDMA4**,
   not WDMA0. Neither path is armed during SIP calls because `primary_in`
   `adev_set_route` never fires for AP calls.
4. ~~**Where is the capture device number set?**~~ — **RESOLVED.**
   `proxy_create_capture_stream` writes `AUSAGE=110` to `stream+12` via
   `[r8, #12]` at epilogue sites. `proxy_open_capture_stream` writes `card=0`
   at `0xaa56`. The secondary gate at `0xaa50` skips the TBB, so the AUSAGE
   from stage 1 reaches `ldrd r6, r8, [r4, #8]` at `0xab0c` and `pcm_open`.
5. ~~**Is Patch F safe for non-call capture?**~~ — Patch F is **deprecated**.
   The correct fix is mixer_paths.xml modification (Fix G).
6. ~~**Does `proxy_open_playback_stream` have a similar gate?**~~ — **ANSWERED.**
   The `proxy_mode` gate is specific to capture (`proxy_open_capture_stream`).
   Playback paths are unaffected by any of the patches above.
7. ~~**Why does `primary_in-adev_set_route` not fire during AP calls?**~~ —
   **UPDATED.** It DOES fire — `primary_in-adev_set_route-3: routes to device(handset-mic)`
   appears during SIP calls (visible in May 26 AUSAGE=16 logs). The earlier
   assumption that it "never appears" was wrong; it was obscured by the
   rapid route/unroute loop caused by `pcm_read` failures.
8. ~~**Why do the mic PGAs power down before capture starts during AP calls?**~~ —
   **RESOLVED.** The `route-apcall-mic` path includes VSS adapter/TXSE routing
   which confuses DAPM, causing premature power-down of the analog front-end.
   With `route-ap-record`, PGAs stay up correctly.
9. **Why does `route-apcall-mic` cause pcm110c to return zeros during SIP calls?**
   — Hypothesis: the `route-apcall-mic` path routes mic audio to VSS_TXADAPTER → TXSE
   (modem path). The ABox DSP may interpret this as "route mic to modem", so for
   SIP calls (no modem), the audio goes nowhere and pcm110c gets zeros. With
   `route-ap-record`, the DSP routes mic audio to calliope_10 normally.
   **Promising result on device (May 27):** two independent SIP calls showed
   pcm110c producing real frames (315,120 and 646,800). Needs further validation
   with different apps and longer call durations.
10. **Does Fix G break anything else?** — Partially tested. Device boots normally,
    no audio crashes. Normal `AudioRecord` (MIC source) not directly tested, but
    `media-mic` path is unchanged. Video recording (CAMCORDER) not tested.
    `communication-speaker-mic` not directly triggered yet. Actual VoLTE call
    not tested (no SIM). Theoretically safe because `incall-*` paths (cellular
    calls) are separate from `communication-*` paths (AP calls).

## Test findings (2026-05-26 / 2026-05-27)

**Stock baseline (no patches):**
- Normal capture (`MIC` or `VOICE_COMMUNICATION` source) opens **pcm110c**
  (`calliope_10`) and produces **real mic audio** via an ABox DSP loopback.
- During a SIP call (`MODE_IN_COMMUNICATION`), pcm110c opens but returns
  **all zeros** — the ABox DSP stops feeding mic audio to the modem uplink path.
- Direct ALSA capture on WDMA0 (`tinycap` on pcm12c) returns **0 frames**
  because WDMA0 source selectors are "None".

**Patch F v2 test (AUSAGE=16, then incorrectly changed to 12):**

*May 26 — AUSAGE=16 (WDMA4):*
- ALSA opened **pcm16c** (WDMA4). The mixer path `communication-handset-mic`
  was applied. DAPM correctly powered up **WDMA4 Capture** and **NSRC4** with
  **no kernel errors**.
- However, the **mic analog front-end (PGAs) powered down ~50 ms before**
  capture started. The `aud3004x` codec logs show `mic1_pga_ev event=1` (UP)
  immediately followed by `event=2` (DOWN) at 49.146–49.149s, while
  `proxy_start_capture_stream` only fires at 49.198s.
- When `pcm_read` finally runs, the mic PGAs are already down → `pcm_read`
  returns `-1` (ENODATA).

*May 27 — AUSAGE=12 (WDMA0):*
- ALSA opened **pcm12c** (WDMA0) but the mixer path still configures **WDMA4**
  (via `route-nsrc4-to-wdma4`). The kernel immediately reports:
  `NSRC0, 1: invalid source dai:0x0`.
- This confirms AUSAGE=12 is the **wrong device** — Samsung's mic paths route
  to WDMA4, not WDMA0.

**Confirmed:** the hook should use **AUSAGE=16** (WDMA4), not 12.
The May 26 test showed WDMA4/NSRC4 powered up correctly, but mic PGAs powered
down before capture started. The May 27 test confirmed AUSAGE=12 produces
`invalid source dai` kernel errors.

*May 27 — AUSAGE=16 + `route-ap-record` (replaces `route-apcall-mic` in `communication-handset-mic`):*
- Replaced `route-apcall-mic` with `route-ap-record` in `communication-handset-mic`
  via `RE/scripts/modify_mixer_paths.py`. The `route-ap-record` path is simpler:
  it routes UAIF0 → NSRC4 → SIFM4 → WDMA4 → VPCMIN_DAI0/2 and does **not**
  include the VSS adapter / TXSE routing present in `route-apcall-mic`.
- Result: **mic PGAs stayed UP** — no premature power-down. DAPM powered up
  WDMA4 Capture and NSRC4 correctly with no kernel errors.
- However, **pcm16c showed `hw_ptr = 0` and `appl_ptr = 0` throughout the call**.
  No frames were transferred. `pcm_read` returned `-1` (I/O error) after ~1 s.

*Critical kernel finding — WDMA4 never produces data for direct ALSA capture:*
- In **stock normal mode** (no patches, no call, stock `mixer_paths.xml`), the
  Android audio server already holds **pcm16c open** with `state: RUNNING`.
  Its `hw_ptr` and `appl_ptr` are both **0** — zero frames transferred.
- This confirms that on the A21s Exynos850 firmware, **WDMA4 is not a usable
  direct ALSA capture path**. The ABox DSP firmware does not feed WDMA4 for
  CPU-side readout. The stock HAL's normal capture uses pcm110c (calliope),
  which receives mic audio via internal DSP routing independent of WDMA4.
- Therefore, **forcing `AUSAGE=16` to open pcm16c cannot work**: the ALSA device
  opens and the mixer configures correctly, but the DSP simply never writes
  capture data into WDMA4's ring buffer.

*May 27 — mixer_paths.xml ONLY test (stock libaudioproxy, modified `communication-handset-mic`):*
- **No libaudioproxy patch.** Stock AUSAGE=110 → pcm110c (calliope_10).
- Replaced `route-apcall-mic` with `route-ap-record` in `communication-handset-mic`.
  Removed `set-call-wdma4-16bit-config`.
- During SIP call, pcm110c showed **`hw_ptr: 315120`** — real mic audio flowing.
- No `pcm_read` errors, no `Read Fail`. Capture transitioned to "Capturing"
  successfully and stayed active for ~7 seconds.

*May 27 — second test with permanent modified mixer_paths.xml (rebooted):*
- Handset mic SIP call: pcm110c showed **`hw_ptr: 646800`** — repeatable result.
- Speaker toggle test: pcm110c showed **`hw_ptr: 1217040`** over ~25 s.
  No `pcm_read` errors. Device booted normally with modified mixer_paths.xml.
  No audio-related crashes in system log.
- Speakerphone toggle via adb did **not** switch input path to
  `communication-speaker-mic`; it remained on `communication-handset-mic`.
  The modified `communication-speaker-mic` path has therefore not been
  directly exercised.

**Conclusion (tentative):** mixer_paths.xml modification (Fix G) may be the
better approach than libaudioproxy AUSAGE patching (Patch F v2), but needs
further validation before calling it the definitive fix. Remaining gaps:
normal `AudioRecord` (MIC source), video recording (CAMCORDER), actual
VoLTE call, `communication-speaker-mic` input path.
