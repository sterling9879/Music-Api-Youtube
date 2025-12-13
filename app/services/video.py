"""
Video processing service for looping videos and combining with audio.
Optimized for HD output and maximum resource utilization.
"""

import subprocess
import logging
import math
import os
from pathlib import Path
from typing import Optional, Tuple

from app.config import ALLOWED_VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

# HD Output settings
HD_WIDTH = 1920
HD_HEIGHT = 1080
OUTPUT_FPS = 30


class VideoProcessingError(Exception):
    """Custom exception for video processing errors."""
    pass


def get_system_resources():
    """Get system resources for optimal encoding."""
    try:
        cpu_count = os.cpu_count() or 4

        # Get available memory in MB
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if 'MemAvailable' in line:
                        mem_kb = int(line.split()[1])
                        mem_mb = mem_kb // 1024
                        break
                else:
                    mem_mb = 4096  # Default 4GB
        except Exception:
            mem_mb = 4096

        return cpu_count, mem_mb
    except Exception:
        return 4, 4096


def get_video_info(file_path: Path) -> Tuple[float, int, int, float]:
    """
    Get video information using ffprobe.
    """
    try:
        duration_result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path)
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        stream_result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate",
                "-of", "csv=p=0",
                str(file_path)
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        if duration_result.returncode != 0:
            raise VideoProcessingError(f"Failed to get duration: {duration_result.stderr}")

        duration = float(duration_result.stdout.strip())

        stream_parts = stream_result.stdout.strip().split(",")
        if len(stream_parts) >= 3:
            width = int(stream_parts[0])
            height = int(stream_parts[1])
            fps_parts = stream_parts[2].split("/")
            fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 else 30.0
        else:
            width, height, fps = 1920, 1080, 30.0

        return duration, width, height, fps

    except subprocess.TimeoutExpired:
        raise VideoProcessingError("ffprobe timeout")
    except ValueError as e:
        raise VideoProcessingError(f"Invalid video info: {e}")


