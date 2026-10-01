# -*- coding: utf-8 -*-
"""分析管线：转文字 → 多维度分析 → 落盘。

产物（与 MP3 同目录）：
- <名字>_文字稿.txt
- <名字>_分析.md   （内容总结 / 情感变化 / 行动要点，各一节）
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

import api_client


def _fmt_eta(sec: float) -> str:
    """把剩余秒数写成「2分30秒」这种自然说法。"""
    sec = int(max(0, sec))
    if sec >= 60:
        m, s = divmod(sec, 60)
        return f"{m}分{s:02d}秒" if s else f"{m}分钟"
    if sec <= 3:
        return "马上就好"
    return f"{sec}秒"

# 输出格式统一要求：给人读的纯文本，不带机器符号
_FMT_RULES = (
    "\n\n【输出格式，非常重要】"
    "\n你的输出会被直接展示给普通用户阅读，必须像人写的总结，禁止任何机器符号："
    "\n1. 禁止 Markdown：不要 ** 加粗、不要 # 或 ## 标题、不要反引号、"
    "不要表格、不要 → 箭头和 ▶ ▸ 之类的装饰符号。"
    "\n2. 时间点用自然的中文写法，例如「30分05秒」「约35分钟时」「1小时10分左右」，"
    "禁止写成 [30:00] 或 (30:00-35:00) 这种刻度格式。"
    "\n3. 分点时每行用「1.」「2.」或「·」开头即可，不要用短横线列表。"
    "\n4. 引用原话时直接说：例如 在30分05秒对方说「我很可爱」；"
    "不要罗列大量括号和标签编号。"
    "\n5. 每个自然段 2~4 句话，段落之间空一行，读起来通顺自然。")

# 每个维度的提示词（system + user 模板，{transcript} 为文字稿，{tone} 为语气曲线）
ASPECTS = {
    "内容总结": {
        "system": "你是专业的通话记录整理助手。请把通话文字稿整理成简明扼要的要点总结，"
                  "按话题分组；电话闲聊内容可简略带过。" + _FMT_RULES,
        "user": "请总结以下通话的内容要点：\n\n{transcript}",
    },
    "情感变化": {
        "system": "你是善解人意的沟通分析助手。你会拿到通话文字稿、一份逐段情感标签"
                  "（由本地语音模型输出：积极/中性/消极/惊讶等）和一份客观语气曲线"
                  "（每 5 分钟的音量水平、说话占比、长静音、音量突增时刻——由本地声学分析生成）。"
                  "请结合三者分析通话的情感氛围与变化：整体氛围如何、情绪高峰/低谷出现在哪个话题"
                  "（可引用时间点，如「30分钟后音量偏轻，结合此时话题…」）、"
                  "双方互动是否顺畅。情感标签与语气曲线都是客观信号，只用于辅助判断节奏与强度，"
                  "不要凭空编造情绪；文字稿没有的细节不要编。语气客观温和，像写给朋友看的信，"
                  "不要写成数据报表。" + _FMT_RULES,
        "user": "以下是通话文字稿、逐段情感标签和语气曲线，请分析情感氛围与变化：\n\n"
                "【文字稿】\n{transcript}\n\n【逐段情感标签（本地模型输出）】\n{emotion}\n\n"
                "【语气曲线（客观声学信号）】\n{tone}",
    },
    "行动要点": {
        "system": "你是可靠的执行助理。请从通话文字稿中提取所有承诺、待办、约定的时间和事项，"
                  "按「谁 - 要做什么 - 什么时间」列出；没有的事项不要编造。" + _FMT_RULES,
        "user": "请提取以下通话中的行动要点：\n\n{transcript}",
    },
}

ASPECT_KEYS = list(ASPECTS.keys())


class AnalyzeProgress:
    """把管线进度回调统一成 (阶段文本, 0-100 或 None)。"""

    def __init__(self, on_update):
        self.on_update = on_update

    def stage(self, text: str, percent=None):
        if self.on_update:
            try:
                self.on_update(text, percent)
            except Exception:
                pass


def _transcribe(mp3: Path, cfg: dict, prog: "AnalyzeProgress",
                log=None) -> tuple[str, str, str]:
    """按用户选择的引擎转写；本地失败绝不自动上传音频。

    返回 (文字稿文本, 引擎说明, 情感标签文本)。
    """
    engine = str(cfg.get("transcribe_engine", "local")).lower()
    if engine == "local":
        try:
            import local_asr
            key = str(cfg.get("local_asr_model") or local_asr.DEFAULT_MODEL)
            if not local_asr.model_available(key):
                raise FileNotFoundError(
                    f"本地模型文件缺失（models/ 下），可选："
                    f"{[k for k, _ in local_asr.available_models()]}")
            threads = int(cfg.get("local_asr_threads", 0) or 0)
            asr = local_asr.LocalASR(key, num_threads=threads or None)
            prog.stage(f"正在本地转写（离线免费，{asr.desc}）…", 5)

            t0 = time.monotonic()

            def cprogress(done, total):
                if total and done < total:
                    pct = int(done / total * 90) + 5
                    eta = ""
                    if done > 0:
                        elapsed = max(time.monotonic() - t0, 0.001)
                        speed = done / elapsed            # 每秒能转写多少音频秒
                        remain = (total - done) / max(speed, 0.01)
                        eta = (f"（已转写 {int(done / total * 100)}%，"
                               f"预计还需 {_fmt_eta(remain)}）")
                    prog.stage(f"本地转写中… {done:.0f}/{total:.0f} 秒"
                               f" {eta}", pct)
                elif total:
                    # 已读完整个音频，剩最后几段解码收尾
                    prog.stage("本地转写中… 音频读完，正在收尾", 93)
                else:
                    prog.stage(f"本地转写中… 已处理 {done:.0f} 秒", None)

            segs, info = asr.transcribe(mp3, on_progress=cprogress)
            text = local_asr.segments_to_text(segs)
            desc = (f"本地离线 {info['model_desc']}"
                    f"（{info['speed']}x 实时，{info['segments']} 段）")
            if log:
                log.info("本地转写完成：%s", desc)
            return text, desc, _emotion_report(segs, info)
        except Exception as exc:
            raise RuntimeError(
                f"本地转写失败：{exc}。录音没有上传；可检查本地模型，"
                "或手动切换到云端转写。") from exc

    if engine != "api":
        raise ValueError(f"未知的转写方式：{engine}")

    # 云端 API（OpenAI 兼容）
    prog.stage("正在云端转写（使用已配置的 API）…")

    def tprogress(done, total):
        prog.stage(f"云端转写中… {done}/{total} 段",
                   int(done / total * 100) if total else None)

    text = api_client.transcribe(mp3, cfg, log=log, progress=tprogress)
    model_name = cfg.get("transcribe_model") or "云端模型"
    return text, f"云端 API {model_name}", ""


_EMO_CN = {
    "<|HAPPY|>": "积极", "<|SAD|>": "消极", "<|ANGRY|>": "激动/生气",
    "<|NEUTRAL|>": "中性", "<|SURPRISED|>": "惊讶", "<|DISGUSTED|>": "厌恶",
    "<|EMO_UNKNOWN|>": "未识别",
}


def _emotion_report(segs: list[dict], info: dict) -> str:
    """把本地模型的逐段情感标签整理成可读文本，供情感分析参考。"""
    from collections import Counter
    emos = [s.get("emotion") for s in segs if s.get("emotion")]
    if not emos:
        return ""
    cnt = Counter(emos)
    lines = [f"- 模型：{info.get('model_desc', '')}",
             "- 标签分布（按段落数）：" + "、".join(
                 f"{_EMO_CN.get(k, k)} {v} 段" for k, v in cnt.most_common()),
             "", "### 非中性段落（时间点 + 标签）"]
    shown = 0
    for s in segs:
        e = s.get("emotion")
        if e and e not in ("<|NEUTRAL|>", "<|EMO_UNKNOWN|>"):
            lines.append(f"- [{int(s['start']) // 60:02d}:{int(s['start']) % 60:02d}]"
                         f" {_EMO_CN.get(e, e)}：{s['text'][:40]}")
            shown += 1
            if shown >= 60:
                lines.append("-（其余略）")
                break
    return "\n".join(lines) + "\n"


def run_analysis(mp3: Path, cfg: dict, aspects: list[str], log=None,
                 on_update=None) -> tuple[Path, Path | None]:
    """完整分析一通录音，返回 (文字稿路径, 分析报告路径)。

    未选择分析维度时不生成报告文件，第二项返回 None。"""
    mp3 = Path(mp3)
    prog = AnalyzeProgress(on_update)
    base = mp3.with_name(mp3.stem)
    txt_path = base.with_name(mp3.stem + "_文字稿.txt")
    md_path = base.with_name(mp3.stem + "_分析.md")

    # 0) 本地声学语气曲线（零 API 成本，缓存复用）
    import acoustic_curve
    tone_path = base.with_name(mp3.stem + "_语气曲线.md")
    if tone_path.exists():
        tone_text = tone_path.read_text("utf-8")
        prog.stage("已有语气曲线缓存，跳过声学分析")
    else:
        prog.stage("正在分析语气曲线（本地声学信号，不联网）…")
        tone_text = acoustic_curve.analyze(mp3)
        tone_path.write_text(tone_text, "utf-8")
        if log:
            log.info("语气曲线完成 %s", tone_path.name)

    # 1) 文字稿（已有缓存可跳过）
    transcript = ""
    engine_desc = ""
    emotion_text = ""
    emo_path = base.with_name(mp3.stem + "_情感标签.txt")
    if txt_path.exists():
        transcript = txt_path.read_text("utf-8").strip()
        prog.stage(f"已有文字稿缓存（{len(transcript)} 字），跳过转写")
    if not transcript:
        transcript, engine_desc, emotion_text = _transcribe(
            mp3, cfg, prog, log)
    if not transcript:
        raise RuntimeError("转写结果为空（本地方案请检查 models/ 下的模型文件；"
                           "云端方案请检查转写模型名是否正确）")
    txt_path.write_text(transcript, "utf-8")
    if emotion_text and not emo_path.exists():
        emo_path.write_text(emotion_text, "utf-8")
    prog.stage(f"文字稿完成（{len(transcript)} 字）", 100)
    if log:
        log.info("转写完成 %s（%d 字，引擎：%s）", mp3.name, len(transcript),
                 engine_desc or "缓存")
    if not emotion_text and emo_path.exists():
        emotion_text = emo_path.read_text("utf-8").strip()

    # 1.5) 文字稿智能校对（把识别错的字词改成合理的；可选、可关）
    polish_on = bool(cfg.get("transcript_polish", True))
    has_key = bool(str(cfg.get("api_key", "")).strip())
    raw_path = base.with_name(mp3.stem + "_文字稿_原始.txt")
    rec_path = base.with_name(mp3.stem + "_校对记录.md")
    if polish_on and has_key and not raw_path.exists():
        import transcript_polish
        prog.stage("正在智能校对文字稿（修正识别错误的字词）…")

        p0 = time.monotonic()

        def pprogress(done, total):
            eta = ""
            if total and done > 0:
                elapsed = max(time.monotonic() - p0, 0.001)
                speed = done / elapsed
                remain = (total - done) / max(speed, 0.01)
                eta = f"（预计还需约 {_fmt_eta(remain)}）"
            prog.stage(f"智能校对中… {done}/{total} 段 {eta}",
                       int(done / total * 100) if total else None)

        try:
            _, changed = transcript_polish.polish_file(
                mp3, cfg, log=log, on_progress=pprogress)
            if changed:
                transcript = txt_path.read_text("utf-8").strip()
                prog.stage(f"智能校对完成：修正 {changed} 行（原稿见 _文字稿_原始.txt）")
                if log:
                    log.info("智能校对完成：修正 %d 行", changed)
            else:
                prog.stage("智能校对完成：未发现需要修正的地方")
        except Exception as exc:
            prog.stage(f"智能校对失败（已保留原始文字稿）：{exc}")
            if log:
                log.warning("智能校对失败：%s", exc)
    elif polish_on and not has_key:
        prog.stage("未配置 API，跳过智能校对（文字稿为原始识别结果）")

    # 2) 逐维度分析（一个维度都不选 = 只做本地免费部分：文字稿 + 语气曲线）
    sections = []
    if not aspects:
        prog.stage("未选择分析维度：仅生成文字稿与语气曲线（本地免费）", 100)
        if log:
            log.info("未选择分析维度，跳过云端 AI 分析")
    total = len(aspects)
    for i, key in enumerate(aspects):
        if key not in ASPECTS:
            continue
        prog.stage(f"AI 分析中（{i + 1}/{total}）：{key}",
                   int((i + 0.2) / max(total, 1) * 100))
        user_text = ASPECTS[key]["user"].format(transcript=transcript,
                                                tone=tone_text,
                                                emotion=emotion_text or "（无标签）")
        messages = [
            {"role": "system", "content": ASPECTS[key]["system"]},
            {"role": "user", "content": user_text},
        ]
        reply = api_client.chat(cfg, messages, max_tokens=2048)
        sections.append((key, reply.strip()))
        prog.stage(f"完成：{key}", int((i + 1) / max(total, 1) * 100))
        if log:
            log.info("分析维度完成：%s（%d 字）", key, len(reply))

    if not sections and aspects:
        raise RuntimeError("没有可执行的维度")

    if not sections:
        # 未选择分析维度：只保留文字稿/语气曲线/情感标签，【不写】报告文件。
        # 旧版本会写一份「本地免费产物」占位报告——既让界面误以为已有分析，
        # 还会覆盖以前生成的真实报告（数据丢失），已移除。
        prog.stage("未选择分析维度：仅生成文字稿与语气曲线（本地免费）", 100)
        if log:
            log.info("未选择分析维度，不生成分析报告文件")
        return txt_path, None

    # 3) 合成报告
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 通话分析：{mp3.stem}", "",
             f"- 分析时间：{stamp}",
             f"- 文字稿：{txt_path.name}（{len(transcript)} 字）",
             f"- 语气曲线：{tone_path.name}"]
    if engine_desc:
        lines.append(f"- 转写引擎：{engine_desc}")
    if emotion_text:
        lines.append(f"- 情感标签：{emo_path.name}")
    if raw_path.exists():
        lines.append(f"- 文字稿校对：已用 AI 校对（原稿 {raw_path.name}、"
                     f"逐条修改见 {rec_path.name}）")
    lines.append("")
    for key, body in sections:
        lines += [f"## {key}", "", body, ""]
    md_path.write_text("\n".join(lines), "utf-8")
    prog.stage("分析完成", 100)
    if log:
        log.info("分析报告已保存 %s", md_path.name)
    return txt_path, md_path


def build_chat_context(mp3: Path) -> tuple[str, str] | None:
    """读取某通录音的文字稿/分析报告/语气曲线，作为 AI 对话上下文。"""
    mp3 = Path(mp3)
    txt = mp3.with_name(mp3.stem + "_文字稿.txt")
    md = mp3.with_name(mp3.stem + "_分析.md")
    tone = mp3.with_name(mp3.stem + "_语气曲线.md")
    emo = mp3.with_name(mp3.stem + "_情感标签.txt")
    transcript = txt.read_text("utf-8") if txt.exists() else ""
    report = md.read_text("utf-8") if md.exists() else ""
    tone_text = tone.read_text("utf-8") if tone.exists() else ""
    emo_text = emo.read_text("utf-8") if emo.exists() else ""
    if not transcript and not report:
        return None
    parts = []
    if transcript:
        parts.append("【通话文字稿】\n" + transcript)
    if report:
        parts.append("【已有分析报告】\n" + report)
    if emo_text:
        parts.append("【逐段情感标签】\n" + emo_text)
    if tone_text:
        parts.append("【语气曲线（客观声学信号）】\n" + tone_text)
    return "\n\n".join(parts), mp3.stem


def chat_about(recording_name: str, context: str, history: list[dict],
               user_text: str, cfg: dict) -> str:
    """围绕某通录音的 AI 对话（一次往返）。"""
    system = (
        "你是用户的通话记录助手。用户会围绕一通录音向你提问（比如总结、"
        "某个话题的细节、对方的话是什么意思、接下来该怎么做）。"
        "材料会同时包含【通话文字稿】和【已有分析报告】：先参考分析报告里"
        "整理好的结论，再回到文字稿核对具体细节和原话，两者结合着回答；"
        "冲突时以文字稿为准并说明。没提到的内容不要编造；"
        "如果材料没有覆盖用户问的细节，就直说录音里没有相关信息。"
        "\n\n回答格式：用自然的中文段落，像平时说话一样；"
        "禁止 Markdown 符号（** 加粗、# 标题、表格、反引号）；"
        "时间点写成「30分05秒」这种自然写法，不要 [30:00] 格式。"
        f"\n\n这通录音是：{recording_name}\n\n{context}")
    messages = [{"role": "system", "content": system}] + history[-12:] + [
        {"role": "user", "content": user_text}]
    return api_client.chat(cfg, messages, max_tokens=2048)
