"""Prevent the music and connection-check bots from running together locally."""
import os
from pathlib import Path
import tempfile


def run_single_instance(main):
    # A shared per-user path also covers launches from another checkout.
    path = Path(tempfile.gettempdir()) / "musicbot_single_instance.lock"
    with path.open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("このPCでは既にBotが起動しています。重複起動を中止しました。")
            return
        # Closing the handle releases the lock, including on process termination.
        main()
