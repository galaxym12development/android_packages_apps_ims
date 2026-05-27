#!/bin/bash
set -uo pipefail

# Comprehensive IMS audio test with Patch F v2 (AUSAGE=12, corrected)
# This script:
# 1. Pushes patched libaudioproxy.so to device
# 2. Reboots and waits for IMS registration
# 3. Makes a SIP call to voicemail
# 4. Collects audio diagnostics and tinymix state
# 5. Hangs up and retrieves logs
#
# Usage: ./test_ims_patch_f_v2.sh <voicemail_number>

VOICEMAIL_NUMBER="${1:-}"
if [ -z "$VOICEMAIL_NUMBER" ]; then
    echo "Usage: $0 <voicemail_number>"
    echo "Example: $0 +491793000000"
    echo "Note:  Replace with your own voicemail number"
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTDIR="/tmp/ims_test_${TIMESTAMP}"
mkdir -p "$OUTDIR"

LOGFILE="$OUTDIR/app_log_raw.txt"
DIAGFILE="$OUTDIR/audio_diag.txt"
MIXERFILE="$OUTDIR/mixer_state.txt"
SUMMARY="$OUTDIR/summary.txt"
APPTAGS="$OUTDIR/app_log.txt"

# Binary paths
PATCHED_PROXY="RE/binaries/libaudioproxy_patched.so"
DEVICE_PROXY="/vendor/lib/libaudioproxy.so"
TINYMIX="/data/local/tmp/tinymix"

# Check adb connectivity
if ! adb devices | grep -q "device$"; then
    echo "ERROR: No Android device connected via ADB"
    exit 1
fi

adb root > /dev/null 2>&1 || true

echo "=== IMS Patch F v2 Test ==="
echo "Target: $VOICEMAIL_NUMBER"
echo "Output: $OUTDIR"
echo ""

# --- Step 1: Push patched binary ---
echo "[1/8] Pushing patched libaudioproxy.so..."
adb shell "mount -o remount,rw /vendor" 2>/dev/null || true
adb push "$PATCHED_PROXY" "$DEVICE_PROXY"
adb shell "chmod 644 $DEVICE_PROXY"
echo "      Patched binary pushed."

# --- Step 2: Reboot and wait ---
echo "[2/8] Rebooting device..."
adb reboot
sleep 5

# Wait for device to come back
while ! adb devices | grep -q "device$"; do
    sleep 2
done
adb root > /dev/null 2>&1 || true

# Wait for boot completion
BOOT_WAIT=60
while [ $BOOT_WAIT -gt 0 ]; do
    if adb shell "getprop sys.boot_completed" 2>/dev/null | grep -q "1"; then
        echo "      Device booted."
        break
    fi
    sleep 2
    BOOT_WAIT=$((BOOT_WAIT - 2))
done
if [ $BOOT_WAIT -le 0 ]; then
    echo "ERROR: Device did not boot"
    exit 1
fi

# Wait for IMS registration
echo "[3/8] Waiting for IMS registration..."
IMS_WAIT=60
IMS_READY=0
while [ $IMS_WAIT -gt 0 ]; do
    if adb shell "logcat -d -b all | grep -q 'IMS SIP registered'" 2>/dev/null; then
        echo "      IMS registered."
        IMS_READY=1
        break
    fi
    if adb shell "dumpsys telephony.registry | grep -q 'mImsRegistered.*true'" 2>/dev/null; then
        echo "      IMS registered (dumpsys)."
        IMS_READY=1
        break
    fi
    sleep 2
    IMS_WAIT=$((IMS_WAIT - 2))
done
if [ $IMS_READY -eq 0 ]; then
    echo "WARNING: IMS not registered. Continuing anyway..."
fi

# --- Step 4: Baseline mixer state (before call) ---
echo "[4/8] Collecting baseline mixer state..."
adb shell "$TINYMIX -D 0" > "$MIXERFILE" 2>/dev/null || echo "tinymix not available" > "$MIXERFILE"

echo "      Baseline mixer state saved."

# --- Step 5: Start logcat and make call ---
echo "[5/8] Starting logcat and dialing..."
adb logcat -c
adb logcat -b all > "$LOGFILE" &
LOGCAT_PID=$!
sleep 1

adb shell "am start -a android.intent.action.CALL -d 'tel:${VOICEMAIL_NUMBER}'"

# --- Step 6: Wait for call to connect ---
echo "[6/8] Waiting for call to connect..."
TIMEOUT=60
CONNECTED=0
while [ $TIMEOUT -gt 0 ]; do
    if grep -qE "callStarted|call connected|INVITE sip:|AudioRecord.*startRecording" "$LOGFILE" 2>/dev/null; then
        echo "      Call active!"
        CONNECTED=1
        break
    fi
    if grep -qE "SIP/2.0 486|SIP/2.0 480|SIP/2.0 404|SIP/2.0 500|callEnded|BYE sip:" "$LOGFILE" 2>/dev/null; then
        echo "      Call ended/rejected."
        break
    fi
    sleep 1
    TIMEOUT=$((TIMEOUT - 1))
done

if [ $CONNECTED -eq 0 ]; then
    echo "      WARNING: Timeout waiting for connection"
fi

# Let audio settle
echo "      Settling audio for 5s..."
sleep 5

