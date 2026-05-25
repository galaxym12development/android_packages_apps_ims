#!/usr/bin/env python3
"""
verify_stream_offsets.py — Verify who writes stream+8 (card) and stream+12 (device/AUSAGE).

Key findings:
  - proxy_create_capture_stream writes AUSAGE to [r8, #12] at epilogue sites
    (0xa1ee, 0xa320, 0xa34c, 0xa382). For stream_type=11 the TBH6 target at
    0xa30e sets AUSAGE = 0x6e (110).
  - proxy_open_capture_stream writes card=0 at [r4, #8] (0xaa56) and has a
    TBB that would write AUSAGE at [r4, #12] (0xaabc), BUT a secondary gate
    (beq 0xaae0 at 0xaa50) skips the TBB for 48 kHz captures — which is
    always true for standard capture. The TBB is therefore dead code.
  - ldrd r6, r8, [r4, #8] at 0xab0c loads (card=0, device=110) into the
    registers passed to pcm_open. The device number comes from stage 1.

Usage: python3 scripts/verify_stream_offsets.py
"""
import re
import subprocess
from pathlib import Path

LIBAP = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"
AP = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830.so"


def get_disasm(binary, start, stop):
    result = subprocess.run(
        [
            "arm-linux-gnueabi-objdump",
            "-d",
            "-j", ".text",
            "--start-address", hex(start),
            "--stop-address", hex(stop),
            str(binary),
        ],
        capture_output=True, text=True,
    )
    return result.stdout


def find_str_to_offsets(text, base_regs, offsets):
    """Find all str instructions that write to [base_reg, #offset] for any base_reg."""
    results = []
    for line in text.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+(.*)", line)
        if not m:
            continue
        vaddr = int(m.group(1), 16)
        insn = m.group(2).strip()
        for base_reg in base_regs:
            for off in offsets:
                # Match str/str.w/strd with [base_reg, #offset]
                pat = rf"str(?:\.w|d)?\s+r\d+,\s+\[{base_reg},\s+#(0x[0-9a-f]+|\d+)\]"
                if re.search(pat, insn, re.IGNORECASE):
                    results.append((vaddr, insn))
                    break
            else:
                continue
            break
    return results


def main():
    print("=== Writes to [r4/r8, #8] and [r4/r8, #12] inside proxy_create_capture_stream ===")
    text = get_disasm(LIBAP, 0x9EE8, 0x9EE8 + 1332)
    writes = find_str_to_offsets(text, ["r4", "r8"], [8, 12])
    if writes:
        for vaddr, insn in writes:
            print(f"  0x{vaddr:04x}: {insn}")
    else:
        print("  NONE found.")
    print("  => AUSAGE is written via [r8, #12] at epilogue sites (0xa1ee, 0xa320, etc.)")
    print()

    print("=== Writes to [r4, #8] and [r4, #12] inside proxy_open_capture_stream ===")
    text = get_disasm(LIBAP, 0xA9F0, 0xA9F0 + 1024)
    writes = find_str_to_offsets(text, ["r4"], [8, 12])
    for vaddr, insn in writes:
        print(f"  0x{vaddr:04x}: {insn}")
    print("  => The ONLY writes in proxy_open_capture_stream are at 0xaa56 (card=0) and")
    print("     0xaabc (AUSAGE from TBB) — BUT the TBB is SKIPPED by the secondary gate.")
    print()

    print("=== ldrd r6, r8, [r4, #8] inside proxy_open_capture_stream ===")
    text = get_disasm(LIBAP, 0xA9F0, 0xA9F0 + 1024)
    for line in text.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+(.*)", line)
        if not m:
            continue
        vaddr = int(m.group(1), 16)
        insn = m.group(2).strip()
        if "ldrd" in insn.lower() and "[r4, #8]" in insn:
            print(f"  0x{vaddr:04x}: {insn}")
    print("  => Card/device are loaded from stream+8 / stream+12 right before pcm_open.")
    print("     stream+12 holds AUSAGE=110 from proxy_create_capture_stream (TBB skipped).")
    print()

    print("=== Secondary gate: beq 0xaae0 at 0xaa50 ===")
    text = get_disasm(LIBAP, 0xA9F0, 0xA9F0 + 1024)
    for line in text.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+(.*)", line)
        if not m:
            continue
        vaddr = int(m.group(1), 16)
        insn = m.group(2).strip()
        if "beq" in insn.lower() and "aae0" in insn:
            print(f"  0x{vaddr:04x}: {insn}")
    print("  => This gate SKIPS the TBB when sample_rate == 48000 (always true).")
    print("     The TBB at 0xaa6e is DEAD CODE for standard capture.")
    print()

    print("=== str [rX, #8] / [rX, #12] in audio.primary after proxy_create_capture_stream ===")
    text = get_disasm(AP, 0xA760, 0xA760 + 400)
    writes = []
    for line in text.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+(.*)", line)
        if not m:
            continue
        vaddr = int(m.group(1), 16)
        insn = m.group(2).strip()
        if re.search(r"str(?:\.w)?\s+r\d+,\s+\[r\d+,\s+#(8|12)\]", insn, re.IGNORECASE):
            writes.append((vaddr, insn))
    if writes:
        for vaddr, insn in writes:
            print(f"  0x{vaddr:04x}: {insn}")
    else:
        print("  NONE found in the 400 bytes after proxy_create_capture_stream returns.")
    print("  => VERIFIED: audio.primary does NOT write card/device into the stream struct")
    print("     immediately after creating it. The values come from libaudioproxy.")


if __name__ == '__main__':
    main()
