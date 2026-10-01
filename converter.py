"""MP3 转换模块：单工作线程的转换队列，限制并发、失败保留源文件。

- 队列容量有限（默认 4），多通电话集中结束时不会堆积大量任务。
- 转换失败自动重试 2 次；仍失败则保留临时 WAV 并通知上层。
- 转换成功且校验可解码后删除临时 WAV。
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Full, Queue

import imageio_ffmpeg

logger = logging.getLogger("recorder.converter")


def ffmpeg_exe() -> str:
    base = Path(imageio_ffmpeg.__file__).parent / "binaries"
    return str(next(base.glob("ffmpeg-win*.exe")))


def convert(wav_path: Path, mp3_path: Path, bitrate: str) -> bool:
    """执行一次转换并做解码校验。"""
    ff = ffmpeg_exe()
    if mp3_path.exists():
        raise FileExistsError(f"拒绝覆盖已有录音: {mp3_path}")
    target = mp3_path
    mp3_path = target.with_name(target.stem + ".partial.mp3")
    flags = subprocess.CREATE_NO_WINDOW
    r = subprocess.run(
        [ff, "-nostdin", "-v", "error", "-y", "-i", str(wav_path),
         "-codec:a", "libmp3lame", "-b:a", bitrate, str(mp3_path)],
        capture_output=True, creationflags=flags)
    if r.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size < 100:
        logger.error("MP3 转换失败 rc=%s stderr=%s", r.returncode,
                     (r.stderr or b"")[-500:].decode("utf-8", "ignore"))
        if mp3_path.exists():
            mp3_path.unlink(missing_ok=True)
        return False
    check = subprocess.run(
        [ff, "-v", "error", "-i", str(mp3_path), "-f", "null", "-"],
        capture_output=True, creationflags=flags)
    if check.returncode != 0:
        logger.error("MP3 解码校验失败，删除损坏输出")
        mp3_path.unlink(missing_ok=True)
        return False
    mp3_path.rename(target)
    return True


class Converter:
    """后台转换队列。on_result(success, wav, mp3, error) 在工作线程回调。"""

    def __init__(self, bitrate: str, max_queue: int = 4,
                 on_result=None, on_state=None):
        self.bitrate = bitrate
        self.queue: Queue = Queue(maxsize=max_queue)
        self.on_result = on_result
        self.on_state = on_state  # on_state(pending_count) 用于状态显示
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="converter")
        self._thread.start()

    def submit(self, wav_path: Path, mp3_path: Path) -> bool:
        if self._stop.is_set():
            return False
        try:
            self.queue.put_nowait((wav_path, mp3_path, 0))
            self._report()
            return True
        except Full:
            logger.error("转换队列已满，本任务被拒绝（临时文件保留）: %s", wav_path)
            return False

    def pending(self) -> int:
        return self.queue.qsize()

    def stop(self, wait_seconds: float = 30.0) -> None:
        self._stop.set()
        self._thread.join(timeout=wait_seconds)

    def _report(self):
        if self.on_state:
            try:
                self.on_state(self.pending())
            except Exception:
                pass

    def _worker(self):
        while not self._stop.is_set() or not self.queue.empty():
            try:
                wav, mp3, attempt = self.queue.get(timeout=0.5)
            except Empty:
                continue
            if self.on_state:
                self.on_state(-1)  # -1 表示正在转换
            ok = False
            for attempt in range(3):
                try:
                    ok = convert(wav, mp3, self.bitrate)
                except Exception:
                    logger.exception("转换异常，源音频保留")
                    ok = False
                if ok:
                    break
                time.sleep(2 * (attempt + 1))
            if ok:
                try:
                    wav.unlink()
                except OSError:
                    pass
                logger.info("已保存 %s", mp3)
            else:
                logger.error("MP3 转换最终失败，保留临时文件 %s", wav)
            if self.on_result:
                try:
                    self.on_result(ok, wav, mp3,
                                   None if ok else "转换失败，已保留临时音频")
                except Exception:
                    logger.exception("on_result 回调出错")
            self._report()
            self.queue.task_done()
