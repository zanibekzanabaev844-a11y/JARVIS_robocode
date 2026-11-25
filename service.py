# -*- coding: utf-8 -*-
"""Background foreground-service entry point (started by main.py)."""
import time
import traceback

from jarvis_core import Jarvis, log

while True:                      # never let the service die on an unexpected error
    try:
        Jarvis().run_forever()
    except Exception:
        log("FATAL:", traceback.format_exc())
        time.sleep(5)
