[app]
title = Jarvis
package.name = jarvis
package.domain = org.jarvis
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = java,bin,.buildozer,venv
version = 1.0

# google-genai is OPTIONAL: it drags in pydantic-core (Rust) which often fails to build for Android.
# Without it the app calls the same Gemini model through plain REST (no extra dependencies).
# To try the SDK, append: ,google-genai,pydantic,httpx,anyio,sniffio,h11,httpcore,websockets,tenacity,google-auth
requirements = python3,kivy==2.3.0,pyjnius,android,requests,urllib3,idna,certifi,charset-normalizer,python-pptx,lxml,pillow,xlsxwriter,typing_extensions,pyrogram==2.0.106,pyaes,pysocks

orientation = portrait
fullscreen = 0

# --- Android ---------------------------------------------------------------
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.enable_androidx = True

android.permissions = RECORD_AUDIO,INTERNET,ACCESS_NETWORK_STATE,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,FOREGROUND_SERVICE,FOREGROUND_SERVICE_MICROPHONE,POST_NOTIFICATIONS,WAKE_LOCK,SYSTEM_ALERT_WINDOW,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS

# Java helper (mic + Vosk thread) and the Vosk / JNA libraries
android.add_src = java
android.gradle_dependencies = com.alphacephei:vosk-android:0.3.47,net.java.dev.jna:jna:5.13.0@aar

# Foreground service that keeps listening with the screen off
# (if your p4a version rejects "foregroundServiceType", delete that last part)
services = Jarvis:service.py:foreground:sticky:foregroundServiceType=microphone

[buildozer]
log_level = 2
warn_on_root = 1
