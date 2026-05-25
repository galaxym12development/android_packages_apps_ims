#!/usr/bin/env python3
"""
Restore libaudioproxy.so from the .orig backup (reverting Patch A).

Usage:
    python3 scripts/restore_libaudioproxy.py [--apply]

Without --apply only verifies the backup exists and shows what would happen.
"""

import shutil, sys
from pathlib import Path

BINARY = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"
ORIG   = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so.orig"

PATCHED_BYTE_AT_9A46 = bytes([0x00, 0xBF])  # NOP16 written by Patch A
ORIG_BYTE_AT_9A46    = bytes([0x4B, 0xD8])  # original bhi 0xaae0


def main() -> None:
    apply = "--apply" in sys.argv

    if not ORIG.exists():
        sys.exit(f"[ERR] Backup not found: {ORIG}\nRun pull_binaries.sh to get a fresh copy.")

    orig_data = ORIG.read_bytes()
    found = orig_data[0x9A46 : 0x9A46 + 2]
    if found != ORIG_BYTE_AT_9A46:
        sys.exit(
            f"[ERR] .orig file has unexpected bytes at 0x9a46: got {found.hex()}\n"
            "The backup itself may be patched — pull a fresh binary from the device."
        )
    print(f"[OK] .orig verified: bytes at 0x9a46 = {found.hex()} (original bhi 0xaae0)")

    cur_data = BINARY.read_bytes()
    cur = cur_data[0x9A46 : 0x9A46 + 2]
    if cur == ORIG_BYTE_AT_9A46:
        print("[OK] libaudioproxy.so already matches the original — nothing to do.")
        return
    if cur == PATCHED_BYTE_AT_9A46:
        print(f"[OK] Patch A detected at 0x9a46 ({cur.hex()} = NOP16), will be reverted.")
    else:
        print(f"[WARN] Unexpected bytes at 0x9a46: {cur.hex()} — restoring anyway.")

    if not apply:
        print("Dry-run complete.  Pass --apply to overwrite libaudioproxy.so with the backup.")
        return

    shutil.copy2(ORIG, BINARY)
    print(f"[OK] Restored: {BINARY}")
    print()
    print("Next steps:")
    print("  adb root && adb remount")
    print(f"  adb push {BINARY} /vendor/lib/libaudioproxy.so")
    print("  adb reboot")


if __name__ == "__main__":
    main()
