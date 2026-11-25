# -*- coding: utf-8 -*-
"""Jarvis - Kivy UI. Requests permissions and starts/stops the background service."""
import os
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.utils import platform

BASE = "/sdcard/Jarvis"
LOG = BASE + "/jarvis.log"
BEAT = BASE + "/.heartbeat"

KV = """
<RoundButton@ButtonBehavior+Label>:
    bg: 0.13, 0.55, 0.95, 1
    canvas.before:
        Color:
            rgba: self.bg if self.state == 'normal' else (self.bg[0]*0.7, self.bg[1]*0.7, self.bg[2]*0.7, 1)
        Ellipse:
            pos: self.pos
            size: self.size

BoxLayout:
    orientation: 'vertical'
    padding: dp(24)
    spacing: dp(12)
    Label:
        text: 'J A R V I S'
        font_size: '30sp'
        bold: True
        color: 0.35, 0.78, 0.98, 1
        size_hint_y: None
        height: dp(70)
    Label:
        id: status
        text: 'Ready'
        font_size: '16sp'
        color: 0.75, 0.80, 0.88, 1
        size_hint_y: None
        height: dp(48)
        text_size: self.width, None
        halign: 'center'
    AnchorLayout:
        RoundButton:
            id: btn
            text: 'START'
            font_size: '34sp'
            bold: True
            size_hint: None, None
            size: dp(210), dp(210)
            on_release: app.toggle()
    Label:
        id: logbox
        text: ''
        font_size: '11sp'
        color: 0.55, 0.62, 0.72, 1
        halign: 'left'
        valign: 'top'
        text_size: self.width, self.height
        size_hint_y: 0.32
"""

BLUE = [0.13, 0.55, 0.95, 1]
RED = [0.90, 0.25, 0.30, 1]


class JarvisApp(App):
    title = "Jarvis"
    running = False

    # ------------------------------------------------------------------ UI
    def build(self):
        Window.clearcolor = (0.04, 0.06, 0.10, 1)
        root = Builder.load_string(KV)
        Clock.schedule_interval(self.tick, 1.0)
        return root

    def set_status(self, text):
        self.root.ids.status.text = text

    def tick(self, _dt):
        ids = self.root.ids
        try:
            with open(BEAT) as f:
                self.running = time.time() - float(f.read().strip()) < 8
        except Exception:
            self.running = False
        ids.btn.text = "STOP" if self.running else "START"
        ids.btn.bg = RED if self.running else BLUE
        if self.running:
            self.set_status("Jarvis is listening in the background.\nSay: «Жарвис» or «Шеф»")
        try:
            with open(LOG, encoding="utf-8") as f:
                ids.logbox.text = "".join(f.readlines()[-9:])
        except Exception:
            pass

    def toggle(self):
        if platform != "android":
            self.set_status("Run this on an Android phone.")
            return
        if self.running:
            self.stop_service()
        else:
            self.begin_start()

    # ------------------------------------------------------------------ permissions
    def activity(self):
        from jnius import autoclass
        return autoclass("org.kivy.android.PythonActivity").mActivity

    def begin_start(self):
        from android.permissions import Permission, request_permissions
        self.perms = [Permission.RECORD_AUDIO,
                      Permission.READ_EXTERNAL_STORAGE,
                      Permission.WRITE_EXTERNAL_STORAGE,
                      "android.permission.POST_NOTIFICATIONS"]
        self.set_status("Requesting permissions...")
        request_permissions(self.perms, self.on_perms)

    def on_perms(self, perms, grants):
        Clock.schedule_once(lambda dt: self.after_perms(dict(zip(perms, grants))), 0)

    def after_perms(self, granted):
        if not granted.get(self.perms[0]):
            self.set_status("Microphone permission is required.\nPress START again and allow it.")
            return
        if self.open_special_settings():
            return
        self.start_service()

    def open_special_settings(self):
        """Opens ONE missing special-access screen; returns True if the user must come back."""
        from jnius import autoclass
        Build = autoclass("android.os.Build$VERSION")
        Settings = autoclass("android.provider.Settings")
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        Context = autoclass("android.content.Context")
        act = self.activity()
        pkg = act.getPackageName()

        def open_screen(action, msg):
            i = Intent(action)
            i.setData(Uri.parse("package:" + pkg))
            act.startActivity(i)
            self.set_status(msg + "\nThen come back and press START again.")
            return True

        if Build.SDK_INT >= 30:
            Env = autoclass("android.os.Environment")
            if not Env.isExternalStorageManager():
                return open_screen(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                                   "Allow access to all files (for /Jarvis folder).")
        if not Settings.canDrawOverlays(act):
            return open_screen(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                               "Allow 'Display over other apps' (needed to go Home).")
        from jnius import cast
        pm = cast("android.os.PowerManager", act.getSystemService(Context.POWER_SERVICE))
        if not pm.isIgnoringBatteryOptimizations(pkg):
            return open_screen(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                               "Allow unrestricted battery use.")
        return False

    # ------------------------------------------------------------------ service
    def service_class(self):
        from jnius import autoclass
        return autoclass(self.activity().getPackageName() + ".ServiceJarvis")

    def start_service(self):
        try:
            os.makedirs(BASE + "/Presentations", exist_ok=True)
            os.makedirs(BASE + "/models", exist_ok=True)
        except Exception:
            pass
        self.service_class().start(self.activity(), "")
        self.set_status("Starting...")

    def stop_service(self):
        try:
            self.service_class().stop(self.activity())
        except Exception:
            pass
        try:
            os.remove(BEAT)
        except Exception:
            pass
        self.running = False
        self.set_status("Stopped")


if __name__ == "__main__":
    JarvisApp().run()
