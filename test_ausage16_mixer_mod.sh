#!/bin/bash
set -uo pipefail

# Test script: Patch AUSAGE=16 + modify mixer_paths.xml communication-handset-mic
# Hypothesis: the `route-apcall-mic` path in `communication-handset-mic` causes DAPM
# to power down the mic PGAs before capture starts. Replacing it with `route-ap-record`
# (which is simpler and doesn't route to VSS adapter / TXSE) might keep the mic PGAs up.
#
# WARNING: This modifies /vendor/etc/mixer_paths.xml on the device. The original is
# backed up and restored after the test.

VOICEMAIL_NUMBER="${1:-}"
if [ -z "$VOICEMAIL_NUMBER" ]; then
    echo "Usage: $0 <voicemail_number>"
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTDIR="/tmp/ims_test_mixer_mod_${TIMESTAMP}"
mkdir -p "$OUTDIR"

echo "=== AUSAGE=16 + mixer_paths.xml modification test ==="
echo "Target: $VOICEMAIL_NUMBER"
echo "Output: $OUTDIR"

# Check adb connectivity
if ! adb devices | grep -q "device$"; then
    echo "ERROR: No Android device connected via ADB"
    exit 1
fi

adb root > /dev/null 2>&1 || true

# --- Step 1: Patch libaudioproxy to AUSAGE=16 ---
echo "[1/7] Patching libaudioproxy.so to AUSAGE=16..."
python3 RE/scripts/patch_ausage_stream_type_11.py patch
adb shell "mount -o remount,rw /vendor" 2>/dev/null || true
adb push "RE/binaries/libaudioproxy_patched.so" "/vendor/lib/libaudioproxy.so"
adb shell "chmod 644 /vendor/lib/libaudioproxy.so"

# --- Step 2: Backup and modify mixer_paths.xml ---
echo "[2/7] Backing up and modifying mixer_paths.xml..."
adb pull "/vendor/etc/mixer_paths.xml" "$OUTDIR/mixer_paths.xml.backup"

# Modify: replace route-apcall-mic with route-ap-record in communication-handset-mic
python3 RE/scripts/modify_mixer_paths.py "$OUTDIR/mixer_paths.xml.backup" "$OUTDIR/mixer_paths.xml.modified"

adb push "$OUTDIR/mixer_paths.xml.modified" "/vendor/etc/mixer_paths.xml"
adb shell "chmod 644 /vendor/etc/mixer_paths.xml"

# --- Step 3: Reboot and wait ---
echo "[3/7] Rebooting device..."
adb reboot
sleep 5
while ! adb devices | grep -q "device$"; do sleep 2; done
adb root > /dev/null 2>&1 || true

BOOT_WAIT=60
while [ $BOOT_WAIT -gt 0 ]; do
    if adb shell "getprop sys.boot_completed" 2>/dev/null | grep -q "1"; then
        echo "      Device booted."
        break
    fi
    sleep 2
    BOOT_WAIT=$((BOOT_WAIT - 2))
done

# Wait for IMS registration
echo "[4/7] Waiting for IMS registration..."
IMS_WAIT=60
while [ $IMS_WAIT -gt 0 ]; do
    if adb shell "logcat -d -b all | grep -q 'IMS SIP registered'" 2>/dev/null; then
        echo "      IMS registered."
        break
    fi
    if adb shell "dumpsys telephony.registry | grep -q 'mImsRegistered.*true'" 2>/dev/null; then
        echo "      IMS registered (dumpsys)."
        break
    fi
    sleep 2
    IMS_WAIT=$((IMS_WAIT - 2))
done

# --- Step 4: Start logcat and make call ---
echo "[5/7] Starting logcat and dialing..."
adb logcat -c
adb logcat -b all > "$OUTDIR/app_log_raw.txt" &
LOGCAT_PID=$!
sleep 1

adb shell "am start -a android.intent.action.CALL -d 'tel:${VOICEMAIL_NUMBER}'"

# --- Step 5: Wait for call to connect ---
echo "[6/7] Waiting for call to connect..."
TIMEOUT=60
CONNECTED=0
while [ $TIMEOUT -gt 0 ]; do
    if grep -qE "callStarted|call connected|INVITE sip:|AudioRecord.*startRecording" "$OUTDIR/app_log_raw.txt" 2>/dev/null; then
        echo "      Call active!"
        CONNECTED=1
        break
    fi
    if grep -qE "SIP/2.0 486|SIP/2.0 480|SIP/2.0 404|SIP/2.0 500|callEnded|BYE sip:" "$OUTDIR/app_log_raw.txt" 2>/dev/null; then
        echo "      Call ended/rejected."
        break
    fi
    sleep 1
    TIMEOUT=$((TIMEOUT - 1))
done

if [ $CONNECTED -eq 0 ]; then
    echo "      WARNING: Timeout waiting for connection"
fi

echo "      Settling audio for 8s..."
sleep 8

# Collect ALSA status during call
for pcm in pcm12c pcm13c pcm14c pcm15c pcm16c pcm110c; do
    echo "--- $pcm ---" >> "$OUTDIR/alsa_status.txt"
    adb shell "cat /proc/asound/card0/${pcm}/sub0/status 2>/dev/null || echo 'not found'" >> "$OUTDIR/alsa_status.txt"
done

# Collect tinymix state
echo "--- tinymix ---" >> "$OUTDIR/mixer_state.txt"
adb shell "/data/local/tmp/tinymix -D 0" >> "$OUTDIR/mixer_state.txt" 2>/dev/null || true

# --- Step 6: Hang up and collect logs ---
echo "[7/7] Hanging up and collecting logs..."
adb shell "input keyevent KEYCODE_ENDCALL" > /dev/null 2>&1 || true
sleep 2

if kill -0 $LOGCAT_PID 2>/dev/null; then
    kill $LOGCAT_PID 2>/dev/null || true
    sleep 1
    kill -9 $LOGCAT_PID 2>/dev/null || true
fi
wait $LOGCAT_PID 2>/dev/null || true

# Filter key logs
grep -E "proxy_open_capture_stream|pcm_read error|Read Fail|mic_pga|vmid|dapm powering up|dapm powering down|abox_wdma_trigger|abox_wdma_open|communication-handset|Apply path|adev_set_route|in_read|in_standby" "$OUTDIR/app_log_raw.txt" > "$OUTDIR/app_log_filtered.txt" 2>/dev/null || true

# --- Step 7: Restore original files ---
echo ""
echo "Restoring original mixer_paths.xml..."
adb shell "mount -o remount,rw /vendor" 2>/dev/null || true
adb push "$OUTDIR/mixer_paths.xml.backup" "/vendor/etc/mixer_paths.xml"
adb shell "chmod 644 /vendor/etc/mixer_paths.xml"
echo "Restoring original libaudioproxy.so..."
adb push "RE/binaries/libaudioproxy.so" "/vendor/lib/libaudioproxy.so"
adb shell "chmod 644 /vendor/lib/libaudioproxy.so"

echo ""
echo "=== Test Complete ==="
echo "Output: $OUTDIR"
echo "  Raw log:      $OUTDIR/app_log_raw.txt"
echo "  Filtered log: $OUTDIR/app_log_filtered.txt"
echo "  ALSA status:  $OUTDIR/alsa_status.txt"
echo "  Mixer state:  $OUTDIR/mixer_state.txt"
echo ""
echo "Files restored. Reboot if you want a completely clean state."
