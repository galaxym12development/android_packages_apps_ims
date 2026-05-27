#!/usr/bin/env python3
"""
patch_ausage_stream_type_11.py — Patch proxy_create_capture_stream to route
VOICE_COMMUNICATION capture from modem uplink (calliope_10, pcm110c) to a real
microphone WDMA path, WITHOUT affecting CAMCORDER or VOICE_RECOGNITION.

Root cause:
  Samsung HAL maps ALL AudioSources with stream_type=11 (MIC, CAMCORDER,
  VOICE_RECOGNITION, VOICE_COMMUNICATION) to AUSAGE=110 (pcm110c, calliope_10).
  During SIP calls there is no modem audio on this path, so the capture is silent.

Targeted fix:
  In proxy_create_capture_stream, TBH target for stream_type=11 (vaddr 0xa30e)
  hardcodes AUSAGE=110. We replace this with a branch to a conditional hook at
  vaddr 0xbae4 (unused NOP padding + unused literal-pool area).

  The hook checks ausage_param ([r8, #4]):
    - ausage_param == 1  (MIC / VOICE_COMMUNICATION) → AUSAGE = 16  (pcm16c, WDMA4)
    - ausage_param == 2  (CAMCORDER)                 → AUSAGE = 110 (pcm110c, stock)
    - ausage_param == 27 (VOICE_RECOGNITION)         → AUSAGE = 110 (pcm110c, stock)

  This way SIP calls get real mic audio, while video recording and other
  stream_type=11 captures keep their original behaviour.

NOTE: mixer_paths.xml analysis confirms Samsung's standard mic paths
(`media-mic`, `communication-handset-mic`) route to WDMA4 (pcm16c).
AUSAGE=16 (WDMA4) is the correct value. AUSAGE=12 (WDMA0) was an earlier
incorrect assumption that leads to `invalid source dai` kernel errors.

File offsets:
  Branch:   fileoff 0x930e  (vaddr 0xa30e)
  Hook:     fileoff 0xaae4  (vaddr 0xbae4)

Usage:
  python3 scripts/patch_ausage_stream_type_11.py patch     # apply
  python3 scripts/patch_ausage_stream_type_11.py restore   # restore from backup
  python3 scripts/patch_ausage_stream_type_11.py verify    # check current state
"""
import argparse
import struct
from pathlib import Path

BINARY = Path(__file__).parent.parent / "binaries" / "libaudioproxy_patched.so"

TEXT_VADDR = 0x7AB0
TEXT_FILEOFF = 0x6AB0


def vaddr_to_fileoff(vaddr):
    return TEXT_FILEOFF + (vaddr - TEXT_VADDR)


# Patch locations
BRANCH_VADDR = 0xA30E
BRANCH_FILEOFF = vaddr_to_fileoff(BRANCH_VADDR)

HOOK_VADDR = 0xBAE4
HOOK_FILEOFF = vaddr_to_fileoff(HOOK_VADDR)

# Original bytes at branch location (movs r6, #110; b.n a37a)
ORIG_BRANCH = bytes([0x6E, 0x26, 0x33, 0xE0])

# New branch: b.w 0xbae4 from 0xa30e
# Assembled with: .thumb; b.w 0xbae4; at .org 0xa30e
PATCH_BRANCH = bytes([0x01, 0xF0, 0xE9, 0xBB])

# Hook code (16 bytes)
#   ldr.w r0, [r8, #4]
#   cmp r0, #1
#   ite eq
#   moveq r6, #16           ; AUSAGE=16 -> pcm16c (WDMA4)
#   movne r6, #110
#   b.w a37a
#
# NOTE (2026-05-27): mixer_paths.xml confirms Samsung's mic paths route to WDMA4
# (pcm16c), not WDMA0. AUSAGE=16 is the correct value.
PATCH_HOOK = bytes([
    0xD8, 0xF8, 0x04, 0x00,  # ldr.w r0, [r8, #4]
    0x01, 0x28,              # cmp r0, #1
    0x0C, 0xBF,              # ite eq
    0x10, 0x26,              # moveq r6, #16
    0x6E, 0x26,              # movne r6, #110
    0xFE, 0xF7, 0x43, 0xBC,  # b.w a37a
])

