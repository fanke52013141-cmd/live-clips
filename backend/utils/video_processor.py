"""
视频处理工具
"""
import subprocess
import json
import logging
import re
from typing import List, Dict, Optional
from pathlib import Path
from .ffmpeg_utils import get_ffprobe_path, get_ffmpeg_path, is_safe_returncode, stderr_tail
from .disk_guard import ensure_disk_space

# 修复导入问题
try:
    from ..core.shared_config import CLIPS_DIR, COLLECTIONS_DIR
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    import sys
    from pathlib import Path
    backend_path = Path(__file__).parent.parent
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    from ..core.shared_config import CLIPS_DIR, COLLECTIONS_DIR

logger = logging.getLogger(__name__)

class VideoProcessor:
    """视频处理工具类"""
    
    def __init__(self, clips_dir: Optional[str] = None, collections_dir: Optional[str] = None):
        # 强制使用传入的项目特定路径，不使用全局路径作为后备
        if not clips_dir:
            raise ValueError("clips_dir 参数是必需的，不能使用全局路径")
        if not collections_dir:
            raise ValueError("collections_dir 参数是必需的，不能使用全局路径")
        
        self.clips_dir = Path(clips_dir)
        self.collections_dir = Path(collections_dir)
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        清理文件名，移除或替换不合法的字符
        
        Args:
            filename: 原始文件名
            
        Returns:
            清理后的文件名
        """
        # 移除或替换不合法的字符
        # Windows和Unix系统都不允许的字符: < > : " | ? * \ /
        # 替换为下划线
        sanitized = re.sub(r'[<>:"|?*\\/]', '_', filename)
        
        # 移除前后空格和点
        sanitized = sanitized.strip(' .')
        
        # 限制长度，避免文件名过长
        if len(sanitized) > 100:
            sanitized = sanitized[:100]
        
        # 确保文件名不为空
        if not sanitized:
            sanitized = "untitled"
            
        return sanitized
    
    @staticmethod
    def convert_srt_time_to_ffmpeg_time(srt_time: str) -> str:
        """
        将SRT时间格式转换为FFmpeg时间格式
        
        Args:
            srt_time: SRT时间格式 (如 "00:00:06,140" 或 "00:00:06.140")
            
        Returns:
            FFmpeg时间格式 (如 "00:00:06.140")
        """
        # 将逗号替换为点
        return srt_time.replace(',', '.')
    
    @staticmethod
    def convert_seconds_to_ffmpeg_time(seconds: float) -> str:
        """
        将秒数转换为FFmpeg时间格式
        
        Args:
            seconds: 秒数
            
        Returns:
            FFmpeg时间格式 (如 "00:00:06.140")
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        milliseconds = int((seconds % 1) * 1000)
        
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"
    
    @staticmethod
    def convert_ffmpeg_time_to_seconds(time_str: str) -> float:
        """
        将FFmpeg时间格式转换为秒数
        
        Args:
            time_str: FFmpeg时间格式 (如 "00:00:06.140")
            
        Returns:
            秒数
        """
        try:
            # 处理毫秒部分
            if '.' in time_str:
                time_part, ms_part = time_str.split('.')
                milliseconds = int(ms_part)
            else:
                time_part = time_str
                milliseconds = 0
            
            # 解析时分秒
            h, m, s = map(int, time_part.split(':'))
            
            return h * 3600 + m * 60 + s + milliseconds / 1000
        except Exception as e:
            logger.error(f"时间格式转换失败: {time_str}, 错误: {e}")
            return 0.0
    
    @staticmethod
    def build_extract_clip_command(
        ffmpeg_bin: str,
        input_video: Path,
        output_path: Path,
        start_time: str,
        duration: float,
    ) -> List[str]:
        """切片编码成应用内 <video> 能播的 H.264 + AAC。

        以前用 `-c:v copy`。B 站 `bestvideo[ext=mp4]` 经常是 HEVC，Windows
        WebView2 / macOS WKWebView 没有系统 HEVC 解码器时画面是黑的，声音可能还在。
        流复制还会留下非零 start time 的 edit list，Chromium 系播放器同样黑屏。
        合集导出已经转成 H.264；单条切片以前没有。

        `-fps_mode cfr` 把可变帧率源（直播平台下载源常见）压成恒定帧率，配合
        `aresample=async=1` 防止时长相对音频漂移导致音画不同步；
        `+negative_cts_offsets` 修复 B 帧/AAC priming 造成的首帧黑帧
        （部分播放器与上传通道忽略 edit list，首帧显示时间会滞后 10-20ms）。
        """
        return [
            ffmpeg_bin,
            "-ss", start_time,
            "-i", str(input_video),
            "-t", f"{duration:.3f}",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr",
            "-af", "aresample=async=1:first_pts=0",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart+negative_cts_offsets",
            "-avoid_negative_ts", "make_zero",
            "-y",
            str(output_path),
        ]

    @staticmethod
    def build_draft_extract_command(
        ffmpeg_bin: str,
        input_video: Path,
        output_path: Path,
        start_time: str,
        duration: float,
        *,
        reencode: bool,
    ) -> List[str]:
        """预览草稿档（OP-3.1）：预览确认用，成片一律走 build_extract_clip_command 精渲染。

        reencode=False：源已是浏览器可播的 H.264/AAC，流复制秒级出片；切点吸附
        关键帧最多偏几秒，是流复制的固有代价，只接受于预览场景。
        reencode=True：HEVC/AV1 等浏览器不支持的源回退 ultrafast 重编码——宁可慢，
        也不能产出应用内黑屏的"预览"（兼容性说明见 build_extract_clip_command）。
        """
        if reencode:
            return [
                ffmpeg_bin,
                "-ss", start_time,
                "-i", str(input_video),
                "-t", f"{duration:.3f}",
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-fps_mode", "cfr",
                "-af", "aresample=async=1:first_pts=0",
                "-c:a", "aac",
                "-b:a", "96k",
                "-movflags", "+faststart+negative_cts_offsets",
                "-avoid_negative_ts", "make_zero",
                "-y",
                str(output_path),
            ]
        # 流复制路径实测（FFmpeg 8.1）：`-avoid_negative_ts make_zero` 与输入端 seek
        # 组合会把 mp4 容器时长错位（请求 4s 实得 6s），故 copy 分支不加该参数；
        # 首帧黑帧防护由 `+negative_cts_offsets` 承担。
        return [
            ffmpeg_bin,
            "-ss", start_time,
            "-i", str(input_video),
            "-t", f"{duration:.3f}",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart+negative_cts_offsets",
            "-y",
            str(output_path),
        ]

    @staticmethod
    def extract_clip_draft(input_video: Path, output_path: Path,
                           start_time: str, end_time: str) -> bool:
        """草稿档切片：探测源编码后自动选择流复制或轻量重编码。

        仅用于预览/草稿；终稿必须走 extract_clip（完整兼容性重编码）。
        """
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            ffmpeg_start = VideoProcessor.convert_srt_time_to_ffmpeg_time(start_time)
            ffmpeg_end = VideoProcessor.convert_srt_time_to_ffmpeg_time(end_time)
            duration = (VideoProcessor.convert_ffmpeg_time_to_seconds(ffmpeg_end)
                        - VideoProcessor.convert_ffmpeg_time_to_seconds(ffmpeg_start))
            if duration <= 0:
                logger.error(f"草稿切片时长非法: {duration:.2f}秒 ({start_time} -> {end_time})")
                return False
            info = VideoProcessor.get_video_info(input_video)
            codecs = {s.get('codec_name') for s in info.get('streams', [])}
            # 只有源本身就是浏览器可播的 H.264（音频至多 AAC/MP3）才允许流复制
            reencode = 'h264' not in codecs or bool(codecs & {'hevc', 'av1', 'vp9'})
            ffmpeg_bin = get_ffmpeg_path()
            cmd = VideoProcessor.build_draft_extract_command(
                ffmpeg_bin, input_video, output_path, ffmpeg_start, duration, reencode=reencode,
            )
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            if is_safe_returncode(result.returncode) and output_path.exists():
                mode = '重编码' if reencode else '流复制'
                logger.info(f"草稿切片完成({mode}): {output_path} ({duration:.2f}秒)")
                return True
            logger.error(f"草稿切片失败: {stderr_tail(result.stderr)}")
            return False
        except Exception as e:
            logger.error(f"草稿切片异常: {str(e)}")
            return False

    @staticmethod
    def extract_clip(input_video: Path, output_path: Path,
                    start_time: str, end_time: str) -> bool:
        """
        从视频中提取指定时间段的片段
        
        Args:
            input_video: 输入视频路径
            output_path: 输出视频路径
            start_time: 开始时间 (格式: "00:01:25,140")
            end_time: 结束时间 (格式: "00:02:53,500")
            
        Returns:
            是否成功
        """
        try:
            # 确保输出目录存在
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 转换时间格式：从SRT格式转换为FFmpeg格式
            ffmpeg_start_time = VideoProcessor.convert_srt_time_to_ffmpeg_time(start_time)
            ffmpeg_end_time = VideoProcessor.convert_srt_time_to_ffmpeg_time(end_time)
            
            # 计算持续时间
            start_seconds = VideoProcessor.convert_ffmpeg_time_to_seconds(ffmpeg_start_time)
            end_seconds = VideoProcessor.convert_ffmpeg_time_to_seconds(ffmpeg_end_time)
            duration = end_seconds - start_seconds
            
            # 转成浏览器能解的 H.264/yuv420p + AAC，并把 moov 放到文件头。
            ffmpeg_bin = get_ffmpeg_path()
            cmd = VideoProcessor.build_extract_clip_command(
                ffmpeg_bin, input_video, output_path, ffmpeg_start_time, duration,
            )
            
            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            
            if result.returncode == 0:
                logger.info(f"成功提取视频片段: {output_path} ({ffmpeg_start_time} -> {ffmpeg_end_time}, 时长: {duration:.2f}秒)")
                return True
            else:
                logger.error(f"提取视频片段失败: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"视频处理异常: {str(e)}")
            return False
    
    @staticmethod
    def create_collection(clips_list: List[Path], output_path: Path) -> bool:
        """
        将多个视频片段拼接成合集
        
        Args:
            clips_list: 视频片段路径列表
            output_path: 输出合集路径
            
        Returns:
            是否成功
        """
        try:
            # 验证输入参数
            if not clips_list:
                logger.error("clips_list为空，无法创建合集")
                return False
            
            # 验证所有视频文件是否存在
            valid_clips = []
            for clip_path in clips_list:
                if not clip_path.exists():
                    logger.warning(f"视频文件不存在，跳过: {clip_path}")
                    continue
                valid_clips.append(clip_path)
            
            if not valid_clips:
                logger.error("没有有效的视频文件，无法创建合集")
                return False
            
            # 确保输出目录存在
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 创建concat文件
            concat_file = output_path.parent / "concat_list.txt"
            
            with open(concat_file, 'w', encoding='utf-8') as f:
                for clip_path in valid_clips:
                    # 使用绝对路径并转义单引号
                    abs_path = clip_path.absolute()
                    escaped_path = str(abs_path).replace("'", "'\"'\"'")
                    f.write(f"file '{escaped_path}'\n")
            
            # 验证concat文件内容
            if concat_file.stat().st_size == 0:
                logger.error("concat文件为空，无法创建合集")
                concat_file.unlink(missing_ok=True)
                return False

            # 磁盘熔断（OP-6.5）：合集输出可能有几 GB，写满盘会让文件损坏
            ensure_disk_space(output_path.parent)

            # 先探测所有输入的编码参数：本流水线产出的切片都是同参数 H.264/AAC，
            # 走 concat demuxer 流复制秒级完成且无二次编码的质量损失；
            # 混入异参数外部视频时流复制会产出坏文件（还不一定报错），
            # 此时回退到重编码路径。
            ffmpeg_bin = get_ffmpeg_path()
            cmd: List[str]
            if VideoProcessor._streams_uniform_for_copy(valid_clips):
                cmd = [
                    ffmpeg_bin,
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', str(concat_file),
                    '-c', 'copy',
                    '-movflags', '+faststart+negative_cts_offsets',
                    '-y',
                    str(output_path),
                ]
                logger.info("合集输入编码一致，使用流复制快速拼接")
            else:
                # 构建FFmpeg命令 - 使用H.264编码确保兼容性
                cmd = [
                    ffmpeg_bin,
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', str(concat_file),
                    '-c:v', 'libx264',  # 使用H.264视频编码
                    '-preset', 'ultrafast',  # 使用最快的编码预设
                    '-crf', '28',  # 稍微降低质量以加快编码速度
                    '-c:a', 'aac',  # 使用AAC音频编码
                    '-b:a', '128k',  # 音频比特率
                    '-movflags', '+faststart+negative_cts_offsets',  # 优化网络播放并防首帧黑帧
                    '-y',
                    str(output_path)
                ]

            logger.info(f"执行FFmpeg命令: {' '.join(cmd)}")

            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

            # 清理临时文件
            concat_file.unlink(missing_ok=True)

            if is_safe_returncode(result.returncode) and output_path.exists():
                logger.info(f"成功创建合集: {output_path}")
                return True
            else:
                logger.error(f"创建合集失败: {stderr_tail(result.stderr)}")
                return False

        except Exception as e:
            logger.error(f"视频拼接异常: {str(e)}")
            return False

    @staticmethod
    def _streams_uniform_for_copy(clips: List[Path]) -> bool:
        """所有输入的视频/音频编码参数一致时才允许流复制拼接。

        ffprobe 比较视频编码、分辨率与音频编码；任一探测失败按"不一致"处理，
        宁可回退到重编码也不产出坏文件。
        """
        ffprobe_bin = get_ffprobe_path()
        signature: Optional[tuple] = None
        for clip in clips:
            try:
                probe = subprocess.run(
                    [ffprobe_bin, '-v', 'quiet', '-print_format', 'json',
                     '-show_streams', str(clip)],
                    capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=30,
                )
                if probe.returncode != 0:
                    return False
                streams = json.loads(probe.stdout or '{}').get('streams', [])
                video = next((s for s in streams if s.get('codec_type') == 'video'), None)
                audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
                if video is None:
                    return False
                sig = (
                    video.get('codec_name'),
                    video.get('width'),
                    video.get('height'),
                    video.get('r_frame_rate'),
                    audio.get('codec_name') if audio else None,
                    audio.get('sample_rate') if audio else None,
                )
                if signature is None:
                    signature = sig
                elif sig != signature:
                    return False
            except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
                return False
        return signature is not None

    @staticmethod
    def extract_thumbnail(video_path: Path, output_path: Path, time_offset: int = 5) -> bool:
        """
        从视频中提取缩略图
        
        Args:
            video_path: 视频文件路径
            output_path: 输出缩略图路径
            time_offset: 提取时间点（秒）
            
        Returns:
            是否成功
        """
        try:
            # 确保输出目录存在
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 构建FFmpeg命令：-ss 放在 -i 之前做输入端寻址，长视频抽帧从分钟级降到秒级
            ffmpeg_bin = get_ffmpeg_path()
            cmd = [
                ffmpeg_bin,
                '-ss', str(time_offset),
                '-i', str(video_path),
                '-vframes', '1',
                '-q:v', '2',  # 高质量
                '-y',  # 覆盖输出文件
                str(output_path)
            ]

            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

            if is_safe_returncode(result.returncode) and output_path.exists():
                logger.info(f"成功提取缩略图: {output_path}")
                return True
            else:
                logger.error(f"提取缩略图失败: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"提取缩略图异常: {str(e)}")
            return False
    
    @staticmethod
    def get_video_info(video_path: Path) -> Dict:
        """
        获取视频信息
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            视频信息字典
        """
        try:
            ffprobe_bin = get_ffprobe_path()
            cmd = [
                ffprobe_bin,
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                str(video_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            
            if result.returncode == 0:
                info = json.loads(result.stdout)
                return {
                    'duration': float(info['format']['duration']),
                    'size': int(info['format']['size']),
                    'bitrate': int(info['format']['bit_rate']),
                    'streams': info['streams']
                }
            else:
                logger.error(f"获取视频信息失败: {result.stderr}")
                return {}
                
        except Exception as e:
            logger.error(f"获取视频信息异常: {str(e)}")
            return {}
    
    def batch_extract_clips(self, input_video: Path, clips_data: List[Dict]) -> List[Path]:
        """
        批量提取视频片段
        
        Args:
            input_video: 输入视频路径
            clips_data: 片段数据列表，每个元素包含id、title、start_time、end_time
            
        Returns:
            成功提取的片段路径列表
        """
        successful_clips = []
        
        for clip_data in clips_data:
            clip_id = clip_data['id']
            title = clip_data.get('title', f"片段_{clip_id}")
            start_time = clip_data['start_time']
            end_time = clip_data['end_time']
            
            # 处理时间格式 - 如果是秒数，转换为SRT格式
            if isinstance(start_time, (int, float)):
                start_time = VideoProcessor.convert_seconds_to_ffmpeg_time(start_time)
            if isinstance(end_time, (int, float)):
                end_time = VideoProcessor.convert_seconds_to_ffmpeg_time(end_time)
            
            # 使用标题作为文件名，并清理不合法的字符
            # 在文件名中包含clip_id，便于后续合集拼接时查找
            safe_title = VideoProcessor.sanitize_filename(title)
            output_path = self.clips_dir / f"{clip_id}_{safe_title}.mp4"
            
            logger.info(f"提取切片 {clip_id}: {start_time} -> {end_time}, 输出: {output_path}")
            
            if VideoProcessor.extract_clip(input_video, output_path, start_time, end_time):
                successful_clips.append(output_path)
                logger.info(f"切片 {clip_id} 提取成功")
            else:
                logger.error(f"切片 {clip_id} 提取失败")
        
        return successful_clips
    
    def create_collections_from_metadata(self, collections_data: List[Dict]) -> List[Dict]:
        """
        根据元数据创建合集
        
        Args:
            collections_data: 合集数据列表
            
        Returns:
            成功创建的合集信息列表，包含视频路径和缩略图路径
        """
        successful_collections = []
        
        for collection_data in collections_data:
            collection_id = collection_data['id']
            collection_title = collection_data.get('collection_title', f'合集_{collection_id}')
            clip_ids = collection_data['clip_ids']
            
            # 构建片段路径列表
            clips_list = []
            for clip_id in clip_ids:
                # 查找对应的切片文件
                # 新的文件名格式是: {clip_id}_{title}.mp4
                clip_path = self.clips_dir / f"{clip_id}_*.mp4"
                found_clips = list(self.clips_dir.glob(f"{clip_id}_*.mp4"))
                
                if found_clips:
                    found_clip = found_clips[0]  # 取第一个匹配的文件
                    clips_list.append(found_clip)
                    logger.info(f"找到合集 {collection_id} 的切片: {found_clip.name}")
                else:
                    logger.warning(f"未找到合集 {collection_id} 的切片 {clip_id}")
            
            if clips_list:
                # 使用collection_title作为文件名，并清理不合法的字符
                safe_title = VideoProcessor.sanitize_filename(collection_title)
                output_path = self.collections_dir / f"{safe_title}.mp4"
                
                if VideoProcessor.create_collection(clips_list, output_path):
                    # 生成合集缩略图
                    thumbnail_path = None
                    try:
                        thumbnail_filename = f"{collection_id}_{safe_title}_thumbnail.jpg"
                        thumbnail_path = self.collections_dir / thumbnail_filename
                        
                        # 从视频中提取缩略图（第2秒的帧）
                        thumbnail_success = VideoProcessor.extract_thumbnail(output_path, thumbnail_path, time_offset=2)
                        if thumbnail_success:
                            logger.info(f"合集 {collection_id} 缩略图生成成功: {thumbnail_path}")
                        else:
                            logger.warning(f"合集 {collection_id} 缩略图生成失败")
                            thumbnail_path = None
                    except Exception as e:
                        logger.error(f"生成合集 {collection_id} 缩略图时出错: {e}")
                        thumbnail_path = None
                    
                    # 返回包含视频路径和缩略图路径的信息
                    collection_info = {
                        'collection_id': collection_id,
                        'video_path': str(output_path),
                        'thumbnail_path': str(thumbnail_path) if thumbnail_path else None,
                        'title': collection_title
                    }
                    successful_collections.append(collection_info)
                    logger.info(f"成功创建合集 {collection_id}: {output_path}")
            else:
                logger.warning(f"合集 {collection_id} 没有找到任何有效的切片文件")
        
        return successful_collections