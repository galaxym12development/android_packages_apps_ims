set -e
set -x
./gradlew assembleRelease
apksigner sign --key ./platform.pk8 --cert ./platform.x509.pem --out app/build/outputs/apk/release/app-release-signed.apk app/build/outputs/apk/release/app-release-unsigned.apk
adb root 
adb remount 
adb push app/build/outputs/apk/release/app-release-signed.apk /system/priv-app/PhhIms/PhhIms.apk
adb reboot
