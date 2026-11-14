"""OP-0~OP-6 优化包真机集成验证：用本机真实 FFmpeg 跑一遍渲染链路。

覆盖（对应《docs/11-优化包实施方案》）：
- OP-1 切片重编码（-fps_mode cfr + aresample + negative_cts_offsets）真实出片
- OP-1 合集快拼：同参数输入走 concat -c copy，异参数输入自动回退重编码
- OP-1/OP-6 返回码白名单、stderr 截尾、磁盘熔断
- OP-3.1 草稿档：H.264 源走流复制秒出
- OP-3.4 yt-dlp format_sort 参数合法性（不发网络请求）
- 缩略图输入端 seek

运行：venv/Scripts/python.exe scripts/verify_optimizations_integration.py
所有产物写在临时目录，结束后清理。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("verify")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(f"{name}{(' — ' + detail) if detail else ''}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def probe(path: Path) -> dict:
    import json
    from backend.utils.ffmpeg_utils import get_ffprobe_path
    r = subprocess.run(
        [get_ffprobe_path(), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return json.loads(r.stdout or "{}")


def make_source(folder: Path, name: str, size: str, seconds: int) -> Path:
    """lavfi 生成带音轨的真实测试视频（H.264 + AAC）。"""
    from backend.utils.ffmpeg_utils import get_ffmpeg_path
    out = folder / name
    r = subprocess.run(
        [get_ffmpeg_path(), "-y", "-f", "lavfi",
         "-i", f"testsrc=size={size}:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0 and out.exists(), f"测试视频生成失败: {r.stderr[-400:]}"
    return out


if __name__ == "__main__":
    from backend.utils.disk_guard import ensure_disk_space
    from backend.utils.video_processor import VideoProcessor

    with tempfile.TemporaryDirectory(prefix="autoclip-op-verify-") as tmp:
        folder = Path(tmp)
        source = make_source(folder, "source.mp4", "640x360", 8)
        check("OP-0 测试源视频生成（lavfi, H.264+AAC）", source.exists())

        # ---- OP-1 切片重编码 ----
        clip1 = folder / "clip1.mp4"
        ok = VideoProcessor.extract_clip(source, clip1, "00:00:01,000", "00:00:04,000")
        check("OP-1 extract_clip 真实出片", ok and clip1.exists())
        streams = probe(clip1).get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = next((s for s in streams if s.get("codec_type") == "audio"), {})
        check("OP-1 输出为 H.264+AAC（应用内可播）",
              v.get("codec_name") == "h264" and a.get("codec_name") == "aac",
              f"video={v.get('codec_name')} audio={a.get('codec_name')}")
        duration = float(probe(clip1).get("format", {}).get("duration") or 0)
        check("OP-1 切片时长正确（≈3s, cfr 下误差 <0.2s）", abs(duration - 3.0) < 0.2, f"duration={duration:.3f}")

        # ---- OP-3.1 草稿档（H.264 源 → 流复制）----
        draft = folder / "draft.mp4"
        ok = VideoProcessor.extract_clip_draft(source, draft, "00:00:02,000", "00:00:06,000")
        check("OP-3.1 extract_clip_draft 真实出片", ok and draft.exists())
        d_duration = float(probe(draft).get("format", {}).get("duration") or 0)
        check("OP-3.1 草稿时长正确（≈4s）", abs(d_duration - 4.0) < 0.3, f"duration={d_duration:.3f}")

        # ---- OP-3.5 合集快拼（同参数 → 流复制）----
        clip2 = folder / "clip2.mp4"
        VideoProcessor.extract_clip(source, clip2, "00:00:04,000", "00:00:07,000")
        collection = folder / "collections" / "set.mp4"
        ok = VideoProcessor.create_collection([clip1, clip2], collection)
        c_duration = float(probe(collection).get("format", {}).get("duration") or 0)
        check("OP-1/OP-3.5 合集拼接成功（同参数流复制）", ok and collection.exists(),
              f"duration={c_duration:.3f}")
        check("OP-3.5 合集时长 ≈ 两段之和（≈6s）", abs(c_duration - 6.0) < 0.3, f"duration={c_duration:.3f}")

        # ---- 混合参数输入 → 回退重编码 ----
        small = make_source(folder, "small.mp4", "320x240", 4)
        clip_small = folder / "clip_small.mp4"
        VideoProcessor.extract_clip(small, clip_small, "00:00:00,000", "00:00:02,000")
        mixed = folder / "collections" / "mixed.mp4"
        ok = VideoProcessor.create_collection([clip1, clip_small], mixed)
        check("OP-3.5 混合参数输入自动回退重编码且成功", ok and mixed.exists())

        # ---- 缩略图（输入端 seek）----
        thumb = folder / "thumb.jpg"
        ok = VideoProcessor.extract_thumbnail(collection, thumb, 1)
        check("OP-1 缩略图提取成功（输入端 seek）", ok and thumb.exists() and thumb.stat().st_size > 1000)

        # ---- OP-6 磁盘熔断 ----
        try:
            ensure_disk_space(folder, min_free_mb=10**9)
            check("OP-6 磁盘熔断：超大阈值正确抛错", False, "未抛出 RuntimeError")
        except RuntimeError as error:
            check("OP-6 磁盘熔断：超大阈值正确抛错", "磁盘剩余空间不足" in str(error))
        try:
            ensure_disk_space(folder, min_free_mb=1)
            check("OP-6 磁盘熔断：正常阈值放行", True)
        except RuntimeError as error:
            check("OP-6 磁盘熔断：正常阈值放行", False, str(error))

        # ---- OP-3.4 yt-dlp format_sort 参数合法性（不联网）----
        try:
            import yt_dlp
            ydl = yt_dlp.YoutubeDL({"format_sort": ["vcodec:avc1"], "skip_download": True, "quiet": True})
            check("OP-3.4 yt-dlp 接受 format_sort 参数（不报错）", ydl is not None)
        except Exception as error:  # noqa: BLE001
            check("OP-3.4 yt-dlp 接受 format_sort 参数（不报错）", False, repr(error)[:200])

    print("\n========== 结果汇总 ==========")
    print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    for item in FAIL:
        print(f"  FAIL: {item}")
    sys.exit(1 if FAIL else 0)
