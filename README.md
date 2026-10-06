# phhusson/ims — VoLTE for LineageOS on Samsung devices

**Note:** For production use, prefer the [krazey-ims](https://github.com/krazey/ims) fork.
It has a better structure, more fixes, and is hardware-agnostic. This repo is kept
primarily as a guide for device tree integration and audio issue debugging on devices
with similar Samsung binaries.

Open-source SIP/IMS stack for LineageOS, based on [phhusson/ims](https://github.com/phhusson/ims).
Tested on Samsung Galaxy A21s (SM-A217F) running LineageOS 23.2 (Android 16) with O2 Germany.

## How it works

The Android telephony framework requires a privileged system app that implements
`android.telephony.ims.ImsService` and is registered as the MmTel provider. This app
provides a pure-userspace SIP/IMS stack: it opens the IMS bearer, discovers the P-CSCF,
performs SIP AKA registration, and handles voice calls over RTP.

## Prerequisites

After cloning (whether via `repo sync`, `git clone`, or roomservice), initialize
the `rnnoise` submodule — `repo sync` does not do this automatically:

```sh
cd packages/apps/PhhIms
git submodule update --init app/jni/rnnoise
```

## Building in-tree (LineageOS / AOSP)

Add this repo to your local manifest or `lineage.dependencies`:

```xml
<!-- .repo/local_manifests/roomservice.xml -->
<project path="packages/apps/PhhIms" remote="github" name="amikhasenko/ims" revision="main" />
```

```json
// lineage.dependencies
{
    "repository": "amikhasenko/ims",
    "target_path": "packages/apps/PhhIms",
    "branch": "main"
}
```

The `Android.bp` uses `platform_apis: true`, which gives access to all internal
framework APIs (`Rlog`, `MmTelFeature`, `ImsConfigImplBase`, etc.) without patching
`android.jar`. No Gradle build or SDK modification is needed.

## Device tree integration (Samsung Exynos example: A21s)

The following shows the full diff needed in your device tree. Adapt paths and package
names for your device.

### `common.mk` (or `device.mk`)

```makefile
# IMS over Wi-Fi data service and network qualification service.
# Required by the telephony framework even for VoLTE-only (no VoWiFi):
# without these, DataServiceManager and NetworkRegistrationManager fail
# to bind their WLAN handlers, which can cascade to IMS setup failures.
PRODUCT_PACKAGES += \
    Iwlan \
    QualifiedNetworksService

# Tell the framework VoLTE is available even if the carrier config says otherwise.
# Required because carrier config defaults to volte_available=false for unknown carriers.
PRODUCT_PROPERTY_OVERRIDES += \
    persist.dbg.volte_avail_ovr=1 \
    persist.dbg.wfc_avail_ovr=1 \
    persist.dbg.allow_ims_off=1

PRODUCT_PACKAGES += \
    PhhIms

PRODUCT_COPY_FILES += \
    $(COMMON_PATH)/privapp-permissions-me.phh.ims.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/permissions/privapp-permissions-me.phh.ims.xml
```

### `overlay/frameworks/base/core/res/res/values/config.xml`

```xml
<!-- Tell the platform VoLTE and VT are available on this device -->
<bool name="config_carrier_volte_available">true</bool>
<bool name="config_device_volte_available">true</bool>
<bool name="config_device_vt_available">true</bool>

<!-- IMS bearer management services -->
<string name="config_wlan_data_service_package">com.google.android.iwlan</string>
<string name="config_wlan_network_service_package">com.google.android.iwlan</string>
<string name="config_qualified_networks_service_package">com.android.telephony.qns</string>
```

### `overlay/packages/services/Telephony/res/values/config.xml`

```xml
<!-- Register me.phh.ims as the IMS MmTel provider -->
<string name="config_ims_mmtel_package" translatable="false">me.phh.ims</string>
```

### `privapp-permissions-me.phh.ims.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<permissions>
    <privapp-permissions package="me.phh.ims">
        <permission name="android.permission.READ_PRIVILEGED_PHONE_STATE"/>
        <permission name="android.permission.MODIFY_PHONE_STATE"/>
    </privapp-permissions>
</permissions>
```

### `sepolicy/vendor/property.te`

```
vendor_internal_prop(vendor_ims_prop)
```

### `sepolicy/vendor/property_contexts`

```
# IMS
vendor.ril.ims.                u:object_r:vendor_ims_prop:s0
```

### `sepolicy/vendor/sehradiomanager.te`

```
allow sehradiomanager vendor_ims_prop:property_service set;
```

### `configs/mixer/mixer_paths.xml` — the audio fix

During a SIP/VoLTE call the Samsung
audio HAL applies the `communication-handset-mic` path, which contains
`route-apcall-mic`. This path tells the ABox DSP to route mic audio to the
modem/VSS path. During a software IMS call there is no modem audio on this
path, so the captured audio is silent.

The fix is to replace `route-apcall-mic` with `route-ap-record` (the normal
capture path) in all `communication-*-mic` paths. This tells the DSP to keep
routing mic audio to `calliope_10` (pcm110c), which is where the HAL opens
the capture stream.

```diff
--- a/configs/mixer/mixer_paths.xml
+++ b/configs/mixer/mixer_paths.xml
@@ -1407,15 +1407,13 @@
 
 	<path name="communication-handset-mic">
 		<path name="dev-dual-mic" />
-		<path name="set-call-wdma4-16bit-config" />
-		<path name="route-apcall-mic" />
+		<path name="route-ap-record" />
 		<ctl name="ABOX Sound Type" value="VOICE" />
 	</path>
 
 	<path name="communication-speaker-mic">
 		<path name="dev-dual-invert-mic" />
-		<path name="set-call-wdma4-16bit-config" />
-		<path name="route-apcall-mic" />
+		<path name="route-ap-record" />
 		<ctl name="ABOX Sound Type" value="SPEAKER" />
 	</path>
 
@@ -1429,8 +1427,7 @@
 
 	<path name="communication-headset-mic">
 		<path name="dev-headset-mic" />
-		<path name="set-call-wdma4-16bit-config" />
-		<path name="route-apcall-mic" />
+		<path name="route-ap-record" />
 		<ctl name="ABOX Sound Type" value="HEADSET" />
 	</path>
```

Cellular calls use separate `incall-*` paths and are unaffected.

No binary patching or framework patches are needed.

## Required binary patches

**None.** Earlier versions required patching `libaudioproxy.so` to change the
ALSA device selection, but the real fix is in the mixer path configuration above.

## Required framework patches

**None.** Earlier versions required a patch in `packages/services/Telecomm`
to use `MODE_IN_COMMUNICATION` instead of `MODE_IN_CALL`. With the current
fork the framework correctly receives the IMS call state and sets the mode
appropriately. The mixer path fix alone is sufficient.

## Current status

| Feature | Status |
|---------|--------|
| Registration | Works (initial and periodic re-REGISTER both succeed) |
| Incoming SMS | Works |
| Outgoing SMS | Not tested |
| Incoming Calls | Fix for "dropped after accept" committed; needs device verification |
| Outgoing Calls | Works with mixer_paths.xml fix |
| Mic audio during call | Works with mixer_paths.xml fix |
| Video recording | Works (stock path, unaffected) |
| Voice Recorder | Works (stock path, unaffected) |

## Building with Gradle

The public `android.jar` (API 33) stubs do not expose the internal IMS APIs. To build
with Gradle you need a full `android.jar` built from AOSP sources in `app/libs/android.jar`,
and the SDK jar must have `MmTelFeature` removed to avoid duplicate class conflicts:

```sh
zip -d ./platforms/android-33/android.jar \
    android/telephony/ims/feature/MmTelFeature.class \
    'android/telephony/ims/feature/MmTelFeature$MmTelCapabilities.class'
```

For production builds use the in-tree Soong build instead.

## Notes

- The app has no launcher icon and does not appear in the app drawer.
- On carriers where the RIL does not report P-CSCF addresses via `LinkProperties`,
  DNS discovery is attempted using the standard 3GPP domain (TS 23.003 §13.2):
  `ims.mnc<MNC>.mcc<MCC>.3gppnetwork.org`. A last-resort override is available via
  `adb shell setprop persist.ims.pcscf_fallback <ip>`.
- VoLTE must be enabled via ADB once after installing:
  ```
  adb shell settings put global enhanced_4g_mode_enabled 1
  ```
  On a fresh install this defaults to on.