def validate_video_format(file_path: Path) -> bool:
    """Validate that the file is a supported video format."""
    if not file_path.exists():
        raise VideoProcessingError(f"File not found: {file_path}")

    if file_path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise VideoProcessingError(
            f"Unsupported format: {file_path.suffix}. "
            f"Allowed: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path)
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        if "video" not in result.stdout.lower():
            raise VideoProcessingError("File does not contain a video stream")

        return True

    except subprocess.TimeoutExpired:
        raise VideoProcessingError("Video validation timeout")


class VideoProcessor:
    """Service for processing video files with maximum resource utilization."""

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # Get system resources
        self.cpu_count, self.mem_mb = get_system_resources()
        self.threads = self.cpu_count  # Use ALL CPU cores

        # Calculate optimal buffer sizes based on available memory
        # Use up to 70% of available memory for FFmpeg
        usable_mem = int(self.mem_mb * 0.7)
        self.buffer_size = min(usable_mem, 2048)  # Max 2GB buffer per operation

        logger.info(f"VideoProcessor initialized: {self.threads} threads, {self.mem_mb}MB RAM available, {self.buffer_size}MB buffer")

    def loop_video_to_duration(
        self,
        input_video: Path,
        target_duration: float,
        output_path: Path
    ) -> Path:
        """
        Create a looped video in HD with maximum resource utilization.
        """
        validate_video_format(input_video)

        video_duration, width, height, fps = get_video_info(input_video)

        if video_duration <= 0:
            raise VideoProcessingError("Invalid video duration")

        num_loops = math.ceil(target_duration / video_duration)

        logger.info(
            f"Looping video {num_loops} times "
            f"(source: {video_duration:.2f}s @ {width}x{height}, target: {target_duration:.2f}s @ {HD_WIDTH}x{HD_HEIGHT})"
        )
        logger.info(f"Using {self.threads} threads, {self.buffer_size}MB buffer")

        # Scale filter for HD output
        scale_filter = f"scale={HD_WIDTH}:{HD_HEIGHT}:force_original_aspect_ratio=decrease,pad={HD_WIDTH}:{HD_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"

        try:
            result = self._loop_with_concat_optimized(
                input_video, target_duration, output_path, num_loops, scale_filter
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg loop error: {result.stderr}")
                raise VideoProcessingError(f"Video looping failed: {result.stderr[-500:]}")

            logger.info(f"Created HD looped video at {output_path}")
            return output_path

        except subprocess.TimeoutExpired:
            raise VideoProcessingError("Video looping timeout (exceeded 2 hours)")

    def _loop_with_concat_optimized(
        self,
        input_video: Path,
        target_duration: float,
        output_path: Path,
        num_loops: int,
        scale_filter: str
    ) -> subprocess.CompletedProcess:
        """
        Loop video with maximum CPU and memory utilization.
        """
        concat_file = self.work_dir / "concat_list.txt"
        with open(concat_file, "w") as f:
            for _ in range(num_loops):
                f.write(f"file '{input_video.absolute()}'\n")

        logger.info(f"Optimized encoding: {num_loops} loops, {self.threads} threads, {self.buffer_size}MB buffer")

        # Calculate x264 specific threading options
        lookahead_threads = max(1, self.threads // 4)

        cmd = [
            "ffmpeg",
            "-y",
            # Input options - large buffer for fast reading
            "-thread_queue_size", "4096",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            # Duration limit
            "-t", str(target_duration),
            # Video filters
            "-vf", f"{scale_filter},fps={OUTPUT_FPS}",
            # Codec settings - optimized for speed
            "-c:v", "libx264",
            "-preset", "ultrafast",  # Fastest preset - uses more CPU but finishes faster
            "-tune", "fastdecode",   # Optimize for fast decoding
            "-crf", "23",            # Slightly lower quality for speed
            "-profile:v", "high",
            "-level", "4.1",
            "-pix_fmt", "yuv420p",
            # Threading - use ALL resources
            "-threads", str(self.threads),
            "-x264-params", f"threads={self.threads}:lookahead_threads={lookahead_threads}:sliced_threads=1",
            # Memory/Buffer optimization
            "-bufsize", f"{self.buffer_size}M",
            "-maxrate", "20M",       # High bitrate for quality
            # Disable audio
            "-an",
            # Output optimization
            "-movflags", "+faststart",
            str(output_path)
        ]

        logger.info(f"Running FFmpeg with optimized settings...")

        # Set environment for maximum resource usage
        env = os.environ.copy()
        env['FFREPORT'] = f'file={self.work_dir}/ffmpeg.log:level=32'

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,
            env=env
        )

        concat_file.unlink(missing_ok=True)
        return result

    def combine_video_audio(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        video_codec: str = "copy"
    ) -> Path:
        """
        Combine video and audio with stream copy (instant).
        """
        try:
            logger.info(f"Combining video and audio (codec: {video_codec}, threads: {self.threads})...")

            cmd = [
                "ffmpeg",
                "-y",
                "-thread_queue_size", "4096",
                "-i", str(video_path),
                "-i", str(audio_path),
                "-c:v", video_codec,
                "-c:a", "aac",
                "-b:a", "320k",
                "-ar", "48000",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                "-movflags", "+faststart",
                "-threads", str(self.threads),
                str(output_path)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg combine error: {result.stderr}")
                raise VideoProcessingError(f"Video/audio combining failed: {result.stderr}")

            logger.info(f"Created final HD video at {output_path}")
            return output_path

        except subprocess.TimeoutExpired:
            raise VideoProcessingError("Video combining timeout")

    def process_video_with_audio(
        self,
        input_video: Path,
        audio_path: Path,
        audio_duration: float,
        output_path: Path
    ) -> Path:
        """
        Complete video processing pipeline with maximum resource usage.
        """
        logger.info(f"=" * 60)
        logger.info(f"Starting HD video processing pipeline")
        logger.info(f"System: {self.cpu_count} CPUs, {self.mem_mb}MB RAM")
        logger.info(f"Using: {self.threads} threads, {self.buffer_size}MB buffer")
        logger.info(f"Input: {input_video}")
        logger.info(f"Target: {audio_duration:.2f}s ({audio_duration/60:.1f} min) @ {HD_WIDTH}x{HD_HEIGHT}")
        logger.info(f"=" * 60)

        looped_video = self.work_dir / "looped_video.mp4"
        self.loop_video_to_duration(input_video, audio_duration, looped_video)

        self.combine_video_audio(looped_video, audio_path, output_path)

        if looped_video.exists():
            looped_video.unlink()

        logger.info(f"HD video processing complete: {output_path}")
        return output_path

    def extract_thumbnail(
        self,
        video_path: Path,
        output_path: Path,
        timestamp: str = "00:00:05"
    ) -> Path:
        """Extract a thumbnail from the video."""
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i", str(video_path),
                    "-ss", timestamp,
                    "-vframes", "1",
                    "-q:v", "2",
                    str(output_path)
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.warning(f"Thumbnail extraction failed: {result.stderr}")
                return None

            return output_path

        except subprocess.TimeoutExpired:
            return None


def create_video_processor(work_dir: Path) -> VideoProcessor:
    """Factory function to create a VideoProcessor instance."""
    return VideoProcessor(work_dir)
