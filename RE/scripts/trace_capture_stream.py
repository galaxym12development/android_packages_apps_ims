#!/usr/bin/env python3
"""
trace_capture_stream.py — Dynamically trace proxy_create_capture_stream AUSAGE selection.

Reads TBH tables and literal pools directly from the binary.
Usage:
    python3 trace_capture_stream.py STREAM_TYPE
    python3 trace_capture_stream.py 11
"""
import struct, sys, argparse
from pathlib import Path

try:
    import capstone
except ImportError:
    capstone = None

BINARY = Path(__file__).parent.parent / "binaries" / "libaudioproxy.so"
TEXT_VADDR   = 0x7ab0
TEXT_FILEOFF = 0x6ab0

# Mapping of AUSAGE values set by movs r6, #imm at various targets
# These are discovered by reading the first instruction at each TBH target.
AUSAGE_FROM_CODE = {
    0x00: None,   # determined dynamically
    0x64: 0x64,
    0x65: 0x65,
    0x66: 0x66,
    0x67: 0x67,
    0x6e: 0x6e,
    0x70: 0x70,
    0x72: 0x72,
    0x73: 0x73,
}

def vaddr_to_fileoff(vaddr):
    return TEXT_FILEOFF + (vaddr - TEXT_VADDR)

def read_tbh_table(data, table_base_vaddr, n_entries):
    """Read a TBH table and return list of (idx, offset, target_vaddr)."""
    fileoff = vaddr_to_fileoff(table_base_vaddr)
    results = []
    for i in range(n_entries):
        val = struct.unpack_from('<H', data, fileoff + i * 2)[0]
        target = table_base_vaddr + val * 2
        results.append((i, val, target))
    return results

def decode_thumb_movs_imm(hw):
    """Decode a 16-bit Thumb MOV Rd, #imm instruction. Returns (rd, imm) or None."""
    if (hw & 0xF800) == 0x2000:
        rd = (hw >> 8) & 0x7
        imm = hw & 0xFF
        return (rd, imm)
    return None

def decode_thumb_b_imm(vaddr, hw):
    """Decode a 16-bit unconditional B <imm11>. Returns target or None."""
    if (hw & 0xF800) == 0xE000:
        imm11 = hw & 0x7FF
        if imm11 >= 0x400:
            imm11 -= 0x800
        return (vaddr + 4 + imm11 * 2) & 0xFFFFFFFF
    return None

def get_first_ausage(data, vaddr):
    """Look at first few instructions starting at vaddr to find movs r6, #imm."""
    fileoff = vaddr_to_fileoff(vaddr)
    pos = fileoff
    # Check up to 8 instructions deep (common pattern: movs r6, #X; b <epilogue>)
    for _ in range(8):
        if pos + 2 > len(data):
            break
        hw = struct.unpack_from('<H', data, pos)[0]
        mov = decode_thumb_movs_imm(hw)
        if mov and mov[0] == 6:  # r6
            return mov[1]
        # Skip 2 or 4 bytes
        is32 = (hw >> 11) in (0x1d, 0x1e, 0x1f)
        if is32 and pos + 4 <= len(data):
            pos += 4
        else:
            pos += 2
    return None

def trace_laddrop_pool(data, ldr_vaddr, add_vaddr):
    """
    For a sequence: ldr r0,[pc,#imm] ... add r0,pc
    Calculate the final r0 address (the pointer to the pcm_config pointer).
    Returns (pool_vaddr, pool_value, final_r0_vaddr).
    """
    fileoff = vaddr_to_fileoff(ldr_vaddr)
    hw = struct.unpack_from('<H', data, fileoff)[0]
    if (hw & 0xF800) != 0x4800:
        return None, None, None
    rt = (hw >> 8) & 0x7
    imm8 = hw & 0xFF
    pc_aligned = (ldr_vaddr + 4) & ~3
    pool_vaddr = pc_aligned + imm8 * 4
    pool_fileoff = vaddr_to_fileoff(pool_vaddr)
    if pool_fileoff + 4 > len(data):
        return pool_vaddr, None, None
    pool_val = struct.unpack_from('<I', data, pool_fileoff)[0]
    # ADD r0, pc at add_vaddr
    # Thumb-1 ADD Rd, PC uses PC = (instruction_address + 4) with bit[1]=0
    # (i.e., & ~1, not & ~3 as for LDR PC)
    pc_for_add = (add_vaddr + 4) & ~1
    final_r0 = (pool_val + pc_for_add) & 0xFFFFFFFF
    return pool_vaddr, pool_val, final_r0

