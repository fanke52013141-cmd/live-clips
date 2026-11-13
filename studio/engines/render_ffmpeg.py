"""ffmpeg 渲染引擎（04 号方案 §10 的 P2-1 子集）。

本批实现：
1. 词边界切割：入/出点吸附最近词边界（无词级时间戳则原样），偏移写回执
2. 30ms 音频淡化防爆音（afade in/out）
3. 两遍 loudnorm 响度归一（I=-14:TP=-1.5:LRA=11，第二遍用实测值 linear）
4. 逐词字幕：ASS 卡拉 OK 式高亮（当前词变色），语义断行（标点/≤12字），
   最后烧字幕（先无其他视频滤镜叠加）；竖屏 v1 为中心裁切（人物取景后续批）
5. 渲染自检：时长偏差≤0.25s、音视频流齐全、文件非空；不合格抛错不静默

明确不做（后续批）：stream-copy 快路径、人物取景竖屏、去气口、cold-open。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from studio.engines.base import (
    ArtifactRef,
    EngineContext,
    ProgressEvent,
    ProgressFn,
    RenderPlan,
    Word,
)

FADE_S = 0.03
BROLL_IMAGE_S = 2.0  # L3 生图片头静帧时长（P3）
MAX_LINE_CHARS = 12
_PUNCT = "，。！？、；：,.!?;:"
_HIGHLIGHT = r"{\c&H00FFFF&}"  # ASS BGR 黄色
_NORMAL = r"{\c&HFFFFFF&}"


def snap_to_words(
    start_ms: int, end_ms: int, words: tuple[Word, ...] | list[Word]
) -> tuple[int, int, dict]:
    """吸附最近词边界。返回 (start, end, 偏移回执)。无词则原样。"""
    if not words:
        return start_ms, end_ms, {"snapped": False}
    starts = [w.start_ms for w in words]
    ends = [w.end_ms for w in words]
    ns = min(starts, key=lambda v: abs(v - start_ms))
    ne = min(ends, key=lambda v: abs(v - end_ms))
    if ne <= ns:  # 吸附后空区间 → 放弃吸附，保留原切点（回执记录）
        return start_ms, end_ms, {"snapped": False, "reason": "吸附后区间为空"}
    return ns, ne, {
        "snapped": True,
        "start_offset_ms": abs(ns - start_ms),
        "end_offset_ms": abs(ne - end_ms),
    }


def group_lines(words: list[Word]) -> list[list[Word]]:
    """语义断行：标点收尾或超 12 字换行，不拦腰断词。"""
    lines: list[list[Word]] = []
    cur: list[Word] = []
    size = 0
    for w in words:
        cur.append(w)
        size += len(w.text)
        if (w.text and w.text[-1] in _PUNCT) or size >= MAX_LINE_CHARS:
            lines.append(cur)
            cur, size = [], 0
    if cur:
        lines.append(cur)
    return lines


def _ass_time(ms: int) -> str:
    ms = max(0, ms)
    cs = ms // 10
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_header(portrait: bool, with_hook: bool = False, with_aigc: bool = False) -> str:
    res_x, res_y = (1080, 1920) if portrait else (1920, 1080)
    font_size = 64 if portrait else 58
    margin_v = 160 if portrait else 80
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {res_x}\nPlayResY: {res_y}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H0000FFFF,"
        "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,60,60,"
        f"{margin_v},1\n"
    )
    if with_hook:
        # L1 贴片：上 1/3（Alignment 8 顶部居中，MarginV≈res_y/6）、大字描边
        hook_size = 96 if portrait else 80
        hook_mv = res_y // 6
        header += (
            f"Style: Hook,Microsoft YaHei,{hook_size},&H00FFFFFF,&H0000FFFF,"
            "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,8,60,60,"
            f"{hook_mv},1\n"
        )
    if with_aigc:
        # AIGC 显式角标（P3）：右上角小字全程，半透明白+黑描边
        header += (
            "Style: AIGC,Microsoft YaHei,32,&H80FFFFFF,&H0000FFFF,"
            "&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,9,40,40,40,1\n"
        )
    header += (
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return header


def _aigc_events(total_dur_ms: int) -> list[str]:
    """AIGC 显式角标：全程「AI生成」（显式标识义务，不可关闭）。"""
    if total_dur_ms <= 0:
        return []
    return [f"Dialogue: 2,0:00:00.00,{_ass_time(total_dur_ms)},AIGC,,0,0,0,,AI生成"]


def _hook_overlay_events(hook_overlay: dict) -> list[str]:
    """L1 贴片事件：输出时间轴 0→duration_s，淡入淡出（\\fad）。"""
    text = _ass_escape(str(hook_overlay.get("text", "")))
    if not text:
        return []
    dur_ms = int(float(hook_overlay.get("duration_s", 2.0)) * 1000)
    return [
        f"Dialogue: 1,0:00:00.00,{_ass_time(dur_ms)},Hook,,0,0,0,,{{\\fad(200,300)}}{text}"
    ]


def _ass_events(
    words: list[Word], clip_start_ms: int, clip_end_ms: int, offset_ms: int = 0
) -> list[str]:
    """逐词高亮事件：每词一个事件，显示整行、当前词变色。时间相对切片+offset。"""
    events: list[str] = []
    for line in group_lines(words):
        line_start = max(line[0].start_ms, clip_start_ms)
        line_end = min(line[-1].end_ms, clip_end_ms)
        if line_end <= line_start:
            continue
        texts = [_ass_escape(w.text) for w in line]
        for i, w in enumerate(line):
            ev_start = max(w.start_ms, clip_start_ms)
            ev_end = min(line[i + 1].start_ms if i + 1 < len(line) else line_end, clip_end_ms)
            if ev_end <= ev_start:
                ev_end = min(ev_start + 80, clip_end_ms)  # 极短词保底可见
            if ev_end <= ev_start:
                continue
            marked = "".join(
                f"{_HIGHLIGHT}{t}{_NORMAL}" if j == i else t for j, t in enumerate(texts)
            )
            events.append(
                f"Dialogue: 0,{_ass_time(ev_start - clip_start_ms + offset_ms)},"
                f"{_ass_time(ev_end - clip_start_ms + offset_ms)},Default,,0,0,0,,{marked}"
            )
    return events


def build_ass(
    words: list[Word], clip_start_ms: int, clip_end_ms: int, *, portrait: bool,
    hook_overlay: dict | None = None, time_offset_ms: int = 0,
    aigc_badge: bool = False, total_dur_ms: int = 0,
) -> str:
    """逐词高亮 ASS（无剪辑快路径）。time_offset_ms=前置段偏移（P3）。"""
    events = _ass_events(words, clip_start_ms, clip_end_ms, time_offset_ms)
    if hook_overlay:
        events = _hook_overlay_events(hook_overlay) + events
    if aigc_badge:
        events = events + _aigc_events(total_dur_ms)
    return _ass_header(portrait, with_hook=bool(hook_overlay), with_aigc=aigc_badge) + "\n".join(events) + "\n"


def build_ass_segments(
    segments, words: tuple[Word, ...] | list[Word], *, portrait: bool,
    hook_overlay: dict | None = None, time_offset_ms: int = 0,
    aigc_badge: bool = False, total_dur_ms: int = 0,
) -> str:
    """段式 ASS（P2-2/P2-3）：每段取段内词，时间按段累计偏移到输出时间轴。

    被剪区间的词自然落选；cold_open 复制段的词在片头再次出现。
    L1 贴片事件在输出时间轴 0→2s，独立于段；time_offset_ms 为前置段偏移（P3）。
    """
    events: list[str] = []
    if hook_overlay:
        events.extend(_hook_overlay_events(hook_overlay))
    acc = time_offset_ms
    for seg in segments:
        seg_words = [w for w in words if w.end_ms > seg.start_ms and w.start_ms < seg.end_ms]
        events.extend(_ass_events(seg_words, seg.start_ms, seg.end_ms, acc))
        acc += seg.duration_ms
    if aigc_badge:
        events.extend(_aigc_events(total_dur_ms))
    return _ass_header(portrait, with_hook=bool(hook_overlay), with_aigc=aigc_badge) + "\n".join(events) + "\n"


def _ass_filter_path(path: Path) -> str:
    """ffmpeg 滤镜路径转义：反斜杠→正斜杠；冒号需双反斜杠。

    滤镜字符串经两层解析（滤镜图解析器 + ass 选项解析器），每层各消耗
    一个反斜杠，故盘符冒号必须写成 ``\\\\:``（字面两个反斜杠+冒号），
    单层 ``\\:`` 会在第一层被吃掉导致路径被当作选项分隔截断。
    """
    return str(path).replace("\\", "/").replace(":", "\\\\:")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def measure_loudness(source: str, start_s: float, end_s: float, target: float) -> dict:
    """loudnorm 第一遍：测量切片响度，返回实测参数 JSON。"""
    r = _run([
        "ffmpeg", "-nostdin", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
        "-i", source,
        "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ])
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if not m:
        raise RuntimeError(f"loudnorm 测量失败: {r.stderr[-500:]}")
    return json.loads(m[-1])


def scan_defects(path: str) -> list[dict]:
    """成片缺陷扫描（04 §10.10 v1）：黑屏≥0.5s / 静音≥1.5s / 冻结≥2s。

    返回问题列表写回执（警告级，不阻断交付）；主体出框需人脸检测，后续批。
    """
    r = _run([
        "ffmpeg", "-nostdin", "-v", "info", "-i", path,
        "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=2",
        "-af", "silencedetect=n=-45dB:d=1.5",
        "-f", "null", "-",
    ])
    if r.returncode != 0:
        return [{"type": "scan_failed", "detail": r.stderr[-200:]}]
    issues: list[dict] = []
    for kind, start_pat, end_pat in (
        # 各滤镜输出格式不一：black_start:0 无空格，silence/freeze 冒号后带空格
        ("black", r"black_start:\s*([\d.]+)", r"black_end:\s*([\d.]+)"),
        ("freeze", r"freeze_start:\s*([\d.]+)", r"freeze_end:\s*([\d.]+)"),
        ("silence", r"silence_start:\s*([\d.]+)", r"silence_end:\s*([\d.]+)"),
    ):
        starts = [float(v) for v in re.findall(start_pat, r.stderr)]
        ends = [float(v) for v in re.findall(end_pat, r.stderr)]
        for i, st in enumerate(starts):
            en = ends[i] if i < len(ends) else None
            issues.append({"type": kind, "start_s": st, "end_s": en})
    return issues


def _video_concat_chain(segments) -> str:
    """段式视频链：trim+setpts 逐段重置时间戳后 concat（输出顺序=段顺序）。"""
    parts = []
    for i, seg in enumerate(segments):
        parts.append(
            f"[0:v]trim=start={seg.start_ms / 1000:.3f}:end={seg.end_ms / 1000:.3f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
    labels = "".join(f"[v{i}]" for i in range(len(segments)))
    parts.append(f"{labels}concat=n={len(segments)}:v=1:a=0[vcat]")
    return ";".join(parts)


def _audio_concat_chain(segments, *, with_whoosh: bool = False, total_dur_s: float = 0.0) -> tuple[str, str]:
    """段式音频链；with_whoosh 时在片头叠 0.5s 粉噪扫频（flash-forward 音效）。"""
    parts = []
    for i, seg in enumerate(segments):
        parts.append(
            f"[0:a]atrim=start={seg.start_ms / 1000:.3f}:end={seg.end_ms / 1000:.3f},"
            f"asetpts=PTS-STARTPTS[a{i}]"
        )
    labels = "".join(f"[a{i}]" for i in range(len(segments)))
    parts.append(f"{labels}concat=n={len(segments)}:v=0:a=1[acat]")
    out_label = "acat"
    if with_whoosh:
        n_samples = max(1, int(total_dur_s * 44100))
        parts.append(
            "anoisesrc=color=pink:sample_rate=44100:duration=0.5,"
            "afade=t=in:d=0.08,afade=t=out:st=0.30:d=0.20,volume=0.6[wh]"
        )
        parts.append(f"[wh]apad=whole_len={n_samples}[whp]")
        parts.append("[acat]aresample=44100[amain]")
        parts.append("[amain][whp]amix=inputs=2:duration=first:normalize=0[amixed]")
        out_label = "amixed"
    return ";".join(parts), out_label


def measure_loudness_graph(
    source: str, chain: str, label: str, target: float,
    extra_inputs: tuple[str, ...] = (),
) -> dict:
    """段式剪辑路径的 loudnorm 第一遍：对 concat 后的音频测量（含 whoosh/TTS）。"""
    args = ["ffmpeg", "-nostdin", "-i", source]
    for p in extra_inputs:
        args += ["-i", p]
    args += [
        "-filter_complex",
        chain + f";[{label}]loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json[ameas]",
        "-map", "[ameas]", "-f", "null", "-",
    ]
    r = _run(args)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if not m:
        raise RuntimeError(f"loudnorm 段式测量失败: {r.stderr[-500:]}")
    return json.loads(m[-1])


def _probe_duration_s(path: str) -> float:
    r = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ])
    if r.returncode != 0:
        raise RuntimeError(f"TTS 音频探测失败: {r.stderr[-200:]}")
    return float(r.stdout.strip() or 0)


def _hook_audio_parts(
    in_label: str, duck: float, tts_dur_s: float, total_dur_s: float,
    input_idx: int = 1,
) -> tuple[list[str], str]:
    """L2 TTS 混音：TTS 输入叠片头，原声在 TTS 期间闪避（volume enable）。

    apad 必须用 whole_len 有限填充：无限 apad + amix duration=first 存在
    线程调度竞态，输出时长非确定截断（实测 2.3s/3.3s 随机）。先统一采样率
    再按输出总时长补零，保证确定性。
    """
    whole_len = max(1, int(total_dur_s * 44100))
    parts = [
        f"[{in_label}]volume=volume={duck}:enable='between(t,0,{tts_dur_s:.3f})'[ducked]",
        f"[{input_idx}:a]aresample=44100,apad=whole_len={whole_len}[hp]",
        "[ducked][hp]amix=inputs=2:duration=first:normalize=0[ahook]",
    ]
    return parts, "ahook"


def _probe_video_info(path: str) -> dict:
    r = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-print_format", "json", path,
    ])
    if r.returncode != 0:
        raise RuntimeError(f"视频探测失败: {r.stderr[-200:]}")
    st = (json.loads(r.stdout).get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate", "25/1") or "25/1").partition("/")
    fps = float(num) / float(den or 1)
    return {"width": int(st.get("width", 0)), "height": int(st.get("height", 0)), "fps": fps or 25.0}


def _prepend_video_chain(idx: int, asset: str, dur_s: float, info: dict, is_image: bool) -> str:
    """前置段视频链（P3）：分辨率/帧率/SAR 对齐主视频，concat 才合法。"""
    w, h, fps = info["width"], info["height"], info["fps"]
    align = (
        f"fps={fps:.3f},scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    if is_image:
        return (
            f"[{idx}:v]loop=loop=-1:size=1:start=0,trim=duration={dur_s:.3f},"
            f"setpts=PTS-STARTPTS,{align}[pre]"
        )
    return f"[{idx}:v]trim=duration={dur_s:.3f},setpts=PTS-STARTPTS,{align}[pre]"


def _prepend_audio_parts(
    main_label: str, dur_s: float, asset_idx: int | None = None, has_audio: bool = False,
) -> tuple[list[str], str]:
    """前置段音频（W-3）：生视频有音轨→原声；无音轨/生图→等长静音。格式对齐后 concat。"""
    fmt = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if has_audio and asset_idx is not None:
        pre = f"[{asset_idx}:a]atrim=duration={dur_s:.3f},asetpts=PTS-STARTPTS,{fmt}[prea]"
    else:
        pre = f"anullsrc=r=44100:cl=stereo,atrim=duration={dur_s:.3f},asetpts=PTS-STARTPTS,{fmt}[prea]"
    parts = [
        pre,
        f"[{main_label}]{fmt}[amainf]",
        "[prea][amainf]concat=n=2:v=0:a=1[apre]",
    ]
    return parts, "apre"


class FfmpegRenderer:
    name = "x264"

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            return False, "ffmpeg/ffprobe 不可用"
        return True, ""

    def render(self, plan: RenderPlan, progress: ProgressFn) -> ArtifactRef:
        from studio.pipeline.editlist import Cut, build_segments

        if not plan.source_path or not Path(plan.source_path).is_file():
            raise RuntimeError(f"源文件不存在: {plan.source_path}")
        out = Path(plan.out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        clip = plan.clip

        # 1) 词边界吸附
        start_ms, end_ms, snap_meta = snap_to_words(clip.start_ms, clip.end_ms, plan.words)
        progress(ProgressEvent("rendering", 1, 4, "步", "词边界吸附"))

        # 剪辑决策（P2-2/P2-3）：cuts/cold_open/flash 任一非空 → 段式路径
        has_edits = bool(plan.cuts or plan.cold_open or plan.flash_forward)
        segments = None
        if has_edits:
            segments = build_segments(
                start_ms, end_ms,
                [Cut(s, e, "cut", "渲染剪除") for s, e in plan.cuts],
                cold_open=plan.cold_open,
                cold_open_jumpcut=plan.cold_open_jumpcut,
                flash_forward=plan.flash_forward,
            )
            dur_s = sum(seg.duration_ms for seg in segments) / 1000
        else:
            dur_s = (end_ms - start_ms) / 1000

        # P3 前置段：L4 生视频冷开场 或 L3 生图 2s B-roll（mock 素材禁入）
        prepend_asset = plan.hook_video or plan.broll_image
        is_image = bool(plan.broll_image) and not plan.hook_video
        prepend_dur_s = 0.0
        src_info: dict | None = None
        if prepend_asset:
            pa = Path(prepend_asset)
            if not pa.is_file():
                raise RuntimeError(f"生成素材不存在: {prepend_asset}")
            with pa.open("rb") as f:
                if f.read(5) == b"MOCK_":
                    raise RuntimeError("mock 生成素材禁入真实渲染（meta.mock 防线）")
            src_info = _probe_video_info(plan.source_path)
            prepend_dur_s = BROLL_IMAGE_S if is_image else _probe_duration_s(prepend_asset)
            if prepend_dur_s <= 0:
                raise RuntimeError(f"生成素材时长非法: {prepend_dur_s}")
        total_dur_s = dur_s + prepend_dur_s

        # 2) ASS：逐词字幕（前置段偏移）+ L1 贴片 + AIGC 显式角标；最后烧录
        ass_path: Path | None = None
        need_ass = (plan.subtitles and (plan.words or plan.hook_overlay)) or plan.aigc_badge
        if need_ass:
            offset_ms = int(prepend_dur_s * 1000)
            badge_kwargs = dict(aigc_badge=plan.aigc_badge, total_dur_ms=int(total_dur_s * 1000))
            if segments is not None:
                ass_text = build_ass_segments(
                    segments, plan.words, portrait=plan.portrait,
                    hook_overlay=plan.hook_overlay, time_offset_ms=offset_ms, **badge_kwargs,
                )
            else:
                clip_words = [w for w in plan.words if w.end_ms > start_ms and w.start_ms < end_ms]
                ass_text = build_ass(
                    clip_words, start_ms, end_ms, portrait=plan.portrait,
                    hook_overlay=plan.hook_overlay, time_offset_ms=offset_ms, **badge_kwargs,
                )
            if ass_text:
                ass_path = out.with_name(f"subs_{clip.candidate_id}.ass")
                ass_path.write_text(ass_text, encoding="utf-8")
        progress(ProgressEvent("rendering", 2, 4, "步", "响度测量"))

        # 3) 音频链 + 两遍 loudnorm（测量链与成片链一致：whoosh/TTS/前置静音）
        with_whoosh = plan.flash_forward is not None
        tts_dur_s = _probe_duration_s(plan.hook_audio) if plan.hook_audio else 0.0
        graph_mode = segments is not None or bool(plan.hook_audio) or bool(prepend_asset)
        # 输入索引：hook_audio=1（如有），前置素材紧随其后；测量链与成片链索引一致
        from studio.media import has_audio_stream

        meas_list: list[str] = [plan.hook_audio] if plan.hook_audio else []
        prepend_idx = 1 + len(meas_list)
        extra_inputs: list[str] = list(meas_list)
        if prepend_asset:
            extra_inputs.append(prepend_asset)
        prepend_has_audio = bool(prepend_asset) and not is_image and has_audio_stream(prepend_asset)
        if prepend_has_audio:
            meas_list.append(prepend_asset)
        meas_inputs = tuple(meas_list)

        if graph_mode:
            if segments is not None:
                a_chain, a_label = _audio_concat_chain(
                    segments, with_whoosh=with_whoosh, total_dur_s=dur_s
                )
                v_chain = _video_concat_chain(segments)
            else:
                a_chain = (
                    f"[0:a]atrim=start={start_ms / 1000:.3f}:end={end_ms / 1000:.3f},"
                    "asetpts=PTS-STARTPTS[acat]"
                )
                a_label = "acat"
                v_chain = (
                    f"[0:v]trim=start={start_ms / 1000:.3f}:end={end_ms / 1000:.3f},"
                    "setpts=PTS-STARTPTS[vcat]"
                )
            if plan.hook_audio:
                hook_parts, a_label = _hook_audio_parts(
                    a_label, plan.hook_duck, tts_dur_s, dur_s, input_idx=1
                )
                a_chain += ";" + ";".join(hook_parts)
            if prepend_asset:
                pre_parts, a_label = _prepend_audio_parts(
                    a_label, prepend_dur_s, prepend_idx, prepend_has_audio
                )
                a_chain += ";" + ";".join(pre_parts)
                v_chain += ";" + _prepend_video_chain(
                    prepend_idx, prepend_asset, prepend_dur_s, src_info, is_image
                )
                v_chain += ";[vcat]setsar=1[vmain];[pre][vmain]concat=n=2:v=1:a=0[vpre]"
                v_label = "vpre"
            else:
                v_label = "vcat"
            measured = measure_loudness_graph(
                plan.source_path, a_chain, a_label, plan.target_lufs, meas_inputs
            )
        else:
            a_chain = v_chain = ""
            v_label = ""
            measured = measure_loudness(plan.source_path, start_ms / 1000, end_ms / 1000, plan.target_lufs)
        af_post = (
            f"loudnorm=I={plan.target_lufs}:TP=-1.5:LRA=11"
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
            f",afade=t=in:st=0:d={FADE_S}"
            f",afade=t=out:st={max(0.0, total_dur_s - FADE_S):.3f}:d={FADE_S}"
        )
        vf_parts: list[str] = []
        if plan.portrait:
            # v1 中心裁切 9:16（人物取景后续批，回执如实记录模式）
            vf_parts.append("scale=-2:1920,crop=1080:1920")
        if ass_path is not None:
            vf_parts.append(f"ass={_ass_filter_path(ass_path)}")  # 字幕最后烧
        vf_post = ",".join(vf_parts) if vf_parts else "null"
        progress(ProgressEvent("rendering", 3, 4, "步", "切割+归一+烧字幕"))

        codec_args = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
        ]
        has_aigc = bool(plan.hook_audio or prepend_asset)
        if has_aigc:
            # AIGC 元数据标识（显式角标已烧 ASS）
            codec_args += ["-metadata", "comment=AIGC: AI-generated hook content (TTS/B-roll/video)"]
        if graph_mode:
            graph = (
                v_chain + ";" + a_chain
                + f";[{v_label}]{vf_post}[vout];[{a_label}]{af_post}[aout]"
            )
            args = ["ffmpeg", "-nostdin", "-y", "-i", plan.source_path]
            for p in extra_inputs:
                args += ["-i", p]
            args += [
                "-filter_complex", graph,
                "-map", "[vout]", "-map", "[aout]",
                *codec_args, str(out),
            ]
        else:
            args = [
                "ffmpeg", "-nostdin", "-y",
                "-ss", f"{start_ms / 1000:.3f}", "-to", f"{end_ms / 1000:.3f}",
                "-i", plan.source_path,
                "-vf", vf_post, "-af", af_post,
                *codec_args, str(out),
            ]
        r = _run(args)
        if r.returncode != 0 or not out.is_file():
            raise RuntimeError(f"渲染失败: {r.stderr[-800:]}")

        # 4) 自检：时长/流/非空
        probe = _run([
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(out),
        ])
        if probe.returncode != 0:
            raise RuntimeError(f"自检 ffprobe 失败: {probe.stderr[-300:]}")
        info = json.loads(probe.stdout)
        out_dur = float(info.get("format", {}).get("duration", 0))
        kinds = {s.get("codec_type") for s in info.get("streams", [])}
        problems: list[str] = []
        if abs(out_dur - total_dur_s) > 0.25:
            problems.append(f"时长偏差 {out_dur:.2f}s != {total_dur_s:.2f}s")
        if not {"video", "audio"} <= kinds:
            problems.append(f"流不全: {kinds}")
        if out.stat().st_size < 1000:
            problems.append("文件过小")
        if problems:
            raise RuntimeError(f"渲染自检不合格: {problems}")
        defects = scan_defects(str(out))
        progress(ProgressEvent("rendering", 4, 4, "步", "自检通过"))

        return ArtifactRef(
            kind="mp4", file_path=str(out), size_bytes=out.stat().st_size,
            meta={
                "platform": plan.platform,
                "portrait": plan.portrait,
                "portrait_mode": "center_crop_v1" if plan.portrait else None,
                "snap": snap_meta,
                "start_ms": start_ms, "end_ms": end_ms,
                "loudnorm": {"target": plan.target_lufs, "measured_I": measured["input_i"]},
                "subtitles": "ass_word_highlight" if ass_path else None,
                "selfcheck": {
                    "duration_s": round(out_dur, 3),
                    "streams": sorted(kinds),
                    "defects": defects,
                },
                # 剪辑决策回执（P2-2/P2-3）：每一刀可审计
                "editlist": {
                    "segments": [
                        {"start_ms": s.start_ms, "end_ms": s.end_ms, "kind": s.kind}
                        for s in segments
                    ] if segments is not None else None,
                    "cuts": [{"start_ms": s, "end_ms": e} for s, e in plan.cuts],
                    "cold_open": plan.cold_open,
                    "cold_open_jumpcut": plan.cold_open_jumpcut if plan.cold_open else None,
                    "flash_forward": plan.flash_forward,
                    "whoosh": with_whoosh if segments is not None else False,
                },
                # 制作钩子回执（P2-4/P2-5/P3）
                "hooks": {
                    "l1_overlay": plan.hook_overlay,
                    "l2_audio": plan.hook_audio,
                    "l2_duck": plan.hook_duck if plan.hook_audio else None,
                    "l2_duration_s": round(tts_dur_s, 3) if plan.hook_audio else None,
                    "broll_image": plan.broll_image,
                    "hook_video": plan.hook_video,
                    "prepend_dur_s": round(prepend_dur_s, 3) if prepend_asset else None,
                    "prepend_audio": ("source" if prepend_has_audio else "silence") if prepend_asset else None,
                    "aigc_badge": plan.aigc_badge,
                    "aigc_labeled": has_aigc and plan.aigc_badge,
                },
            },
        )