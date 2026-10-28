import os

# Tests never download the meme sound pack or open files in other apps.
os.environ.setdefault("BF6KT_NO_SOUND_DOWNLOAD", "1")
os.environ.setdefault("BF6KT_NO_OPEN", "1")
