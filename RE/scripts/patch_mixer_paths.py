#!/usr/bin/env python3
"""
patch_mixer_paths.py — Patch mixer_paths.xml for SIP/VoIP mic audio on Samsung A21s.

Problem: During SIP calls, the HAL applies `communication-handset-mic` which uses
`route-apcall-mic`. This path routes mic audio through VSS_TXADAPTER → TXSE
(modem path), causing the ABox DSP to stop feeding mic audio to calliope_10.
Result: pcm110c (the capture device) returns zeros.

Fix: Replace `route-apcall-mic` with `route-ap-record` (the normal capture path)
in all `communication-*-mic` paths. This tells the DSP "this is normal capture",
so mic audio continues to flow through calliope_10.

Paths modified:
- communication-handset-mic
- communication-speaker-mic
- communication-headset-mic

These paths are ONLY for AP (software) calls. Cellular calls use separate
`incall-*` paths and are unaffected.
"""

import sys
import xml.etree.ElementTree as ET

def patch(src_path, dst_path):
    tree = ET.parse(src_path)
    root = tree.getroot()

    modified_paths = []
    for path_name in ["communication-handset-mic", "communication-speaker-mic",
                       "communication-headset-mic"]:
        for path in root.findall(f'.//path[@name="{path_name}"]'):
            # Remove set-call-wdma4-16bit-config
            for ctl in list(path.findall("ctl")):
                if ctl.get("name") == "set-call-wdma4-16bit-config":
                    path.remove(ctl)
            # Replace route-apcall-mic with route-ap-record
            for subpath in path.findall("path"):
                if subpath.get("name") == "route-apcall-mic":
                    subpath.set("name", "route-ap-record")
                    modified_paths.append(path_name)

    if not modified_paths:
        print("WARNING: No paths were modified", file=sys.stderr)

    tree.write(dst_path, encoding="UTF-8", xml_declaration=True)
    print(f"Modified paths: {', '.join(set(modified_paths))}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <src_mixer_paths.xml> <dst_mixer_paths.xml>")
        sys.exit(1)
    patch(sys.argv[1], sys.argv[2])
