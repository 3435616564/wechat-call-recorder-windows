# -*- coding: utf-8 -*-
"""API 客户端（OpenAI 兼容格式）。

- chat(messages, ...)      -> POST {base}/chat/completions
- transcribe(mp3, ...)     -> POST {base}/audio/transcriptions（multipart）
  超过 SINGLE_UPLOAD_MB 的文件先用 ffmpeg 按时长分段，逐段转写后拼接。

base_url 形如 https://api.example.com/v1（末尾不带斜杠也可）。
仅使用 Python 标准库 urllib，不新增运行时依赖。
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import urllib.error
import urllib.request

import imageio_ffmpeg

SINGLE_UPLOAD_MB = 20          # 超过此大小先分段
SEGMENT_SECONDS = 600          # 每段 10 分钟


def _ffmpeg() -> str:
    return str(next((Path(imageio_ffmpeg.__file__).parent / "binaries")
                    .glob("ffmpeg-win*.exe")))


def _full_url(base_url: str, path: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("未配置 API 地址（API 设置里填写 base_url）")
    if not base.endswith("/v1") and "/v1" not in base.split("?")[0]:
        base = base + "/v1"          # 常见服务商缺省补 /v1
    return base + path


def _headers(cfg: dict) -> dict:
    key = (cfg.get("api_key") or "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _post_json(url: str, headers: dict, body: dict, timeout: int = 180) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API 返回 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 API（检查网络和地址）：{exc.reason}") from exc


# ---------- 对话补全 ----------
def chat(cfg: dict, messages: list[dict], temperature: float = 0.7,
         max_tokens: int = 2048) -> str:
    """调用 /chat/completions，返回助手回复文本。"""
    url = _full_url(cfg.get("api_base_url", ""), "/chat/completions")
    body = {
        "model": cfg.get("chat_model") or "",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if not body["model"]:
        raise RuntimeError("未配置对话模型名（API 设置里填写）")
    data = _post_json(url, _headers(cfg), body, timeout=300)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"API 响应格式异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc


# ---------- 语音转文字 ----------
def _multipart(field: str, filename: str, mime: str, data: bytes,
               fields: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
            .encode("utf-8"))
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
         f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8"))
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def _transcribe_one(mp3: Path, cfg: dict, timeout: int = 600) -> str:
    url = _full_url(cfg.get("api_base_url", ""), "/audio/transcriptions")
    model = cfg.get("transcribe_model") or ""
    if not model:
        raise RuntimeError("未配置转写模型名（API 设置里填写）")
    data = mp3.read_bytes()
    body, boundary = _multipart(
        "file", mp3.name, "audio/mpeg", data,
        {"model": model, "response_format": "json"})
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    key = (cfg.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"转写 API 返回 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接转写 API：{exc.reason}") from exc
    return out.get("text") or ""


def transcribe(mp3: Path, cfg: dict, log=None, progress=None) -> str:
    """转写整段 MP3；大文件自动分段。progress(done,total) 用于界面进度。"""
    mp3 = Path(mp3)
    size_mb = mp3.stat().st_size / 1024 / 1024
    if size_mb <= SINGLE_UPLOAD_MB:
        if progress:
            progress(0, 1)
        text = _transcribe_one(mp3, cfg)
        if progress:
            progress(1, 1)
        return text.strip()

    # 分段：按 10 分钟切块（复制流，秒级完成）
    tmp_dir = mp3.parent / f".seg_{mp3.stem[:40]}_{uuid.uuid4().hex[:6]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-i", str(mp3),
               "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
               "-c:a", "libmp3lame", "-b:a", "48k", "-ac", "1",
               str(tmp_dir / "seg%03d.mp3")]
        subprocess.run(cmd, check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        segs = sorted(tmp_dir.glob("seg*.mp3"))
        if not segs:
            raise RuntimeError("分段失败：未生成音频片段")
        if log:
            log.info("转写分段：%d 段（每段约 %d 秒）", len(segs), SEGMENT_SECONDS)
        texts = []
        total = len(segs)
        for i, seg in enumerate(segs):
            texts.append(_transcribe_one(seg, cfg))
            if progress:
                progress(i + 1, total)
        return "".join(texts).strip()
    finally:
        try:
            for p in tmp_dir.glob("*"):
                p.unlink()
            tmp_dir.rmdir()
        except OSError:
            pass


def test_connection(cfg: dict) -> str:
    """设置界面的「测试连接」：发一条最小对话请求，返回模型回复摘要。"""
    reply = chat(cfg, [{"role": "user", "content": "回复两个字：正常"}],
                 max_tokens=16)
    return reply.strip()[:100] or "（空回复）"
