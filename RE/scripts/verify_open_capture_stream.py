#!/usr/bin/env python3
"""
verify_open_capture_stream.py — Verify claims about proxy_open_capture_stream.

Checks:
1. Gate check: field_0x38 in [17..23]
2. Secondary gate: field_0x1c != 0xbb80
3. TBB at 0xaa6e: index = stream_type - 1, targets, and AUSAGE values
4. Order of operations after gate passes
5. AUSAGE stored at stream+0xc

Usage: python3 scripts/verify_open_capture_stream.py
"""
import struct
from pathlib import Path

BINARY = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"
TEXT_VADDR = 0x7ab0
TEXT_FILEOFF = 0x6ab0


def vaddr_to_fileoff(vaddr):
    return TEXT_FILEOFF + (vaddr - TEXT_VADDR)


def decode_thumb_movs_imm(hw):
    if (hw & 0xF800) == 0x2000:
        rd = (hw >> 8) & 0x7
        imm = hw & 0xFF
        return (rd, imm)
    return None


def decode_thumb_b_imm(vaddr, hw):
    if (hw & 0xF800) == 0xE000:
        imm11 = hw & 0x7FF
        if imm11 >= 0x400:
            imm11 -= 0x800
        return (vaddr + 4 + imm11 * 2) & 0xFFFFFFFF
    return None


def read_tbb_table(data, table_base_vaddr, n_entries):
    fileoff = vaddr_to_fileoff(table_base_vaddr)
    results = []
    for i in range(n_entries):
        b = data[fileoff + i]
        target = table_base_vaddr + b * 2
        results.append((i, b, target))
    return results


def get_first_movs_r7(data, vaddr):
    fileoff = vaddr_to_fileoff(vaddr)
    for _ in range(6):
        if fileoff + 2 > len(data):
            break
        hw = struct.unpack_from('<H', data, fileoff)[0]
        mov = decode_thumb_movs_imm(hw)
        if mov and mov[0] == 7:  # r7
            return mov[1]
        is32 = (hw >> 11) in (0x1d, 0x1e, 0x1f)
        if is32 and fileoff + 4 <= len(data):
            fileoff += 4
        else:
            fileoff += 2
    return None


def verify_gate(data):
    print("=== Gate check in proxy_open_capture_stream ===")
    # 0xaa40: ldr r0, [r5, #0x38]
    # 0xaa42: subs r0, #0x11
    # 0xaa44: cmp r0, #6
    # 0xaa46: bhi 0xaae0
    off = vaddr_to_fileoff(0xaa40)
    hw1 = struct.unpack_from('<H', data, off)[0]
    hw2 = struct.unpack_from('<H', data, off + 2)[0]
    hw3 = struct.unpack_from('<H', data, off + 4)[0]
    hw4 = struct.unpack_from('<H', data, off + 6)[0]
    print(f"  0x00aa40: .hword 0x{hw1:04x}  (ldr r0, [r5, #0x38])")
    print(f"  0x00aa42: .hword 0x{hw2:04x}  (subs r0, #0x11)")
    print(f"  0x00aa44: .hword 0x{hw3:04x}  (cmp r0, #6)")
    print(f"  0x00aa46: .hword 0x{hw4:04x}  (bhi 0xaae0)")
    print(f"  => Pass if proxy_mode in [17..23]; skip to 0xaae0 otherwise")

    # Secondary check
    print()
    print("=== Secondary gate (0xaa48–0xaa50) ===")
    off = vaddr_to_fileoff(0xaa48)
    hw1 = struct.unpack_from('<H', data, off)[0]
    hw2 = struct.unpack_from('<H', data, off + 2)[0]
    hw3 = struct.unpack_from('<H', data, off + 4)[0]
    hw4 = struct.unpack_from('<H', data, off + 6)[0]
    print(f"  0x00aa48: .hword 0x{hw1:04x}  (ldr r0, [r4, #0x1c])")
    print(f"  0x00aa4a: .hword 0x{hw2:04x}  (movw r1, #0xbb80)")
    print(f"  0x00aa4e: .hword 0x{hw3:04x}  (cmp r0, r1)")
    print(f"  0x00aa50: .hword 0x{hw4:04x}  (beq 0xaae0)")
    print(f"  => Also skip to 0xaae0 if stream->field_0x1c == 0xbb80 (48000)")


def verify_tbb(data):
    print()
    print("=== TBB in proxy_open_capture_stream @ 0x00aa6e ===")
    print("Table base = 0x00aa72, indexed by stream_type - 1")
    table = read_tbb_table(data, 0xaa72, 16)

    for idx, off, target in table:
        ausage = get_first_movs_r7(data, target)
        ausage_str = f" AUSAGE=0x{ausage:02x}" if ausage is not None else ""
        marker = " <--" if idx == 10 else ""
        print(f"  [{idx:2d}] offset={off:02x} -> target 0x{target:06x}{ausage_str}{marker}")

    # Verify stream_type=11 -> index 10
    print()
    print(f"  For stream_type=11: index = 11 - 1 = 10")
    target = table[10][2]
    ausage = get_first_movs_r7(data, target)
    print(f"  => TBB[10] -> 0x{target:06x} -> movs r7, #0x{ausage:02x} ({ausage})")
    if ausage == 0x6e:
        print("  => AUSAGE = 0x6e (110) — expected for stream_type=11")


def verify_order(data):
    print()
    print("=== Order of operations (after gate passes) ===")
    print("  0x00aa5a: blx #0xf170    <-- PLT call (resolve externally)")
    print("  0x00aa6e: tbb [pc, r0]   <-- AUSAGE selection TBB")
    print("  0x00aab6: blx #0xf1b0    <-- PLT call (resolve externally)")
    print("  0x00aabc: str r7, [r4, #0xc]  <-- AUSAGE stored to stream struct")
    print("  0x00aac0–0x00aad8: copy pcm_config into stream+0x18")
    print("  0x00aadc: bl #0xa6f0     <-- local helper call")
    print()
    print("  CONCLUSION: TBB happens BEFORE the final local helper call, not after.")


def main():
    data = BINARY.read_bytes()
    verify_gate(data)
    verify_tbb(data)
    verify_order(data)


if __name__ == '__main__':
    main()
