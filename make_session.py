# -*- coding: utf-8 -*-
"""
Run this ONCE on your PC (not on the phone):

    pip install pyrogram tgcrypto
    python make_session.py

It asks for api_id / api_hash (https://my.telegram.org -> API development tools), your phone number and the
login code. Paste the printed string into /sdcard/Jarvis/config.json as "tg_session_string".
"""
from pyrogram import Client

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()

with Client("tmp_session", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
    print("\n=== SESSION STRING (keep it secret, it is a full login to your account) ===\n")
    print(app.export_session_string())
    print("\n===========================================================================")
