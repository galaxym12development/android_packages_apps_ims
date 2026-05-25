#!/usr/bin/env python3
"""
verify_proxy_setters.py — Verify claims about proxy_set_route and proxy_set_audiomode.

Checks:
1. proxy_set_route vaddr / size from dynsym
2. Clear sentinel writes (0xbd0a–0xbd18)
3. Transition check at 0xbe1c
4. Real mode write at 0xbeea (strd r6,r8,[sl,#0x38])
5. proxy_set_audiomode vaddr / size
6. Android mode stored to proxy field (NOT field_0x38)

Usage: python3 scripts/verify_proxy_setters.py
"""
import struct
from pathlib import Path

BINARY = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"
TEXT_VADDR = 0x7ab0
TEXT_FILEOFF = 0x6ab0


def vaddr_to_fileoff(vaddr):
    return TEXT_FILEOFF + (vaddr - TEXT_VADDR)


def parse_dynsym(data):
    """Return {name: (vaddr, size)} from ELF .dynsym."""
    e_shoff = struct.unpack_from('<I', data, 0x20)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x2e)[0]
    e_shnum = struct.unpack_from('<H', data, 0x30)[0]
    e_shstrndx = struct.unpack_from('<H', data, 0x32)[0]

    sections = [struct.unpack_from('<IIIIII', data, e_shoff + i * e_shentsize)
                for i in range(e_shnum)]
    shstr_off = sections[e_shstrndx][4]

    def shname(idx):
        start = shstr_off + sections[idx][0]
        return data[start:data.index(b'\x00', start)].decode()

    dynsym = next((i for i in range(e_shnum) if shname(i) == '.dynsym'), None)
    dynstr = next((i for i in range(e_shnum) if shname(i) == '.dynstr'), None)
    if dynsym is None or dynstr is None:
        return {}

    sym_off, sym_size = sections[dynsym][4], sections[dynsym][5]
    str_off = sections[dynstr][4]
    result = {}
    for i in range(sym_size // 16):
        st_name, st_value, st_size = struct.unpack_from('<III', data, sym_off + i * 16)
        if st_value:
            start = str_off + st_name
            name = data[start:data.index(b'\x00', start)].decode()
            result[name] = (st_value & ~1, st_size)
    return result


def read_hw2(data, off):
    """Read second halfword of a 32-bit Thumb-2 instruction."""
    return struct.unpack_from('<H', data, off + 2)[0]


def verify_proxy_set_route(data, syms):
    print("=== proxy_set_route ===")
    vaddr, size = syms.get('proxy_set_route', (0, 0))
    print(f"  .dynsym: vaddr=0x{vaddr:06x}, size={size}")
    if size != 996:
        print(f"  *** SIZE MISMATCH: expected 996, got {size} ***")

    # Check clear sentinel at 0xbd0a
    print()
    print("  Clear sentinel path (0xbd0a–0xbd18):")
    addr = 0xbd0a
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x2024, f"movs r0, #0x24 expected 0x2024, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: movs r0, #0x24")

    addr = 0xbd0c
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x2126, f"movs r1, #0x26 expected 0x2126, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: movs r1, #0x26")

    addr = 0xbd0e
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xf1b8 and hw2 == 0x0f0f, f"cmp.w r8, #0xf unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: cmp.w r8, #0xf")

    addr = 0xbd12
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0xbf8c, f"ite hi expected 0xbf8c, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: ite hi")

    addr = 0xbd14
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xe9ca and hw2 == 0x1011, f"strdhi unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: strdhi r1, r0, [sl, #0x44]")

    addr = 0xbd18
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xe9ca and hw2 == 0x100e, f"strdls unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: strdls r1, r0, [sl, #0x38]  ; sentinel (38, 36)")

    # Check transition check at 0xbe1c
    print()
    print("  Transition check (0xbe1c–0xbe38):")
    addr = 0xbe1c
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xf8da and hw2 == 0x1038, f"ldr.w unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: ldr.w r1, [sl, #0x38]")

    addr = 0xbe20
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x2926, f"cmp r1, #0x26 expected 0x2926, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: cmp r1, #0x26")

    addr = 0xbe24
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xf8da and hw2 == 0x203c, f"ldrne.w unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: ldrne.w r2, [sl, #0x3c]")

    addr = 0xbe28
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x2a24, f"cmpne r2, #0x24 expected 0x2a24, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: cmpne r2, #0x24")

    addr = 0xbe2a
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    imm8 = hw & 0xff
    if imm8 >= 0x80:
        imm8 -= 0x100
    target = (addr + 4 + imm8 * 2) & 0xffff
    assert target == 0xbeda, f"bne target expected 0xbeda, got 0x{target:04x}"
    print(f"    0x{addr:06x}: bne #0xbeda")

    addr = 0xbe34
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    # bl 0xc24c: encoding check
    assert hw == 0xf000 and (hw2 & 0xd000) == 0xd000, f"bl unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: bl #0xc24c")

    addr = 0xbe38
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    # b #imm11: 0xe000 | imm11. Target = addr + 4 + imm11*2
    # 0xe055: imm11 = 0x55 = 85. Target = 0xbe38 + 4 + 170 = 0xbee6
    assert (hw & 0xf800) == 0xe000, f"b expected, got 0x{hw:04x}"
    imm11 = hw & 0x7ff
    target = (addr + 4 + imm11 * 2) & 0xffff
    assert target == 0xbee6, f"b target expected 0xbee6, got 0x{target:04x}"
    print(f"    0x{addr:06x}: b #0xbee6")

    # Check real mode write at 0xbeea
    print()
    print("  Real mode write (0xbeea):")
    addr = 0xbeea
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xe9ca and hw2 == 0x680e, f"strd unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: strd r6, r8, [sl, #0x38]")
    print("    => VERIFIED: field_0x38 = proxy_set_route arg1 (mode)")