# --- Step 7: Collect diagnostics during call ---
echo "[7/8] Running audio diagnostics and collecting mixer state..."
# Both commands are wrapped with `timeout` on the device side because
# audio_diag.sh may contain an infinite loop.
adb shell "timeout 10 /data/local/tmp/audio_diag.sh" > "$DIAGFILE" 2>&1 || adb shell "timeout 8 /data/local/bin/audio_diag" > "$DIAGFILE" 2>&1 || true

# Collect ALSA PCM status
for pcm in pcm12c pcm13c pcm14c pcm15c pcm16c pcm110c; do
    echo "--- $pcm ---" >> "$DIAGFILE"
    adb shell "cat /proc/asound/card0/${pcm}/sub0/status 2>/dev/null || echo 'not found'" >> "$DIAGFILE"
    adb shell "cat /proc/asound/card0/${pcm}/sub0/hw_params 2>/dev/null || echo 'not found'" >> "$DIAGFILE"
done

# Collect mixer state during call
echo "" >> "$MIXERFILE"
echo "=== Mixer state DURING call ===" >> "$MIXERFILE"
adb shell "$TINYMIX -D 0" >> "$MIXERFILE" 2>/dev/null || true

# Look for WDMA0 source selectors specifically
echo "" >> "$MIXERFILE"
echo "=== WDMA0 / WDMA4 selectors ===" >> "$MIXERFILE"
adb shell "$TINYMIX -D 0 | grep -i 'WDMA'" >> "$MIXERFILE" 2>/dev/null || true

# Collect all adev_set_route logs
echo "" >> "$DIAGFILE"
echo "=== adev_set_route logs ===" >> "$DIAGFILE"
adb shell "logcat -d -b all | grep 'adev_set_route' | tail -20" >> "$DIAGFILE" 2>/dev/null || true

# --- Step 8: Hang up and stop logcat ---
echo "[8/8] Hanging up..."
adb shell "input keyevent KEYCODE_ENDCALL" > /dev/null 2>&1 || true
sleep 2

if kill -0 $LOGCAT_PID 2>/dev/null; then
    kill $LOGCAT_PID 2>/dev/null || true
    sleep 1
    kill -9 $LOGCAT_PID 2>/dev/null || true
fi
wait $LOGCAT_PID 2>/dev/null || true

# Filter app logs
grep -E "PHH SipHandler|PHH MmTelFeature|AudioRecord|AudioTrack|encode thread|decode thread|codec=|RTP packet|callStarted|callStopped|call connected|IMS uplink gain|allZero|totalPacketsSent|totalPacketsReceived|INVITE sip:|BYE sip:|SIP/2.0 200 OK|SIP/2.0 180|SIP/2.0 183|SIP/2.0 486|SIP/2.0 480|adev_set_route|primary_out|primary_in" "$LOGFILE" > "$APPTAGS" 2>/dev/null || true

# Generate summary
echo "=== IMS Patch F v2 Test Summary ===" > "$SUMMARY"
echo "Timestamp: $TIMESTAMP" >> "$SUMMARY"
echo "Target: $VOICEMAIL_NUMBER" >> "$SUMMARY"
echo "Connected: $CONNECTED" >> "$SUMMARY"
echo "" >> "$SUMMARY"

echo "--- ALSA pcm12c (WDMA0) ---" >> "$SUMMARY"
grep -A 15 "pcm12c:" "$DIAGFILE" | head -20 >> "$SUMMARY" 2>/dev/null || echo "pcm12c not in diag" >> "$SUMMARY"

echo "" >> "$SUMMARY"
echo "--- ALSA pcm110c (calliope) ---" >> "$SUMMARY"
grep -A 15 "pcm110c:" "$DIAGFILE" | head -20 >> "$SUMMARY" 2>/dev/null || echo "pcm110c not in diag" >> "$SUMMARY"

echo "" >> "$SUMMARY"
echo "--- WDMA0 source selectors ---" >> "$SUMMARY"
grep -i "WDMA0" "$MIXERFILE" | head -10 >> "$SUMMARY" 2>/dev/null || echo "No WDMA0 entries" >> "$SUMMARY"

echo "" >> "$SUMMARY"
echo "--- AudioRecord / Encode Thread Logs ---" >> "$SUMMARY"
grep -E "AudioRecord|Encode thread|IMS uplink gain|allZero|RTP.*sent|totalPacketsSent" "$APPTAGS" | head -30 >> "$SUMMARY" 2>/dev/null || echo "No AudioRecord logs" >> "$SUMMARY"

echo "" >> "$SUMMARY"
echo "--- adev_set_route logs ---" >> "$SUMMARY"
grep "adev_set_route" "$APPTAGS" | head -20 >> "$SUMMARY" 2>/dev/null || echo "No adev_set_route logs" >> "$SUMMARY"

echo ""
echo "=== Test Complete ==="
cat "$SUMMARY"
echo ""
echo "Full logs: $OUTDIR"
echo "  Raw log:     $LOGFILE"
echo "  App log:     $APPTAGS"
echo "  Audio diag:  $DIAGFILE"
echo "  Mixer state: $MIXERFILE"
echo "  Summary:     $SUMMARY"

# Also restore stock binary after test so device is clean
# (comment out if you want to keep the patch)
echo ""
echo "Restoring stock libaudioproxy.so..."
adb shell "mount -o remount,rw /vendor" 2>/dev/null || true
adb push "RE/binaries/libaudioproxy.so" "$DEVICE_PROXY"
adb shell "chmod 644 $DEVICE_PROXY"
echo "Stock binary restored. Reboot if you want to revert completely."
