#!/usr/bin/env python3
"""
Patch B — audio.primary.universal3830.so: force proxy_mode_compute to always
return 20 (0x14) when Android audio mode is IN_CALL (2) or IN_COMMUNICATION (3).

Two edits inside proxy_mode_compute (vaddr 0x089b4):

  PATCH 1  vaddr 0x089ce  fileoff 0x079ce  (2 bytes)
    bne 0x08a5c  →  b 0x08a1e
    45 D1        →  26 E0
    Folds MODE_IN_CALL into the MODE_IN_COMMUNICATION branch.

  PATCH 2  vaddr 0x08a1e  fileoff 0x07a1e  (4 bytes)
    ldr.w r1,[r0,#0xf4]  →  movs r0,#0x14 ; pop {r4,pc}
    D0 F8 F4 10          →  14 20 10 BD
    Unconditional early return of 20 instead of inspecting aproxy sub-flags
    that are all zero for software IMS/SIP calls.

Usage:
    python3 scripts/patch_audio_primary.py [--apply]

Without --apply the script only verifies the expected bytes and exits.
"""

import shutil, sys
from pathlib import Path

BINARY  = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830.so"
PATCHED = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830_patched.so"

PATCHES = [
    {
        "name":     "patch1_bne_to_b",
        "offset":   0x079CE,
        "expected": bytes([0x45, 0xD1]),   # bne 0x08a5c
        "new":      bytes([0x26, 0xE0]),   # b   0x08a1e
        "desc":     "0x089ce: bne 0x08a5c → b 0x08a1e (fold IN_CALL into IN_COMM path)",
    },
    {
        "name":     "patch2_early_return_20",
        "offset":   0x07A1E,
        "expected": bytes([0xD0, 0xF8, 0xF4, 0x10]),  # ldr.w r1,[r0,#0xf4]
        "new":      bytes([0x14, 0x20, 0x10, 0xBD]),  # movs r0,#0x14 ; pop {r4,pc}
        "desc":     "0x08a1e: ldr.w r1,[r0,#0xf4] → movs r0,#0x14; pop {r4,pc} (return 20 unconditionally)",
    },
]


def verify(data: bytes) -> None:
    for p in PATCHES:
        found = data[p["offset"] : p["offset"] + len(p["expected"])]
        if found != p["expected"]:
            sys.exit(
                f"[ERR] {p['name']}: unexpected bytes at 0x{p['offset']:x}: "
                f"got {found.hex()} expected {p['expected'].hex()}\n"
                "Binary may already be patched or does not match the expected version."
            )
        print(f"[OK] Verified {p['name']}: {p['desc']}")


def apply_patches(data: bytearray) -> None:
    for p in PATCHES:
        data[p["offset"] : p["offset"] + len(p["new"])] = p["new"]
        print(f"[OK] Applied  {p['name']}: wrote {p['new'].hex()} at 0x{p['offset']:x}")


def main() -> None:
    apply = "--apply" in sys.argv

    raw = BINARY.read_bytes()
    verify(raw)

    if not apply:
        print("Dry-run complete.  Pass --apply to write the patched binary.")
        return

    buf = bytearray(raw)
    apply_patches(buf)

    PATCHED.write_bytes(buf)
    print(f"[OK] Written:  {PATCHED}")
    print()
    print("Next steps:")
    print("  adb root && adb remount")
    print(f"  adb push {PATCHED} /vendor/lib/hw/audio.primary.universal3830.so")
    print("  adb reboot")


if __name__ == "__main__":
    main()
