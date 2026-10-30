import os

# Tests never download the meme sound pack or open files in other apps.
os.environ.setdefault("BF6KT_NO_SOUND_DOWNLOAD", "1")
os.environ.setdefault("BF6KT_NO_OPEN", "1")

# never let a developer's saved speed-test choice change how tests decode (CI keeps the default path)
if not os.environ.get("CI"):
    os.environ.setdefault("BF6KT_HWACCEL", "none")