def resolve_got_symbol(binary_path, got_vaddr):
    """Use LIEF if available to resolve a GOT entry to its symbol name."""
    try:
        import lief
        binary = lief.parse(str(binary_path))
        for r in binary.dynamic_relocations:
            if r.address == got_vaddr:
                if r.symbol:
                    return r.symbol.name
                # For R_ARM_RELATIVE, return the addend or a descriptive string
                if hasattr(r, 'addend'):
                    return f"R_ARM_RELATIVE(addend={r.addend})"
        return None
    except Exception:
        return None

def main():
    p = argparse.ArgumentParser(description="Trace proxy_create_capture_stream control flow")
    p.add_argument("stream_type", type=int, help="Stream type (e.g., 11 for VOICE_COMM)")
    p.add_argument("--verify", action="store_true", help="Verify hardcoded claims from previous session")
    args = p.parse_args()

    data = BINARY.read_bytes()
    stream_type = args.stream_type

    # ---- Outer TBH1 at 0x9f24 ----
    tbh1_base = 0x9f24
    tbh1 = read_tbh_table(data, tbh1_base, 7)
    outer_idx = stream_type - 0xa

    print(f"=== Outer TBH1 @ 0x{tbh1_base:06x} (stream_type - 0xa) ===")
    for idx, off, target in tbh1:
        marker = " <--" if idx == outer_idx else ""
        print(f"  [{idx}] offset=0x{off:04x} -> target 0x{target:06x}{marker}")

    if outer_idx < 0 or outer_idx >= len(tbh1):
        print(f"\nstream_type={stream_type}: out of range for TBH1")
        sys.exit(1)

    target1 = tbh1[outer_idx][2]
    print(f"\n=> TBH1[{outer_idx}] -> 0x{target1:06x}")

    # ---- If target1 == 0xa094 (stream_type=11), read inner TBH6 ----
    if target1 == 0x00a094:
        # The code at 0xa094 loads [r8], subtracts 1, and indexes TBH6.
        # TBH6 base is 0xa0b0.
        tbh6_base = 0xa0b0
        tbh6 = read_tbh_table(data, tbh6_base, 16)
        inner_idx = stream_type - 1  # confirmed by disasm: ldr r0,[r8]; subs r0,#1

        print(f"\n=== Inner TBH6 @ 0x{tbh6_base:06x} (stream_type - 1) ===")
        for idx, off, target in tbh6:
            ausage = get_first_ausage(data, target)
            marker = " <--" if idx == inner_idx else ""
            ausage_str = f" AUSAGE=0x{ausage:02x}" if ausage is not None else ""
            print(f"  [{idx:2d}] offset=0x{off:04x} -> target 0x{target:06x}{ausage_str}{marker}")

        if inner_idx < 0 or inner_idx >= len(tbh6):
            print(f"\ninner_idx={inner_idx}: out of range for TBH6")
            sys.exit(1)

        target2 = tbh6[inner_idx][2]
        ausage = get_first_ausage(data, target2)
        print(f"\n=> TBH6[{inner_idx}] -> 0x{target2:06x}")
        if ausage is not None:
            print(f"   First instruction sets AUSAGE (r6) = 0x{ausage:02x} ({ausage})")
        else:
            print(f"   No immediate movs r6 found at target (may be set earlier)")

        # Trace where target2 branches to (usually an epilogue)
        print(f"\n=== Tracing branches from 0x{target2:06x} ===")
        fileoff = vaddr_to_fileoff(target2)
        pos = fileoff
        for _ in range(6):
            if pos + 2 > len(data):
                break
            hw = struct.unpack_from('<H', data, pos)[0]
            cur_vaddr = TEXT_VADDR + (pos - TEXT_FILEOFF)
            mov = decode_thumb_movs_imm(hw)
            if mov and mov[0] == 6:
                print(f"  0x{cur_vaddr:06x}: movs r6, #0x{mov[1]:02x} ({mov[1]})")
            elif (hw & 0xF800) == 0x4800:
                rt = (hw >> 8) & 0x7
                imm8 = hw & 0xFF
                pc_a = (cur_vaddr + 4) & ~3
                pool = pc_a + imm8 * 4
                print(f"  0x{cur_vaddr:06x}: ldr r{rt}, [pc, #0x{imm8*4:x}]  ; pool=0x{pool:06x}")
            elif (hw & 0xFFC0) == 0x4440:
                rd = hw & 0xF
                rm = (hw >> 3) & 0xF
                if rm == 15:
                    print(f"  0x{cur_vaddr:06x}: add r{rd}, pc")
                else:
                    print(f"  0x{cur_vaddr:06x}: add r{rd}, r{rm}")
            else:
                b_target = decode_thumb_b_imm(cur_vaddr, hw)
                if b_target is not None:
                    print(f"  0x{cur_vaddr:06x}: b 0x{b_target:06x}")
                elif (hw & 0xF800) == 0xD000:
                    cond = (hw >> 8) & 0xF
                    imm8 = hw & 0xFF
                    if imm8 >= 0x80:
                        imm8 -= 0x100
                    bt = (cur_vaddr + 4 + imm8 * 2) & 0xFFFFFFFF
                    cc = ['eq','ne','cs','cc','mi','pl','vs','vc','hi','ls','ge','lt','gt','le','al','nv'][cond]
                    print(f"  0x{cur_vaddr:06x}: b{cc} 0x{bt:06x}")
                else:
                    is32 = (hw >> 11) in (0x1d, 0x1e, 0x1f)
                    if is32 and pos + 4 <= len(data):
                        hw2 = struct.unpack_from('<H', data, pos + 2)[0]
                        if (hw & 0xF800) == 0xF000 and (hw2 & 0xD000) == 0xD000:
                            s = (hw >> 10) & 1
                            i1 = 1 ^ (s ^ ((hw2 >> 13) & 1))
                            i2 = 1 ^ (s ^ ((hw2 >> 11) & 1))
                            imm = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
                            if s:
                                imm |= (-1 << 25)
                            bt = (cur_vaddr + 4 + imm) & 0xFFFFFFFF
                            if hw2 & 0x1000:
                                print(f"  0x{cur_vaddr:06x}: bl 0x{bt:06x}")
                            else:
                                print(f"  0x{cur_vaddr:06x}: blx 0x{bt:06x}")
                        elif (hw & 0xF800) == 0xF000 and (hw2 & 0xF000) in (0x8000, 0x9000, 0xa000, 0xb000):
                            s = (hw >> 10) & 1
                            j1 = (hw2 >> 13) & 1
                            j2 = (hw2 >> 11) & 1
                            imm = (s << 20) | (j2 << 19) | (j1 << 18) | ((hw & 0x3F) << 12) | ((hw2 & 0x7FF) << 1)
                            if s:
                                imm |= (-1 << 21)
                            bt = (cur_vaddr + 4 + imm) & 0xFFFFFFFF
                            print(f"  0x{cur_vaddr:06x}: b.w 0x{bt:06x}")
                        else:
                            print(f"  0x{cur_vaddr:06x}: .word32 0x{hw:04x}{hw2:04x}")
                        pos += 2  # extra 2 for 32-bit
                    else:
                        print(f"  0x{cur_vaddr:06x}: .hword 0x{hw:04x}")

            is32 = (hw >> 11) in (0x1d, 0x1e, 0x1f)
            pos += 4 if is32 and pos + 4 <= len(data) else 2

        # ---- Resolve literal pool -> pcm_config pointer ----
        # Find the branch target (epilogue) and trace its LDR+ADD+load chain
        print(f"\n=== Resolving pcm_config pointer for epilogue ===")
        # Follow the first unconditional branch from target2 to find epilogue
        epilogue = None
        pos = vaddr_to_fileoff(target2)
        for _ in range(6):
            if pos + 2 > len(data):
                break
            hw = struct.unpack_from('<H', data, pos)[0]
            cur_vaddr = TEXT_VADDR + (pos - TEXT_FILEOFF)
            b_target = decode_thumb_b_imm(cur_vaddr, hw)
            if b_target is not None:
                epilogue = b_target
                print(f"   Branch from 0x{cur_vaddr:06x} -> epilogue 0x{epilogue:06x}")
                break
            is32 = (hw >> 11) in (0x1d, 0x1e, 0x1f)
            pos += 4 if is32 and pos + 4 <= len(data) else 2

        if epilogue:
            # Disassemble epilogue with Capstone for precision
            if capstone:
                md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_LITTLE_ENDIAN)
                md.detail = False
                epi_fileoff = vaddr_to_fileoff(epilogue)
                epi_code = data[epi_fileoff:epi_fileoff + 24]
                print(f"\n   Epilogue disassembly:")
                ldr_vaddr = None
                add_vaddr = None
                for insn in md.disasm(epi_code, epilogue):
                    print(f"     0x{insn.address:06x}: {insn.mnemonic} {insn.op_str}")
                    if insn.mnemonic == 'ldr' and 'pc' in insn.op_str and ldr_vaddr is None:
                        ldr_vaddr = insn.address
                    elif insn.mnemonic == 'add' and 'pc' in insn.op_str and add_vaddr is None:
                        add_vaddr = insn.address
                if ldr_vaddr and add_vaddr:
                    pool_vaddr, pool_val, final_r0 = trace_laddrop_pool(data, ldr_vaddr, add_vaddr)
                    print(f"\n   Literal pool at 0x{pool_vaddr:06x} = 0x{pool_val:08x}")
                    print(f"   ADD PC -> r0 = 0x{final_r0:08x}")
                    sym = resolve_got_symbol(BINARY, final_r0)
                    if sym:
                        print(f"   Symbol: {sym}")
                    else:
                        print(f"   (No LIEF symbol at this address)")

                    # Also read the pcm_config pointer value itself
                    ptr_fileoff = vaddr_to_fileoff(final_r0)
                    if ptr_fileoff + 4 <= len(data):
                        pcm_ptr = struct.unpack_from('<I', data, ptr_fileoff)[0]
                        print(f"   Value at 0x{final_r0:08x} = 0x{pcm_ptr:08x} (unrelocated)")
            else:
                print("   (Install capstone for epilogue disassembly)")

    elif target1 == 0x00a06a:  # fast path for stream_type=10
        print("\n=> Fast path for stream_type=10")
        ausage = get_first_ausage(data, 0x00a06a)
        if ausage:
            print(f"   Sets AUSAGE = 0x{ausage:02x} ({ausage})")
        # Trace LDR+ADD
        if capstone:
            md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_LITTLE_ENDIAN)
            md.detail = False
            code = data[vaddr_to_fileoff(0x00a06a):vaddr_to_fileoff(0x00a06a)+20]
            print("   Disassembly:")
            ldr_vaddr = None
            add_vaddr = None
            for insn in md.disasm(code, 0x00a06a):
                print(f"     0x{insn.address:06x}: {insn.mnemonic} {insn.op_str}")
                if insn.mnemonic == 'ldr' and 'pc' in insn.op_str and ldr_vaddr is None:
                    ldr_vaddr = insn.address
                elif insn.mnemonic == 'add' and 'pc' in insn.op_str and add_vaddr is None:
                    add_vaddr = insn.address
            if ldr_vaddr and add_vaddr:
                pool_vaddr, pool_val, final_r0 = trace_laddrop_pool(data, ldr_vaddr, add_vaddr)
                print(f"\n   Literal pool at 0x{pool_vaddr:06x} = 0x{pool_val:08x}")
                print(f"   ADD PC -> r0 = 0x{final_r0:08x}")
                sym = resolve_got_symbol(BINARY, final_r0)
                if sym:
                    print(f"   Symbol: {sym}")
    else:
        ausage = get_first_ausage(data, target1)
        print(f"\n=> Target 0x{target1:06x}")
        if ausage:
            print(f"   Sets AUSAGE = 0x{ausage:02x} ({ausage})")
        else:
            print(f"   (No immediate movs r6 at target — examine manually)")

if __name__ == '__main__':
    main()
