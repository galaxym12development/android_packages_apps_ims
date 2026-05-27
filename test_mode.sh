#!/system/bin/sh
# test_mode.sh — controlled audio-mode test, no real call needed

LOG=/data/local/tmp/mode_test.log
rm -f $LOG

echo "=== $(date) ===" >> $LOG

# 1. Set mode to IN_COMMUNICATION (same as during SIP call)
echo "[1] Setting MODE_IN_COMMUNICATION..." >> $LOG
cmd audio set-mode 3

# 2. Verify mode was set
echo "[2] Current audio mode:" >> $LOG
dumpsys media.audio_policy | grep -i "mode" | head -5 >> $LOG

# 3. Dump ALSA state BEFORE opening recorder
echo "[3] ALSA state BEFORE recording:" >> $LOG
for dev in pcm12c pcm13c pcm14c pcm15c pcm16c; do
    status=$(cat /proc/asound/card0/$dev/sub0/status 2>/dev/null)
    [ "$status" = "closed" ] && continue
    rate=$(cat /proc/asound/card0/$dev/sub0/hw_params 2>/dev/null | grep rate)
    echo "  $dev: $status | $rate" >> $LOG
done
[ $? -eq 0 ] && echo "  (WDMA capture paths checked)" >> $LOG

# Also check calliope paths
echo "[3b] Calliope paths:" >> $LOG
for dev in $(ls /proc/asound/card0/ | grep '^pcm1[0-9][0-9]c$'); do
    status=$(cat /proc/asound/card0/$dev/sub0/status 2>/dev/null)
    [ "$status" = "closed" ] && continue
    rate=$(cat /proc/asound/card0/$dev/sub0/hw_params 2>/dev/null | grep rate)
    echo "  $dev: $status | $rate" >> $LOG
done

# 4. Dump full audio policy state
echo "[4] Audio policy inputs:" >> $LOG
dumpsys media.audio_policy | grep -A3 "input" | head -20 >> $LOG

# 5. Reset to NORMAL
echo "[5] Resetting MODE_NORMAL..." >> $LOG
cmd audio set-mode 0

echo "Done. Log saved to $LOG"
cat $LOG
