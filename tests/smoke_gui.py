"""Run a short GUI construction smoke test against an unpacked release tree.

No recording, API request, configuration write, or resident background process.
"""

import logging
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_gui.py UNPACKED_RELEASE_DIR")
    root_dir = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root_dir))

    import tkinter as tk
    import app
    import manager_gui

    original_tk = tk.Tk

    def short_lived_tk(*args, **kwargs):
        root = original_tk(*args, **kwargs)
        root.after(900, root.destroy)
        return root

    tk.Tk = short_lived_tk
    with tempfile.TemporaryDirectory(prefix="recorder-release-smoke-") as temp:
        class FakeRecorder:
            cfg = app.default_config()
            out_dir = Path(temp)
            log = logging.getLogger("release-smoke")

            def get_status(self):
                return {
                    "enabled": False,
                    "display_text": "自动录音未开启",
                    "last_error": None,
                    "out_dir": str(self.out_dir),
                    "max_recordings_gb": 10.0,
                }

            def recording_is_enabled(self):
                return False

            def log_path(self):
                return self.out_dir / "recorder.log"

        gui = manager_gui.ManagerWindow(FakeRecorder(), start_minimized=True)
        gui.run()
    print("GUI 构建与事件循环通过")


if __name__ == "__main__":
    main()
