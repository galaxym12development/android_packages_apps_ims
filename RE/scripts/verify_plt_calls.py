#!/usr/bin/env python3
"""
verify_plt_calls.py — Verify actual PLT targets for calls inside
proxy_open_capture_stream and proxy_create_capture_stream.

The README incorrectly identified some blx targets. This script resolves
all PLT entries referenced by libaudioproxy.so::proxy_open_capture_stream
and prints the actual symbol names.

Usage: python3 scripts/verify_plt_calls.py
"""
import re
import subprocess
from pathlib import Path

LIBAP = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"


def get_plt_map():
    """Build PLT vaddr -> symbol name map by parsing readelf output."""
    result = subprocess.run(
        ["readelf", "--relocs", str(LIBAP)],
        capture_output=True, text=True
    )
    # Parse .rel.plt entries. Each line looks like:
    # 0002f150  0000b602 R_ARM_JUMP_SLOT   00000000   pthread_rwlock_rdlock
    plt_map = {}
    plt_base = None
    entry_size = None

    # First get PLT section info
    sec_result = subprocess.run(
        ["readelf", "-S", str(LIBAP)],
        capture_output=True, text=True
    )
    for line in sec_result.stdout.splitlines():
        if ".plt" in line and "PROGBITS" in line:
            parts = line.split()
            # e.g. [16] .plt PROGBITS 0000f130 00e130 0007b0 00 AX 0 0 16
            plt_base = int(parts[4], 16)
            entry_size = int(parts[-1])
            break

    if plt_base is None:
        raise RuntimeError("Could not find .plt section")

    for line in result.stdout.splitlines():
        if "R_ARM_JUMP_SLOT" in line or "R_ARM_GLOB_DAT" in line:
            parts = line.split()
            sym_name = parts[-1]
            # PLT entry index: skip resolver (0) and padding (1)
            # relplt row 0 -> PLT entry 2
            # We need to count rows to get the right PLT address
            # But readelf doesn't show the row index directly in --relocs
            pass

    # Better: use objdump -d -j .plt and read the PLT entry addresses
    # Then use the relplt mapping we already know works
    # Let's just parse objdump directly for the function
    return plt_base, entry_size


def get_plt_symbols():
    """Return {plt_vaddr: symbol_name} by parsing .rel.plt with known PLT layout."""
    # PLT: vaddr=0xf130, entry_size=16
    # Entry 0=resolver at 0xf130, Entry 1=padding at 0xf140
    # Entry N (N>=2) corresponds to relplt index N-2
    PLT_BASE = 0xF130
    ENTRY_SIZE = 16

    result = subprocess.run(
        ["readelf", "-r", "--wide", str(LIBAP)],
        capture_output=True, text=True
    )
    # Parse the relocation dump
    plt_map = {}
    in_relplt = False
    idx = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if ".rel.plt" in line:
            in_relplt = True
            continue
        if not in_relplt:
            continue
        if not line or line.startswith("Offset"):
            continue
        if not line[0].isdigit():
            in_relplt = False
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        sym_name = parts[-1]
        plt_vaddr = PLT_BASE + (idx + 2) * ENTRY_SIZE
        plt_map[plt_vaddr] = sym_name
        idx += 1

    return plt_map


def find_bl_calls_in_range(plt_map, func_start, func_size):
    """Use objdump to find bl/blx in the given vaddr range and resolve PLT targets."""
    result = subprocess.run(
        [
            "arm-linux-gnueabi-objdump",
            "-d",
            "-j", ".text",
            "--start-address", hex(func_start),
            "--stop-address", hex(func_start + func_size),
            str(LIBAP),
        ],
        capture_output=True, text=True,
    )
    calls = []
    for line in result.stdout.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+([0-9a-f]{4})\s+([0-9a-f]{4})\s+.*(bl[x]?)", line)
        if not m:
            # Try 32-bit format where objdump shows just one word
            m = re.match(r"\s+([0-9a-f]+):\s+([0-9a-f]{8})\s+.*(bl[x]?)", line)
            if m:
                addr = int(m.group(1), 16)
                kind = m.group(3)
                # Parse the 32-bit word
                word = int(m.group(2), 16)
                hw1 = word & 0xFFFF
                hw2 = (word >> 16) & 0xFFFF
                target = decode_thumb_bl(addr, hw1, hw2, kind == "blx")
                sym = plt_map.get(target, "<local>")
                calls.append((addr, kind, target, sym))
            continue

        addr = int(m.group(1), 16)
        kind = m.group(4)
        hw1 = int(m.group(2), 16)
        hw2 = int(m.group(3), 16)
        target = decode_thumb_bl(addr, hw1, hw2, kind == "blx")
        sym = plt_map.get(target, "<local>")
        calls.append((addr, kind, target, sym))

    return calls


def decode_thumb_bl(vaddr, hw1, hw2, is_blx):
    """Decode Thumb-2 BL / BLX immediate target."""
    h = (hw1 >> 10) & 1  # sign/high bit
    imm10 = hw1 & 0x3FF
    j1 = (hw2 >> 13) & 1
    j2 = (hw2 >> 11) & 1
    imm11 = hw2 & 0x7FF

    i1 = 1 if j1 == h else 0
    i2 = 1 if j2 == h else 0

    # Build 25-bit immediate and shift left by 1
    imm32 = (h << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    # Sign extend from 25 bits
    if h:
        imm32 -= 1 << 25

    target = (vaddr + 4 + imm32) & 0xFFFFFFFF

    if is_blx:
        # BLX target is always word-aligned (bit[1] forced to 0)
        target &= ~0x2

    return target


def main():
    plt_map = get_plt_symbols()

    # proxy_open_capture_stream: vaddr=0xa9f0, size=1024
    print("=== PLT calls inside proxy_open_capture_stream (vaddr 0xa9f0, size=1024) ===")
    targets = find_bl_calls_in_range(plt_map, 0xA9F0, 1024)
    for vaddr, kind, target, sym in targets:
        print(f"  0x{vaddr:04x}: {kind} 0x{target:04x}  =>  {sym}")

    print()

    # proxy_create_capture_stream: vaddr=0x9ee8, size=1332
    print("=== PLT calls inside proxy_create_capture_stream (vaddr 0x9ee8, size=1332) ===")
    targets = find_bl_calls_in_range(plt_map, 0x9EE8, 1332)
    for vaddr, kind, target, sym in targets:
        print(f"  0x{vaddr:04x}: {kind} 0x{target:04x}  =>  {sym}")

    print()
    print("=== CORRECTIONS to README claims ===")
    print("  README claim: 0x00aa5a blx #0xf170 = audio_route_apply_path")
    print("  ACTUAL:      0x00aa5a blx #0xf170 = pthread_rwlock_rdlock")
    print()
    print("  README claim: 0x00aab6 blx #0xf1b0 = 'second call (unknown)'")
    print("  ACTUAL:      0x00aab6 blx #0xf1b0 = pthread_rwlock_unlock")
    print()
    print("  README claim: audio_route_apply_path is called inside proxy_open_capture_stream")
    print("  ACTUAL:      audio_route_apply_path is at c0fe and c302 (different functions)")
    print()
    print("  README claim: 0x00aadc bl #0xa6f0 = pcm_open")
    print("  ACTUAL:      0x00aadc bl #0xa6f0 = local call (helper inside libaudioproxy)")
    print("  The actual pcm_open call is at 0x00ab92 (blx #0xf310)")


if __name__ == '__main__':
    main()
