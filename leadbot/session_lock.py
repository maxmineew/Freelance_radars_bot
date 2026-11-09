"""One process at a time may open the observer's SQLite session."""
import os
import time


class SessionLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self, timeout=45):
        self.file = open(self.path, "a+b")
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    self.file = None
                    raise TimeoutError("Telegram-сессия занята другим процессом") from None
                time.sleep(0.2)

    def release(self):
        if self.file is not None:
            self.file.close()  # OS releases the lease, including after a process crash.
            self.file = None


class LockedClient:
    """Create TelegramClient only AFTER taking the session lease."""
    def __init__(self, factory, path):
        self.factory = factory
        self.lock = SessionLock(path)
        self.client = None

    def connect(self):
        if self.client is not None:
            return
        self.lock.acquire()
        try:
            self.client = self.factory()
            self.client.connect()
        except BaseException:
            self.disconnect()
            raise

    def disconnect(self):
        try:
            if self.client is not None:
                self.client.disconnect()
        finally:
            self.client = None
            self.lock.release()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def __call__(self, *args, **kwargs):
        return self.client(*args, **kwargs)

    def __enter__(self):
        self.connect()
        try:
            self.client.__enter__()
        except BaseException:
            self.disconnect()
            raise
        return self

    def __exit__(self, *args):
        self.disconnect()
