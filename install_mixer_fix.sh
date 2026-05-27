#!/bin/bash
set -uo pipefail

# Install script: Apply the mixer_paths.xml fix for SIP/VoIP mic audio.
# This modifies /vendor/etc/mixer_paths.xml on the device.
# Run without arguments to install, or with 'restore' to revert.

MIXER_SRC="/vendor/etc/mixer_paths.xml"
MIXER_BACKUP="/vendor/etc/mixer_paths.xml.stock"
PATCH_SCRIPT="RE/scripts/patch_mixer_paths.py"

check_device() {
    if ! adb devices | grep -q "device$"; then
        echo "ERROR: No Android device connected via ADB"
        exit 1
    fi
    adb root > /dev/null 2>&1 || true
}

install() {
    echo "=== Installing mixer_paths.xml fix ==="
    check_device

    # Pull current mixer_paths.xml
    local tmpdir=$(mktemp -d)
    adb pull "$MIXER_SRC" "$tmpdir/mixer_paths.xml.orig" > /dev/null

    # Patch it
    python3 "$PATCH_SCRIPT" "$tmpdir/mixer_paths.xml.orig" "$tmpdir/mixer_paths.xml.patched"

    # Mount /vendor rw
    adb shell "mount -o remount,rw /vendor" 2>/dev/null || true

    # Save backup on device if not already present
    adb shell "if [ ! -f $MIXER_BACKUP ]; then cp $MIXER_SRC $MIXER_BACKUP; fi" 2>/dev/null || true

    # Push the patched version
    adb push "$tmpdir/mixer_paths.xml.patched" "$MIXER_SRC" > /dev/null
    adb shell "chmod 644 $MIXER_SRC"

    rm -rf "$tmpdir"
    echo "Done. Reboot the device for the fix to take effect."
    echo ""
    echo "To verify after reboot:"
    echo "  adb shell \"/data/local/tmp/tinymix -D 0 'ABOX Sound Type'\""
    echo "  adb shell 'cat /proc/asound/card0/pcm110c/sub0/status'"
    echo ""
    echo "During a SIP call, pcm110c should show hw_ptr > 0."
}

restore() {
    echo "=== Restoring stock mixer_paths.xml ==="
    check_device

    if adb shell "[ -f $MIXER_BACKUP ]" 2>/dev/null; then
        adb shell "mount -o remount,rw /vendor" 2>/dev/null || true
        adb shell "cp $MIXER_BACKUP $MIXER_SRC"
        adb shell "chmod 644 $MIXER_SRC"
        echo "Restored from $MIXER_BACKUP. Reboot for clean state."
    else
        echo "WARNING: No backup found at $MIXER_BACKUP"
        echo "You can pull a fresh copy from another device running stock ROM."
    fi
}

case "${1:-install}" in
    install)
        install
        ;;
    restore)
        restore
        ;;
    *)
        echo "Usage: $0 [install|restore]"
        exit 1
        ;;
esac
