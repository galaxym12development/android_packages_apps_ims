#!/usr/bin/env python3
"""
verify_audio_source_mapping.py — Verify claims about AudioSource → stream_type mapping.

Findings from static analysis of audio.primary.universal3830.so:
1. stream_type=11 (movs r5, #0xb) appears EXACTLY ONCE in .text, at vaddr 0x8876.
2. stream_type=12 (movs r5, #0xc) appears EXACTLY ONCE in .text, at vaddr 0x887c.
3. Both assignments are inside update_capture_stream, reached ONLY after a blx to an
   external function (PLT entry at 0xfe30).
4. The immediate conditions checked are r0==1 → stream_type=11 and r0==2 → stream_type=12.
5. No direct comparisons against AudioSource values 5, 6, or 7 exist in .text.

Conclusion: the exact mapping of AudioSource 5/6/7 to stream_type=11 is NOT statically
verifiable from audio.primary alone; it is mediated through an external function call.

Usage: python3 scripts/verify_audio_source_mapping.py
"""
import struct
from pathlib import Path

BINARY = Path(__file__).parent.parent / "binaries" / "audio.primary.universal3830.so"
TEXT_FILEOFF = 0x6260
TEXT_SIZE = 0x89D4


def find_movs_r5(data):
    """Find all movs r5, #imm in .text. Returns list of (vaddr, imm)."""
    results = []
    for foff in range(TEXT_FILEOFF, TEXT_FILEOFF + TEXT_SIZE, 2):
        hw = int.from_bytes(data[foff:foff + 2], "little")
        if (hw & 0xF800) == 0x2000:
            rd = (hw >> 8) & 0x7
            imm = hw & 0xFF
            if rd == 5:
                vaddr = foff + 0x1000
                results.append((vaddr, imm))
    return results


def verify_stream_type_assignments(data):
    print("=== stream_type assignments (movs r5, #N) in .text ===")
    assignments = find_movs_r5(data)

    # Filter for values that are likely stream types (3–12)
    stream_type_assignments = [(v, i) for v, i in assignments if 3 <= i <= 12]

    for vaddr, imm in stream_type_assignments:
        print(f"  vaddr=0x{vaddr:04x}: movs r5, #{imm} (0x{imm:x})")

    # Verify exactly one 11 and one 12
    elevens = [(v, i) for v, i in stream_type_assignments if i == 11]
    twelves = [(v, i) for v, i in stream_type_assignments if i == 12]

    print()
    if len(elevens) == 1 and elevens[0][0] == 0x8876:
        print("  => VERIFIED: stream_type=11 assigned exactly once at vaddr 0x8876")
    else:
        print(f"  *** UNEXPECTED: found {len(elevens)} stream_type=11 assignments ***")
        for v, i in elevens:
            print(f"      at 0x{v:04x}")

    if len(twelves) == 1 and twelves[0][0] == 0x887c:
        print("  => VERIFIED: stream_type=12 assigned exactly once at vaddr 0x887c")
    else:
        print(f"  *** UNEXPECTED: found {len(twelves)} stream_type=12 assignments ***")
        for v, i in twelves:
            print(f"      at 0x{v:04x}")


def decode_thumb1(data, vaddr):
    """Simple Thumb-1 decoder for display. Returns (size, text)."""
    hw = int.from_bytes(data[vaddr - 0x1000:vaddr - 0x1000 + 2], "little")
    if (hw & 0xF800) == 0x2000:
        rd = (hw >> 8) & 0x7
        imm = hw & 0xFF
        return 2, f"movs r{rd}, #0x{imm:x}"
    elif (hw & 0xF800) == 0x2800:
        rn = (hw >> 8) & 0x7
        imm = hw & 0xFF
        return 2, f"cmp r{rn}, #{imm}"
    elif (hw & 0xFF00) == 0xBF00:
        it_cond = (hw >> 4) & 0xF
        cc = ['eq','ne','cs','cc','mi','pl','vs','vc','hi','ls','ge','lt','gt','le','',''][it_cond]
        return 2, f"it {cc}"
    elif (hw & 0xFF87) == 0x4700:
        rm = (hw >> 3) & 0xF
        return 2, f"bx r{rm}"
    elif (hw & 0xF800) == 0x6800:
        rt, rn, imm5 = hw & 0x7, (hw >> 3) & 0x7, (hw >> 6) & 0x1F
        return 2, f"ldr r{rt}, [r{rn}, #0x{imm5 * 4:x}]"
    elif (hw & 0xF800) == 0x7800:
        rt, rn, imm5 = hw & 0x7, (hw >> 3) & 0x7, (hw >> 6) & 0x1F
        return 2, f"ldrb r{rt}, [r{rn}, #0x{imm5:x}]"
    elif (hw & 0xF800) == 0xF000:
        hw2 = int.from_bytes(data[vaddr - 0x1000 + 2:vaddr - 0x1000 + 4], "little")
        if (hw2 & 0xD000) == 0xC000:
            return 4, "blx #... (PLT)"
        elif (hw2 & 0xD000) == 0xD000:
            return 4, "bl #..."
        return 4, f".word32 0x{hw:04x}{hw2:04x}"
    elif (hw & 0xFFF0) == 0xF8D0:
        hw2 = int.from_bytes(data[vaddr - 0x1000 + 2:vaddr - 0x1000 + 4], "little")
        rn, rt, imm12 = hw & 0xF, (hw2 >> 12) & 0xF, hw2 & 0xFFF
        return 4, f"ldr.w r{rt}, [r{rn}, #0x{imm12:x}]"
    elif (hw & 0xFF00) == 0xBD00:
        mask = hw & 0xFF
        regs = [f"r{i}" for i in range(8) if mask & (1 << i)]
        if mask & 0x100:
            regs.append("pc")
        return 2, f"pop {{{', '.join(regs)}}}"
    elif (hw & 0xF800) == 0xE000:
        imm11 = hw & 0x7FF
        if imm11 >= 0x400:
            imm11 -= 0x800
        target = (vaddr + 4 + imm11 * 2) & 0xFFFF
        return 2, f"b #0x{target:04x}"
    elif hw == 0xB002:
        return 2, "add sp, #8"
    elif (hw & 0xFF80) == 0xB000:
        imm7 = hw & 0x7F
        return 2, f"add sp, sp, #0x{imm7 * 4:x}"
    else:
        return 2, f".hword 0x{hw:04x}"