# Original bytes at hook location (12 NOPs + 4 bytes unused literal pool)
ORIG_HOOK = bytes([
    0x00, 0xBF, 0x00, 0xBF, 0x00, 0xBF,
    0x00, 0xBF, 0x00, 0xBF, 0x00, 0xBF,
    0x26, 0x00, 0x24, 0x00,
])


def verify(data):
    print("=== Verification ===")
    ok = True

    branch = data[BRANCH_FILEOFF:BRANCH_FILEOFF + 4]
    if branch == PATCH_BRANCH:
        print(f"  0x{BRANCH_VADDR:04x}: conditional branch to hook — PATCHED")
    elif branch == ORIG_BRANCH:
        print(f"  0x{BRANCH_VADDR:04x}: original movs r6, #110 — STOCK")
        ok = False
    else:
        print(f"  0x{BRANCH_VADDR:04x}: {branch.hex()} — UNEXPECTED")
        ok = False

    hook = data[HOOK_FILEOFF:HOOK_FILEOFF + 16]
    if hook == PATCH_HOOK:
        print(f"  0x{HOOK_VADDR:04x}: conditional hook present — PATCHED")
    elif hook == ORIG_HOOK:
        print(f"  0x{HOOK_VADDR:04x}: original NOPs — STOCK")
        ok = False
    else:
        print(f"  0x{HOOK_VADDR:04x}: {hook.hex()} — UNEXPECTED")
        ok = False

    return ok


def patch(args):
    binary = Path(args.binary)
    if not binary.exists():
        print(f"ERROR: {binary} not found")
        return 1

    data = bytearray(binary.read_bytes())

    print("=== Applying conditional patch ===")
    print(f"Binary: {binary}")
    print(f"Branch vaddr: 0x{BRANCH_VADDR:04x} (file offset: 0x{BRANCH_FILEOFF:04x})")
    print(f"Hook   vaddr: 0x{HOOK_VADDR:04x} (file offset: 0x{HOOK_FILEOFF:04x})")

    branch = data[BRANCH_FILEOFF:BRANCH_FILEOFF + 4]
    hook = data[HOOK_FILEOFF:HOOK_FILEOFF + 16]

    if branch == PATCH_BRANCH and hook == PATCH_HOOK:
        print("  Already patched (conditional hook)")
    else:
        print(f"  Writing branch at 0x{BRANCH_VADDR:04x}")
        data[BRANCH_FILEOFF:BRANCH_FILEOFF + 4] = PATCH_BRANCH
        print(f"  Writing hook at 0x{HOOK_VADDR:04x}")
        data[HOOK_FILEOFF:HOOK_FILEOFF + 16] = PATCH_HOOK

    # Verify
    print()
    verify(data)

    if args.dry_run:
        print("Dry run — not writing file.")
        return 0

    # Backup original if not already backed up
    backup = binary.with_suffix(".so.backup")
    if not backup.exists():
        backup.write_bytes(binary.read_bytes())
        print(f"Backup written to: {backup}")

    binary.write_bytes(data)
    print(f"Patched binary written to: {binary}")
    return 0


def restore(args):
    binary = Path(args.binary)
    backup = binary.with_suffix(".so.backup")
    if not backup.exists():
        print(f"ERROR: backup not found: {backup}")
        return 1
    binary.write_bytes(backup.read_bytes())
    print(f"Restored {binary} from {backup}")
    return 0


def check(args):
    binary = Path(args.binary)
    if not binary.exists():
        print(f"ERROR: {binary} not found")
        return 1
    data = binary.read_bytes()
    return 0 if verify(data) else 1


def main():
    parser = argparse.ArgumentParser(
        description="Patch libaudioproxy.so to route VOICE_COMMUNICATION capture to real mic"
    )
    parser.add_argument("--binary", default=str(BINARY), help="Path to libaudioproxy.so")
    sub = parser.add_subparsers(dest="command", required=True)

    p_patch = sub.add_parser("patch", help="Apply patch")
    p_patch.add_argument("--dry-run", action="store_true", help="Do not write file")

    sub.add_parser("restore", help="Restore from backup")
    sub.add_parser("verify", help="Check current patch state")

    args = parser.parse_args()

    if args.command == "patch":
        return patch(args)
    elif args.command == "restore":
        return restore(args)
    elif args.command == "verify":
        return check(args)


if __name__ == "__main__":
    raise SystemExit(main())
