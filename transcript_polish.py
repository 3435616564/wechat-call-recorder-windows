# -*- coding: utf-8 -*-
"""文字稿智能校对：用大模型逐行修正语音识别错误。

设计要点
- 输入是本地转写产出的「[MM:SS] 文本」逐行文字稿。
- 只允许修正：同音/近音错别字、专有名词误识别、明显漏字多字、明显断句错误。
- 禁止：总结、改写、润色、合并/删除行、改动时间戳 —— 保证与录音一一对应。
- 输出按时间戳回填，输出行与输入行对不上时，该行保留原文（宁可少改，不可乱改）。
- 长文本按行分块（默认每块 ~1600 字），逐块调用；单块失败只影响该块。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import api_client

CHUNK_CHARS = 1600          # 每块大致字符数
MAX_LINES_PER_CHUNK = 80    # 每块最多行数
DEFAULT_WORKERS = 3         # 并发校对块数（太快可能触发限流）

_TS_LINE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")

SYSTEM_PROMPT = (
    "你是中文电话录音转写稿的校对员。用户给你若干行「[时间戳] 文本」，"
    "这些文本来自语音识别，存在错别字。\n"
    "你只做一件事：把识别错的字词改成正确、通顺的写法。具体包括：\n"
    "1) 同音/近音错别字；\n"
    "2) 人名、昵称、游戏/软件/地名等专有名词的误识别；\n"
    "3) 明显漏字、多字；\n"
    "4) 明显的断句错误（只调整逗号、句号位置）。\n"
    "严格禁止：总结、改写、润色、翻译、补充原文没有的内容、合并或删除行、"
    "改动或省略时间戳。口语风格、口头语、语气词、重复、口头禅都要原样保留。\n"
    "如果某一行没有字词错误，就原样输出该行；不要只为了加标点、把"
    "「ok」改成「OK」这类无关紧要的写法差异去改动整行。\n"
    "听不懂的拟声词、歌词、乱码片段保持原样，不要试图猜成句子。\n"
    "输出要求：逐行输出，行数与顺序必须和输入完全一致，每行以原时间戳开头"
    "（形如 [01:23] ），后面是修正后的文本；不要输出任何解释、标题或 Markdown。"
)

USER_PROMPT = (
    "{context}请校对下面这段转写稿，按上述规则逐行输出：\n\n{body}"
)


def _parse_lines(text: str) -> list[tuple[str, str]]:
    """把文字稿切成 (时间戳, 正文)；非标准行的时间戳为空字符串。"""
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _TS_LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
        else:
            out.append(("", line))
    return out


def _build_chunks(lines: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    chunks, cur, size = [], [], 0
    for item in lines:
        cost = len(item[0]) + len(item[1]) + 4
        if cur and (size + cost > CHUNK_CHARS or len(cur) >= MAX_LINES_PER_CHUNK):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += cost
    if cur:
        chunks.append(cur)
    return chunks


def _render(items: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{ts}] {txt}" if ts else txt for ts, txt in items)


def _apply_reply(items: list[tuple[str, str]], reply: str):
    """把模型回复按时间戳回填，返回 (新行列表, 修改数)。

    行数一致时按位置回填；否则按时间戳匹配；都失败则该块保留原文。
    """
    parsed = []
    for raw in reply.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^```[a-zA-Z]*|```$", "", line).strip()
        m = _TS_LINE.match(line)
        if m:
            parsed.append((m.group(1), m.group(2)))

    # 1) 行数一致 → 位置对齐（最稳）
    if parsed and len(parsed) == len(items):
        fixed = []
        changed = 0
        for (ts0, txt0), (ts1, txt1) in zip(items, parsed):
            new_text = txt1.strip()
            if not new_text:
                fixed.append((ts0, txt0))
                continue
            if new_text != txt0:
                changed += 1
            fixed.append((ts0, new_text))
        return fixed, changed

    # 2) 按时间戳配对
    pool: dict[str, list[str]] = {}
    for ts, txt in parsed:
        pool.setdefault(ts, []).append(txt)
    if pool:
        fixed, changed, hit = [], 0, 0
        for ts0, txt0 in items:
            bucket = pool.get(ts0)
            if bucket:
                new_text = bucket.pop(0).strip()
                hit += 1
                if new_text and new_text != txt0:
                    changed += 1
                    fixed.append((ts0, new_text))
                    continue
            fixed.append((ts0, txt0))
        if hit >= max(1, len(items) * 0.6):
            return fixed, changed

    # 3) 放弃该块：保留原文
    return list(items), 0


def polish(text: str, cfg: dict, log=None, on_progress=None,
           context_hint: str = "", workers: int = DEFAULT_WORKERS
           ) -> tuple[str, str, int]:
    """校对整篇文字稿（分块并发调用，输出按原顺序拼回）。

    返回 (校对后文本, 校对记录 Markdown, 修改行数)。
    """
    lines = _parse_lines(text)
    if not lines:
        return text, "", 0
    chunks = _build_chunks(lines)
    context = ""
    if context_hint:
        context = f"背景：{context_hint}\n"

    results: list[tuple[list[tuple[str, str]], int, bool]] = [None] * len(chunks)
    done = 0
    lock_done = 0
    import threading
    lock = threading.Lock()

    def work(idx: int):
        nonlocal done
        chunk = chunks[idx]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT.format(context=context,
                                                           body=_render(chunk))},
        ]
        failed = False
        try:
            reply = api_client.chat(cfg, messages, temperature=0.1,
                                    max_tokens=3000)
            fixed, changed = _apply_reply(chunk, reply)
        except Exception as exc:
            failed = True
            fixed, changed = list(chunk), 0
            if log:
                log.warning("校对第 %d/%d 块失败，保留原文：%s",
                            idx + 1, len(chunks), exc)
        results[idx] = (fixed, changed, failed)
        with lock:
            done += 1
            if on_progress:
                on_progress(done, len(chunks))

    workers = max(1, min(int(workers or 1), len(chunks)))
    if workers == 1:
        for i in range(len(chunks)):
            work(i)
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="polish") as pool:
            list(pool.map(work, range(len(chunks))))

    out_lines: list[tuple[str, str]] = []
    records: list[str] = []
    changed_total = 0
    failed_blocks = 0
    for chunk, (fixed, changed, failed) in zip(chunks, results):
        if fixed is None:                      # 理论上不会发生
            fixed, changed, failed = list(chunk), 0, True
        out_lines.extend(fixed)
        changed_total += changed
        failed_blocks += 1 if failed else 0
        if changed:
            for (ts0, txt0), (_, txt1) in zip(chunk, fixed):
                if txt0 != txt1:
                    records.append(f"- [{ts0}] {txt0}\n  → {txt1}")

    polished = "\n".join(f"[{ts}] {txt}" if ts else txt for ts, txt in out_lines) + "\n"

    head = ["# 文字稿校对记录", "",
            f"- 校对引擎：{cfg.get('chat_model') or '对话模型'}",
            f"- 原稿 {len(lines)} 行，修正 {changed_total} 行"
            + (f"，{failed_blocks} 块调用失败（保留原文）" if failed_blocks else ""), ""]
    if failed_blocks:
        head.append("> 有分块调用失败，失败部分保持原样，可重新分析再校对一次。")
        head.append("")
    record_md = "\n".join(head + records) + "\n" if records else "\n".join(head)
    return polished, record_md, changed_total


def polish_file(mp3: Path, cfg: dict, log=None, on_progress=None) -> tuple[Path, int]:
    """就地校对某通录音的文字稿：原稿另存为 _文字稿_原始.txt，正文覆盖为校对稿。

    返回 (文字稿路径, 修改行数)。
    """
    mp3 = Path(mp3)
    txt_path = mp3.with_name(mp3.stem + "_文字稿.txt")
    raw_path = mp3.with_name(mp3.stem + "_文字稿_原始.txt")
    rec_path = mp3.with_name(mp3.stem + "_校对记录.md")
    text = txt_path.read_text("utf-8")
    if not raw_path.exists():
        raw_path.write_text(text, "utf-8")
    hint = ""
    name = mp3.stem.split("_")[3] if len(mp3.stem.split("_")) >= 4 else ""
    if name:
        hint = f"这是一通微信语音通话，通话联系人的备注名叫「{name}」，" \
               f"文中出现的人名/昵称可能是这个或与之相关的称呼。"
    polished, record, changed = polish(text, cfg, log=log,
                                       on_progress=on_progress,
                                       context_hint=hint)
    if changed:
        txt_path.write_text(polished, "utf-8")
        rec_path.write_text(record, "utf-8")
    return txt_path, changed