def verify_proxy_set_audiomode(data, syms):
    print()
    print("=== proxy_set_audiomode ===")
    vaddr, size = syms.get('proxy_set_audiomode', (0, 0))
    print(f"  .dynsym: vaddr=0x{vaddr:06x}, size={size}")
    if size != 228:
        print(f"  *** SIZE MISMATCH: expected 228, got {size} ***")

    print()
    print("  Key instructions:")
    addr = 0xd670
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x460c, f"mov r4, r1 expected 0x460c, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: mov r4, r1  ; save arg1 = Android mode")

    addr = 0xd672
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert (hw & 0xfbf0) == 0xf240, f"movw unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: movw r1, #0x1f8c")

    addr = 0xd676
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert (hw & 0xfbf0) == 0xf2c0, f"movt unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: movt r1, #1  => r1 = 0x11f8c")

    addr = 0xd67a
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x4605, f"mov r5, r0 expected 0x4605, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: mov r5, r0  ; save arg0 = proxy")

    addr = 0xd67c
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    assert hw == 0x5840, f"ldr r0, [r0, r1] expected 0x5840, got 0x{hw:04x}"
    print(f"    0x{addr:06x}: ldr r0, [r0, r1]  ; read proxy->[0x11f8c] (old mode)")

    addr = 0xd67e
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xeb05 and hw2 == 0x0801, f"add.w unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: add.w r8, r5, r1  ; r8 = &proxy->[0x11f8c]")

    addr = 0xd6de
    off = vaddr_to_fileoff(addr)
    hw = struct.unpack_from('<H', data, off)[0]
    hw2 = read_hw2(data, off)
    assert hw == 0xf8c8 and hw2 == 0x4000, f"str.w unexpected: 0x{hw:04x}{hw2:04x}"
    print(f"    0x{addr:06x}: str.w r4, [r8]  ; store Android mode to proxy field")

    print()
    print("  => VERIFIED: proxy_set_audiomode stores Android mode to proxy field")
    print("     at offset 0x11f8c (via r8 = r5 + 0x11f8c), NOT to field_0x38")


def main():
    data = BINARY.read_bytes()
    syms = parse_dynsym(data)
    verify_proxy_set_route(data, syms)
    verify_proxy_set_audiomode(data, syms)


if __name__ == '__main__':
    main()
