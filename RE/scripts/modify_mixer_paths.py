#!/usr/bin/env python3
"""Modify mixer_paths.xml: replace route-apcall-mic with route-ap-record
in communication-handset-mic."""
import argparse
import re
from pathlib import Path


def modify(xml_text):
    old = (
        '\t<path name="communication-handset-mic">\n'
        '\t\t<path name="dev-dual-mic" />\n'
        '\t\t<path name="set-call-wdma4-16bit-config" />\n'
        '\t\t<path name="route-apcall-mic" />\n'
        '\t\t<ctl name="ABOX Sound Type" value="VOICE" />\n'
        '\t</path>'
    )
    new = (
        '\t<path name="communication-handset-mic">\n'
        '\t\t<path name="dev-dual-mic" />\n'
        '\t\t<path name="route-ap-record" />\n'
        '\t</path>'
    )
    if old in xml_text:
        xml_text = xml_text.replace(old, new)
        print('Replaced route-apcall-mic with route-ap-record (exact match)')
        return xml_text

    # Fallback: regex
    pattern = (
        r'(<path name="communication-handset-mic">\s+'
        r'<path name="dev-dual-mic" />)\s+'
        r'<path name="set-call-wdma4-16bit-config" />\s+'
        r'<path name="route-apcall-mic" />\s+'
        r'<ctl name="ABOX Sound Type" value="VOICE" />\s+'
        r'(</path>)'
    )
    result, count = re.subn(pattern, r'\1\n\t\t<path name="route-ap-record" />\n\t\2', xml_text)
    if count:
        print(f'Replaced route-apcall-mic with route-ap-record (regex match, count={count})')
        return result

    print('WARNING: could not find communication-handset-mic pattern')
    return xml_text


def main():
    parser = argparse.ArgumentParser(description='Modify mixer_paths.xml for AP call mic test')
    parser.add_argument('input', help='Input mixer_paths.xml')
    parser.add_argument('output', help='Output modified mixer_paths.xml')
    args = parser.parse_args()

    xml = Path(args.input).read_text()
    modified = modify(xml)
    Path(args.output).write_text(modified)


if __name__ == '__main__':
    raise SystemExit(main())
