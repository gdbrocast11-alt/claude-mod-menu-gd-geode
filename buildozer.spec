[app]
title = FRETSTORM
package.name = fretstorm
package.domain = org.fretstorm
source.dir = .
source.include_exts = py
source.exclude_dirs = bin,.buildozer,.git,__pycache__
version = 1.0.0

# python3 + kivy; pyjnius is only used (optionally) for vibration.
requirements = python3,kivy==2.3.0,pyjnius

orientation = landscape
fullscreen = 1

# VIBRATE is the only permission, used for the optional haptics setting.
# Remove it to ship with zero permissions (vibration then disables itself).
android.permissions = VIBRATE

android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = False
android.logcat_filters = *:S python:D

[buildozer]
log_level = 2
warn_on_root = 1
