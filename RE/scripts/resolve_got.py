#!/usr/bin/env python3
"""
resolve_got.py — Resolve GOT entries in libaudioproxy.so by parsing ELF headers.

Usage:
    python3 resolve_got.py [GOT_VADDR ...]
    python3 resolve_got.py 0x10a3e 0x10a42 0x10a46 0x10a48 0x10a54

Without arguments, dumps all non-zero GOT entries.
"""
import struct, sys
from pathlib import Path

# ELF32 section header format
SH_FMT = '<IIIIIIIIII'

def read_sections(data):
    e_shoff = struct.unpack_from('<I', data, 0x20)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x2e)[0]
    e_shnum = struct.unpack_from('<H', data, 0x30)[0]
    e_shstrndx = struct.unpack_from('<H', data, 0x32)[0]

    sections = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sections.append(struct.unpack_from(SH_FMT, data, off))

    shstr_off = sections[e_shstrndx][4]

    def shname(idx):
        start = shstr_off + sections[idx][0]
        return data[start:data.index(b'\x00', start)].decode()

    return {shname(i): sections[i] for i in range(e_shnum)}

def main():
    binary = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"
    data = binary.read_bytes()
    sections = read_sections(data)

    got = sections.get('.got')
    if not got:
        print("No .got section found", file=sys.stderr)
        sys.exit(1)

    got_vaddr, got_fileoff, got_size = got[3], got[4], got[5]
    print(f"=== .got: vaddr=0x{got_vaddr:06x} fileoff=0x{got_fileoff:06x} size={got_size} ===")

    if len(sys.argv) > 1:
        targets = [int(a, 0) for a in sys.argv[1:]]
        for vaddr in targets:
            if got_vaddr <= vaddr < got_vaddr + got_size:
                off = got_fileoff + (vaddr - got_vaddr)
                val = struct.unpack_from('<I', data, off)[0]
                print(f"  0x{vaddr:06x} = 0x{val:08x}")
            else:
                print(f"  0x{vaddr:06x} OUTSIDE .got")
    else:
        for i in range(0, got_size, 4):
            vaddr = got_vaddr + i
            off = got_fileoff + i
            val = struct.unpack_from('<I', data, off)[0]
            if val != 0:
                print(f"  GOT[0x{i:04x}] vaddr=0x{vaddr:06x} = 0x{val:08x}")

if __name__ == '__main__':
    main()
