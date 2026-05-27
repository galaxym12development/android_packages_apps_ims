#!/usr/bin/env python3
"""
Test script to experiment with AMR bit-repacking from MediaCodec encoder output.

Usage:
  python3 test_amr_repack.py

The script uses actual encoder data captured from logs and tries different
repacking strategies to produce valid RTP payload.
"""

# Raw encoder data from log: "Raw encoderData (96 bytes): 3c 54 fd 1f ..."
# This is 3 frames of 32 bytes each
ENCODER_DATA = bytes([
    0x3c, 0x54, 0xfd, 0x1f, 0xb6, 0x66, 0x79, 0xe1, 0xe0, 0x01, 0xe7, 0xcf, 0xf0, 0x00, 0x00, 0x00,
    0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x3c, 0x48, 0xf5, 0x1f, 0x96, 0x66, 0x79, 0xe1, 0xe0, 0x01, 0xe7, 0x8a, 0xf0, 0x00, 0x00, 0x00,
    0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x3c, 0x54, 0xfd, 0x1f, 0xb6, 0x66, 0x79, 0xe1, 0xe0, 0x01, 0xe7, 0xcf, 0xf0, 0x00, 0x00, 0x00,
    0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

# AMR-NB frame size to FT mapping (RFC 4867)
AMR_FRAME_SIZES = {
    12: 0,  # 4.75 kbps
    13: 1,  # 5.15 kbps
    14: 2,  # 5.9 kbps
    15: 3,  # 6.7 kbps
    16: 4,  # 7.4 kbps
    17: 5,  # 7.95 kbps
    18: 6,  # 10.2 kbps
    20: 6,  # 10.2 kbps (alternate)
    31: 7,  # 12.2 kbps
    32: 7,  # 12.2 kbps (padded)
}

def extract_ft(byte):
    """Extract frame type from encoder output byte (bits 3-6)."""
    return (byte >> 3) & 0xf

def repack_method_1(data, frame_size=32):
    """
    Original method: skip first byte, repack from index 1.
    Take 2 bits from current byte, 6 bits from next.
    """
    result = []
    for i in range(1, frame_size - 1):
        left = (data[i] & 0x03) << 6
        right = (data[i + 1] >> 2) & 0x3f
        result.append(left | right)
    return bytes(result)

def repack_method_2(data, frame_size=32):
    """
    Start from index 0 instead of 1.
    Take 2 bits from current byte, 6 bits from next.
    """
    result = []
    for i in range(0, frame_size - 1):
        left = (data[i] & 0x03) << 6
        right = (data[i + 1] >> 2) & 0x3f
        result.append(left | right)
    return bytes(result)

def repack_method_3(data, frame_size=32):
    """
    Try reading raw payload directly without repacking (just strip header byte).
    """
    return data[1:frame_size]

def repack_method_4(data, frame_size=32):
    """
    Direct copy of bytes 1-31 (31 bytes total, no bit manipulation).
    """
    return data[1:32]

def build_rtp_payload(payload, seq=100, ts=16000, ssrc=0x03000d20, pt=97, marker=False):
    """Build complete RTP packet with header."""
    rtp_header = bytes([
        0x80 | (0x80 if marker else 0x00),  # Version 2, marker bit
        pt,                                   # Payload type
        (seq >> 8) & 0xff, seq & 0xff,       # Sequence number
        (ts >> 24) & 0xff, (ts >> 16) & 0xff,
        (ts >> 8) & 0xff, ts & 0xff,         # Timestamp
        (ssrc >> 24) & 0xff, (ssrc >> 16) & 0xff,
        (ssrc >> 8) & 0xff, ssrc & 0xff,     # SSRC
    ])
    return rtp_header + payload

def analyze_output(output, label):
    """Print analysis of repacked output."""
    print(f"\n=== {label} ===")
    print(f"Output length: {len(output)} bytes")
    print(f"Hex: {output.hex()}")

    # Check if output looks like valid AMR
    # AMR at 12.2kbps should have ~31 bytes with data throughout
    non_zero = sum(1 for b in output if b != 0)
    print(f"Non-zero bytes: {non_zero}/{len(output)}")

    # First few bytes analysis
    if len(output) >= 2:
        print(f"First 4 bytes: {output[:4].hex()}")

    return output

def main():
    print("AMR Bit-Repacking Test Script")
    print("=" * 50)
    print(f"Input: {len(ENCODER_DATA)} bytes (3 frames of 32 bytes)")

    # Process each of the 3 frames
    for frame_idx in range(3):
        frame_start = frame_idx * 32
        frame_data = ENCODER_DATA[frame_start:frame_start + 32]

        print(f"\n{'='*50}")
        print(f"FRAME {frame_idx + 1}")
        print(f"Raw encoder data: {frame_data.hex()}")

        ft = extract_ft(frame_data[0])
        print(f"First byte: 0x{frame_data[0]:02x}, FT extracted: {ft} ({'12.2kbps' if ft == 7 else f'{ft}'} )")

        # Try different repacking methods
        analyze_output(repack_method_1(frame_data), "Method 1: Skip byte 0, repack 1..30")
        analyze_output(repack_method_2(frame_data), "Method 2: Start from 0, repack 0..30")
        analyze_output(repack_method_3(frame_data), "Method 3: Strip header, copy 1..31")
        analyze_output(repack_method_4(frame_data), "Method 4: Direct slice [1:32]")

        # Build and show full RTP packet for best method
        payload = repack_method_1(frame_data)
        rtp = build_rtp_payload(payload, seq=100 + frame_idx)
        print(f"\nFull RTP packet (method 1): {rtp.hex()}")

if __name__ == "__main__":
    main()