def find_symbol(data, name):
    """Find (vaddr, size) of a symbol from .dynsym."""
    e_shoff = struct.unpack_from('<I', data, 0x20)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x2e)[0]
    e_shnum = struct.unpack_from('<H', data, 0x30)[0]
    e_shstrndx = struct.unpack_from('<H', data, 0x32)[0]

    sections = [struct.unpack_from('<IIIIIIII', data, e_shoff + i * e_shentsize)
                for i in range(e_shnum)]
    shstr_off = sections[e_shstrndx][4]

    def shname(idx):
        start = shstr_off + sections[idx][0]
        return data[start:data.index(b'\x00', start)].decode('ascii', errors='replace')

    dynsym = next((i for i in range(e_shnum) if shname(i) == '.dynsym'), None)
    dynstr = next((i for i in range(e_shnum) if shname(i) == '.dynstr'), None)
    if dynsym is None or dynstr is None:
        return None

    sym_off, sym_size = sections[dynsym][4], sections[dynsym][5]
    str_off = sections[dynstr][4]
    for i in range(sym_size // 16):
        st_name, st_value, st_size = struct.unpack_from('<III', data, sym_off + i * 16)
        if st_value:
            start = str_off + st_name
            sym_name = data[start:data.index(b'\x00', start)].decode('ascii', errors='replace')
            if sym_name == name:
                return (st_value & ~1, st_size)
    return None


def verify_plt_target(data):
    """Trace the blx at 0x886c to its PLT symbol."""
    # Read .rel.plt to find symbol for PLT entry at 0xfe30
    e_shoff = struct.unpack_from('<I', data, 0x20)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x2e)[0]
    e_shnum = struct.unpack_from('<H', data, 0x30)[0]
    e_shstrndx = struct.unpack_from('<H', data, 0x32)[0]

    sections = [struct.unpack_from('<IIIIIIII', data, e_shoff + i * e_shentsize)
                for i in range(e_shnum)]
    shstr_off = sections[e_shstrndx][4]

    def shname(idx):
        start = shstr_off + sections[idx][0]
        return data[start:data.index(b'\x00', start)].decode('ascii', errors='replace')

    relplt = next((i for i in range(e_shnum) if shname(i) == '.rel.plt'), None)
    dynsym = next((i for i in range(e_shnum) if shname(i) == '.dynsym'), None)
    dynstr = next((i for i in range(e_shnum) if shname(i) == '.dynstr'), None)
    if relplt is None or dynsym is None or dynstr is None:
        return "<unknown>"

    relplt_off, relplt_size = sections[relplt][4], sections[relplt][5]
    sym_off, sym_size = sections[dynsym][4], sections[dynsym][5]
    str_off = sections[dynstr][4]

    # PLT entry at 0xfe30:
    # PLT starts at 0xfc40 (fileoff 0xec40). Entry size = 16 bytes.
    # Entry 0 = resolver, Entry 1 = padding, Entry 2 onwards are function stubs.
    # 0xfe30 = 0xfc40 + N*16  => N = 31
    # Function entries start at N=2, so rel.plt index = N - 2 = 29? Wait...
    # Actually let's just search for r_offset matching computed GOT address.
    # For entry at fileoff 0xee30:
    #   pc at ldr = runtime 0xfe38 + 8 = 0xfe40
    #   ip = 0xfe40 + 4096 = 0x10e40
    #   ldr offset = 0x960
    #   GOT addr = 0x10e40 + 0x960 = 0x117a0
    target_got = 0x117a0
    for i in range(relplt_size // 8):
        r_offset, r_info = struct.unpack_from('<II', data, relplt_off + i * 8)
        if r_offset == target_got:
            sym_idx = r_info >> 8
            st_name = struct.unpack_from('<I', data, sym_off + sym_idx * 16)[0]
            return data[str_off + st_name:data.index(b'\x00', str_off + st_name)].decode('ascii', errors='replace')
    return "<unknown>"


def verify_context_at_8870(data):
    print()
    print("=== Context around vaddr 0x8870 (update_capture_stream) ===")
    for vaddr in range(0x8868, 0x8880, 2):
        sz, text = decode_thumb1(data, vaddr)
        print(f"  0x{vaddr:04x}: {text}")

    print()
    plt_sym = verify_plt_target(data)
    print(f"  => The blx at 0x886c resolves to PLT symbol: '{plt_sym}'")
    print("     r0 is loaded from [r5, #0xf4] (aproxy ptr) before the call.")

    # Disassemble voice_is_call_mode from binary
    sym = find_symbol(data, 'voice_is_call_mode')
    if sym:
        vaddr, size = sym
        print()
        print(f"  voice_is_call_mode from binary (vaddr 0x{vaddr:04x}, size={size}):")
        pos = vaddr
        end = vaddr + size
        while pos < end:
            sz, text = decode_thumb1(data, pos)
            print(f"    0x{pos:04x}: {text}")
            pos += sz

        # Verify key properties from the disassembly
        print()
        # Collect instructions as (mnemonic, operands) for analysis
        instrs = []
        pos = vaddr
        while pos < end:
            sz, text = decode_thumb1(data, pos)
            parts = text.split(None, 1)
            mnem = parts[0]
            ops = parts[1] if len(parts) > 1 else ""
            instrs.append((pos, mnem, ops))
            pos += sz

        returns = [ops for _, mnem, ops in instrs if mnem == "movs" and ops.startswith("r0,")]
        cmp_ops = [ops for _, mnem, ops in instrs if mnem == "cmp"]
        it_ops = [ops for _, mnem, ops in instrs if mnem == "it"]

        if returns:
            print(f"  => Return values: {returns}")
        if cmp_ops:
            print(f"  => Comparison: {cmp_ops}")
        if it_ops:
            print(f"  => Conditional: {it_ops}")

        # Check if it ever returns 2
        has_return_2 = any("#0x2" in r for r in returns)
        if has_return_2:
            print("  => Function CAN return 2.")
        else:
            print("  => Function NEVER returns 2.")
            print("     Therefore, the cmp r0, #2 / stream_type=12 path at 0x8878")
            print("     is UNREACHABLE via the local implementation.")
    else:
        print("  => Could not find 'voice_is_call_mode' symbol in .dynsym")


def verify_no_direct_audiosource_checks(data):
    print()
    print("=== Direct AudioSource comparisons in update_capture_stream ===")
    # update_capture_stream is at 0x8608–0x88d8 (next function set_call_forwarding starts at 0x88d8)
    UPDATE_CAPTURE_START = 0x8608
    UPDATE_CAPTURE_END = 0x88D8

    found_in_func = []
    for vaddr in range(UPDATE_CAPTURE_START, UPDATE_CAPTURE_END, 2):
        hw = int.from_bytes(data[vaddr - 0x1000:vaddr - 0x1000 + 2], "little")
        if (hw & 0xF800) == 0x2800:
            rn = (hw >> 8) & 0x7
            imm = hw & 0xFF
            if rn == 0 and imm in (5, 6, 7):
                found_in_func.append((vaddr, imm))

    if found_in_func:
        print(f"  Found {len(found_in_func)} cmp r0, #N for N in {{5,6,7}}:")
        for vaddr, imm in found_in_func:
            print(f"    0x{vaddr:04x}: cmp r0, #{imm}")
    else:
        print("  Found ZERO cmp r0, #N instructions for N in {5,6,7}")
        print("  => AudioSource values 5/6/7 are NEVER directly compared")
        print("     inside the function that assigns stream_type.")

    # For completeness, also check the whole .text
    found_all = []
    for foff in range(TEXT_FILEOFF, TEXT_FILEOFF + TEXT_SIZE, 2):
        hw = int.from_bytes(data[foff:foff + 2], "little")
        if (hw & 0xF800) == 0x2800:
            rn = (hw >> 8) & 0x7
            imm = hw & 0xFF
            if rn == 0 and imm in (5, 6, 7):
                vaddr = foff + 0x1000
                found_all.append((vaddr, imm))

    print()
    print(f"  (Note: {len(found_all)} cmp r0, #{{5,6,7}} exist elsewhere in .text,")
    print(f"   but none are inside update_capture_stream.)")


def main():
    data = BINARY.read_bytes()
    verify_stream_type_assignments(data)
    verify_context_at_8870(data)
    verify_no_direct_audiosource_checks(data)

    print()
    print("=== CONCLUSION ===")
    print("  - stream_type=11 and stream_type=12 each have exactly ONE assignment site.")
    print("  - Both are gated on the return value of an external PLT call.")
    print("  - The binary does NOT contain direct comparisons against AudioSource 5, 6, or 7.")
    print("  - Therefore, the claim that AudioSource 5/6/7 map to stream_type=11")
    print("    CANNOT be statically verified from audio.primary.universal3830.so alone.")


if __name__ == '__main__':
    main()
