"""PyInstaller entry point: no arguments opens the app, otherwise the command line.

The exe is built without a console window (so double-clicking it doesn't flash
one). When it's run from a terminal with arguments, stdout/stderr are
re-attached to the terminal - or to whatever the output was redirected to.
"""
import multiprocessing
import os
import sys


def _attach_console() -> None:
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return
    import ctypes
    import msvcrt

    k32 = ctypes.windll.kernel32
    k32.AttachConsole(-1)   # parent terminal, if any (fails harmlessly otherwise)
    for num, name in ((-11, "stdout"), (-12, "stderr")):
        if getattr(sys, name) is not None:
            continue
        h = k32.GetStdHandle(num)
        stream = None
        if h and h != -1:
            try:
                stream = os.fdopen(msvcrt.open_osfhandle(h, os.O_WRONLY), "w", buffering=1,
                                   encoding="utf-8", errors="replace")
            except OSError:
                stream = None
        if stream is None:
            try:
                stream = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
            except OSError:
                stream = open(os.devnull, "w")
        setattr(sys, name, stream)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        _attach_console()
    from bf6killtracker.cli import main
    sys.exit(main())
