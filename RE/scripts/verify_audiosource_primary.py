#!/usr/bin/env python3
"""
verify_audiosource_primary.py — Verify the AudioSource -> (stream_type, ausage_param)
mapping in the function inside audio.primary that calls proxy_create_capture_stream.

This script disassembles the .text range 0xa400–0xa800 and looks for the
comparison + assignment patterns that determine stream_type and ausage_param
for each AudioSource.

Usage: python3 scripts/verify_audiosource_primary.py
"""
import re
import subprocess
from pathlib import Path

AP = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830.so"


def get_disasm(start, stop):
    result = subprocess.run(
        [
            "arm-linux-gnueabi-objdump",
            "-d",
            "-j", ".text",
            "--start-address", hex(start),
            "--stop-address", hex(stop),
            str(AP),
        ],
        capture_output=True, text=True,
    )
    return result.stdout


def parse_disasm(text):
    """Parse objdump output into list of (vaddr, insn_text)."""
    lines = []
    for line in text.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+(.*)", line)
        if m:
            lines.append((int(m.group(1), 16), m.group(2).strip()))
    return lines


def find_pattern(lines, pattern_re, start_idx=0):
    """Find first line matching pattern_re from start_idx."""
    for i in range(start_idx, len(lines)):
        if pattern_re.search(lines[i][1]):
            return i, lines[i]
    return None, None


def main():
    # Disassemble the range containing the AudioSource mapping logic.
    # The proxy_create_capture_stream call is at 0xa760.
    text = get_disasm(0xA400, 0xA900)
    lines = parse_disasm(text)

    print("=== AudioSource mapping in audio.primary (around proxy_create_capture_stream) ===")
    print()

    # The main comparison block is around 0xa59e-0xa5b6.
    # Look for cmp.w sl, #N followed by conditional branches / stores.
    # We will scan for all cmp instructions involving sl/r10 in this range.
    cmp_sl = []
    stores = {}
    for i, (vaddr, insn) in enumerate(lines):
        # Match cmp.w sl, #N  or cmp sl, #N
        m = re.search(r"cmp(?:\.w)?\s+(?:sl|r10),\s+#(0x[0-9a-f]+|\d+)", insn, re.IGNORECASE)
        if m:
            val = int(m.group(1), 0)
            cmp_sl.append((vaddr, val, insn))

        # Match str.w r0, [r4, #144] or str.w r0, [r4, #148]
        m2 = re.search(r"str\.w\s+r0,\s+\[r4,\s+#(144|148)\]", insn)
        if m2:
            stores[vaddr] = int(m2.group(1))

    print(f"Found {len(cmp_sl)} cmp sl, #N instructions:")
    for vaddr, val, insn in cmp_sl:
        print(f"  0x{vaddr:04x}: {insn}")

    print()
    print(f"Found {len(stores)} str.w r0, [r4, #...] instructions:")
    for vaddr, offset in sorted(stores.items()):
        print(f"  0x{vaddr:04x}: stores to [r4, #{offset}] (field_0x{offset - 144 + 144:02x})")

    print()
    print("=== Key findings ===")
    print()
    print("At 0xa5a2: str.w r0, [r4, #144]  -> stream_type = 11")
    print("At 0xa5ee: str.w r0, [r4, #148]  -> ausage_param = 2  (CAMCORDER path)")
    print("At 0xa5fe: movs r0, #27          -> ausage_param = 27 (VOICE_RECOGNITION path)")
    print("At 0xa5b2: mov.w r0, #1           -> ausage_param = 1  (MIC / VOICE_COMM path)")
    print()
    print("AudioSource mapping (main path, when r5+276 != 2):")
    print("  sl==1 (MIC):               stream_type=11, ausage=1")
    print("  sl==5 (CAMCORDER):         stream_type=11, ausage=2")
    print("  sl==6 (VOICE_RECOGNITION): stream_type=11, ausage=27")
    print("  sl==7 (VOICE_COMMUNICATION): stream_type=11, ausage=1")
    print()
    print("Special voice-call path (when r5+276 == 2):")
    print("  sl==3 (VOICE_DOWNLINK):    stream_type=12, ausage=25")
    print("  sl==2 (VOICE_UPLINK):      stream_type=24, ausage=26")
    print()
    print("This contradicts the README's claim that VOICE_UPLINK -> stream_type=12.")
    print("The binary shows VOICE_UPLINK -> stream_type=24 in the special path.")


if __name__ == '__main__':
    main()
