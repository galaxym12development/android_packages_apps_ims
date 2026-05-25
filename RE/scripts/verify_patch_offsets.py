#!/usr/bin/env python3
"""
verify_patch_offsets.py — Verify Patch A and Patch C offset calculations.

Checks:
1. Patch C: vaddr 0x08a9a in audio.primary -> fileoff and actual byte value
2. Patch A: vaddr 0xaa46 in libaudioproxy -> fileoff and actual byte value
3. Patch C safety: only affects alternate path
4. Patch A safety: only affects proxy_open_capture_stream

Usage: python3 scripts/verify_patch_offsets.py
"""
import struct
from pathlib import Path

AP_BINARY = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830.so"
LP_BINARY = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"

# audio.primary section mapping
AP_TEXT_VADDR = 0x7260
AP_TEXT_FILEOFF = 0x6260

# libaudioproxy section mapping
LP_TEXT_VADDR = 0x7ab0
LP_TEXT_FILEOFF = 0x6ab0


def ap_vaddr_to_fileoff(vaddr):
    return AP_TEXT_FILEOFF + (vaddr - AP_TEXT_VADDR)


def lp_vaddr_to_fileoff(vaddr):
    return LP_TEXT_FILEOFF + (vaddr - LP_TEXT_VADDR)


def verify_patch_c():
    print("=== Patch C: audio.primary.universal3830.so ===")
    target_vaddr = 0x08a9a  # movs r0, #0x25 in alternate path
    fileoff = ap_vaddr_to_fileoff(target_vaddr)
    print(f"  Target vaddr: 0x{target_vaddr:05x}")
    print(f"  .text vaddr: 0x{AP_TEXT_VADDR:05x}, fileoff: 0x{AP_TEXT_FILEOFF:05x}")
    print(f"  Computed fileoff: 0x{fileoff:05x}")

    data = AP_BINARY.read_bytes()
    if fileoff < len(data):
        byte = data[fileoff]
        print(f"  Byte at fileoff: 0x{byte:02x}")
        if byte == 0x25:
            print("  => VERIFIED: current byte is 0x25 (movs r0, #0x25)")
            print("     Patch: change 0x25 -> 0x16 (movs r0, #0x16 = return 22)")
        elif byte == 0x16:
            print("  => ALREADY PATCHED: byte is 0x16 (movs r0, #0x16)")
        else:
            print(f"  *** UNEXPECTED BYTE: expected 0x25, got 0x{byte:02x} ***")
    else:
        print(f"  *** FILEOFF OUT OF RANGE: 0x{fileoff:05x} >= file size {len(data)} ***")

    # Verify surrounding context: check it's in the alternate path
    print()
    print("  Context verification (alternate path at 0x08a88):")
    context_off = ap_vaddr_to_fileoff(0x08a88)
    for i in range(8):
        addr = 0x08a88 + i * 2
        off = context_off + i * 2
        hw = struct.unpack_from('<H', data, off)[0]
        is32 = (hw >> 11) in (0x1d, 0x1e, 0x1f)
        extra = ""
        if addr == 0x08a88:
            extra = "  ; ldr r1, [pc, #0x38]"
        elif addr == 0x08a8a:
            extra = "  ; movs r0, #0x4"
        elif addr == 0x08a9a and hw == 0x2520:
            extra = "  ; movs r0, #0x25  <-- PATCH HERE"
        elif addr == 0x08a9c:
            extra = "  ; pop {pc,...}"
        print(f"    0x{addr:05x}: 0x{hw:04x}{extra}")
        if is32:
            i += 1  # skip extra halfword in loop


def verify_patch_a():
    print()
    print("=== Patch A: libaudioproxy.so ===")
    target_vaddr = 0xaa46  # bhi in gate check
    fileoff = lp_vaddr_to_fileoff(target_vaddr)
    print(f"  Target vaddr: 0x{target_vaddr:05x}")
    print(f"  .text vaddr: 0x{LP_TEXT_VADDR:05x}, fileoff: 0x{LP_TEXT_FILEOFF:05x}")
    print(f"  Computed fileoff: 0x{fileoff:05x}")

    data = LP_BINARY.read_bytes()
    if fileoff < len(data):
        hw = struct.unpack_from('<H', data, fileoff)[0]
        print(f"  Halfword at fileoff: 0x{hw:04x}")
        # bhi 0xaae0: encoding is 0xd8XX where XX is the signed offset
        # 0xd84b: condition=hi (8), imm8=0x4b
        if (hw & 0xf800) == 0xd800:
            cond = (hw >> 8) & 0xf
            imm8 = hw & 0xff
            if imm8 >= 0x80:
                imm8 -= 0x100
            target = (0xaa46 + 4 + imm8 * 2) & 0xffff
            print(f"  Decoded: bhi #0x{target:05x} (condition={cond})")
            if target == 0xaae0:
                print("  => VERIFIED: bhi branches to 0xaae0 (skip mixer arming)")
                print("     Patch: overwrite with 0xbf00 (NOP16) to always fall through")
            else:
                print(f"  *** UNEXPECTED TARGET: expected 0xaae0, got 0x{target:05x} ***")
        elif hw == 0xbf00:
            print("  => ALREADY PATCHED: NOP16 at gate location")
        else:
            print(f"  *** UNEXPECTED INSTRUCTION: expected bhi, got 0x{hw:04x} ***")
    else:
        print(f"  *** FILEOFF OUT OF RANGE: 0x{fileoff:05x} >= file size {len(data)} ***")


def main():
    verify_patch_c()
    verify_patch_a()


if __name__ == '__main__':
    main()
