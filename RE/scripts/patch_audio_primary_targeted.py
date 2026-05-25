#!/usr/bin/env python3
"""
Patch C — audio.primary.universal3830.so: single-byte targeted fix for
MODE_IN_COMMUNICATION on the alternate proxy_mode_compute path.

Problem
-------
With the stock Samsung HAL, proxy_mode_compute has an alternate code path
(at vaddr 0x089e4) that activates when an internal HAL pointer is set.
On that path MODE_IN_COMMUNICATION returns 0x25 = 37, which is outside the
[17..23] range-gate in libaudioproxy.so::proxy_open_capture_stream.
The result: the ALSA mic mixer path is never armed and the captured audio
is silent during SIP/IMS calls.

Fix
---
vaddr 0x08a9a  fileoff 0x07a9a  (1 byte)
  Original:  movs r0, #0x25   ; returns 37, gate FAILS
  Patched:   movs r0, #0x14   ; returns 20, gate PASSES

This is the ONLY return value in [17..23] on the alternate path, so it
causes proxy_open_capture_stream to arm the mic mixer path while keeping
all other modes (MODE_NORMAL, MODE_IN_CALL) untouched.

Post-call safety
----------------
after a call ends audio.primary calls proxy_set_route with a clear
sentinel that sets field_0x38 = 38 (outside [17..23]).  The gate in
libaudioproxy is still intact, so normal capture falls through to the
non-call path once the call is over.

Usage:
    python3 scripts/patch_audio_primary_targeted.py [--apply]

Without --apply the script only verifies the expected byte and exits.
"""

import shutil, sys
from pathlib import Path

BINARY  = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830.so"
PATCHED = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830_targeted.so"

PATCH = {
    "name":     "alt_path_return_22",
    "offset":   0x07A9A,
    "expected": bytes([0x25]),   # movs r0, #0x25
    "new":      bytes([0x16]),   # movs r0, #0x16
    "desc":     "0x08a9a: movs r0,#0x25 (returns 37) → movs r0,#0x16 (returns 22);"
                " 22 is the natural return of the main-path MODE_IN_COMMUNICATION handler"
                " (null aproxy, field_0x108==0) and maps to WDMA0 instead of calliope_10",
}


def verify(data: bytes) -> None:
    found = data[PATCH["offset"]:PATCH["offset"]+len(PATCH["expected"])]
    if found != PATCH["expected"]:
        sys.exit(
            f"[ERR] {PATCH['name']}: unexpected byte at 0x{PATCH['offset']:x}: "
            f"got {found.hex()} expected {PATCH['expected'].hex()}\n"
            "Binary may already be patched or does not match the expected version."
        )
    print(f"[OK] Verified {PATCH['name']}: {PATCH['desc']}")


def apply_patch(data: bytearray) -> None:
    data[PATCH["offset"]:PATCH["offset"]+len(PATCH["new"])] = PATCH["new"]
    print(f"[OK] Applied  {PATCH['name']}: wrote {PATCH['new'].hex()} at 0x{PATCH['offset']:x}")


def main() -> None:
    apply = "--apply" in sys.argv

    raw = BINARY.read_bytes()
    verify(raw)

    if not apply:
        print("Dry-run complete.  Pass --apply to write the patched binary.")
        return

    buf = bytearray(raw)
    apply_patch(buf)

    PATCHED.write_bytes(buf)
    print(f"[OK] Written:  {PATCHED}")
    print()
    print("Next steps:")
    print("  adb root && adb remount")
    print(f"  adb push {PATCHED} /vendor/lib/hw/audio.primary.universal3830.so")
    print("  adb shell restorecon /vendor/lib/hw/audio.primary.universal3830.so")
    print("  adb reboot")


if __name__ == "__main__":
    main()
