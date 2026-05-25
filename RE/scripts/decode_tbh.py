#!/usr/bin/env python3
"""
decode_tbh.py — Decode Thumb-2 TBH (Table Branch Halfword) tables from ARM binaries.

Usage:
    python3 decode_tbh.py BINARY VADDR TABLE_BASE_VADDR N_ENTRIES
    python3 decode_tbh.py RE/binaries/libaudioproxy.so 0x9f4a 0x9f4c 16
"""
import struct, sys
from pathlib import Path

def decode_tbh(data, table_base_vaddr, n_entries, base_vaddr_for_text=0x1000):
    """Decode a TBH table and print each entry with its branch target."""
    fileoff = table_base_vaddr - base_vaddr_for_text
    if fileoff < 0 or fileoff + n_entries * 2 > len(data):
        print(f"[WARN] Table at 0x{table_base_vaddr:x} out of bounds for file", file=sys.stderr)
        return

    print(f"=== TBH table @ 0x{table_base_vaddr:06x} ({n_entries} entries) ===")
    for i in range(n_entries):
        val = struct.unpack_from('<H', data, fileoff + i * 2)[0]
        target = table_base_vaddr + val * 2
        print(f"  [{i:2d}] offset=0x{val:04x}  ->  target 0x{target:06x}")
    print()

def main():
    if len(sys.argv) < 5:
        print("Usage: decode_tbh.py BINARY VADDR_INS TABLE_BASE_VADDR N_ENTRIES [TEXT_BASE_VADDR]", file=sys.stderr)
        sys.exit(1)

    binary = Path(sys.argv[1])
    vaddr_ins = int(sys.argv[2], 0)
    table_base = int(sys.argv[3], 0)
    n_entries = int(sys.argv[4])
    text_base = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0x1000

    data = binary.read_bytes()
    decode_tbh(data, table_base, n_entries, text_base)

if __name__ == '__main__':
    main()
