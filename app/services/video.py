"""
Video processing service for looping videos and combining with audio.
"""

import subprocess
import logging
import math
from pathlib import Path
from typing import Optional, Tuple

from app.config import ALLOWED_VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)


class VideoProcessingError(Exception):
    """Custom exception for video processing errors."""
    pass


def get_video_info(file_path: Path) -> Tuple[float, int, int, float]:
    """
    Get video information using ffprobe.

    Args:
        file_path: Path to the video file

    Returns:
        Tuple of (duration_seconds, width, height, fps)
    """
    try:
        # Get duration
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

        # Get video stream info
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

        # Parse stream info
        stream_parts = stream_result.stdout.strip().split(",")
        if len(stream_parts) >= 3:
            width = int(stream_parts[0])
            height = int(stream_parts[1])
            fps_parts = stream_parts[2].split("/")
            fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 else 30.0
        else:
            # Default values if parsing fails
            width, height, fps = 1920, 1080, 30.0

        return duration, width, height, fps

    except subprocess.TimeoutExpired:
        raise VideoProcessingError("ffprobe timeout")
    except ValueError as e:
        raise VideoProcessingError(f"Invalid video info: {e}")


def validate_video_format(file_path: Path) -> bool:
    """
    Validate that the file is a supported video format.

    Args:
        file_path: Path to the video file

    Returns:
        True if valid, raises exception otherwise
    """
    if not file_path.exists():
        raise VideoProcessingError(f"File not found: {file_path}")

    if file_path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise VideoProcessingError(
            f"Unsupported format: {file_path.suffix}. "
            f"Allowed: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    # Verify it's a valid video file
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
    """Service for processing video files."""

    def __init__(self, work_dir: Path):
        """
        Initialize the video processor.

        Args:
            work_dir: Working directory for temporary files
        """
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def loop_video_to_duration(
        self,
        input_video: Path,
        target_duration: float,
        output_path: Path
    ) -> Path:
        """
        Create a looped video that matches the target duration.

        Args:
            input_video: Path to the input video
            target_duration: Target duration in seconds
            output_path: Path for the output video

        Returns:
            Path to the looped video
        """
        validate_video_format(input_video)

        video_duration, width, height, fps = get_video_info(input_video)

        if video_duration <= 0:
            raise VideoProcessingError("Invalid video duration")

        # Calculate how many loops we need
        num_loops = math.ceil(target_duration / video_duration)

        logger.info(
            f"Looping video {num_loops} times "
            f"(source: {video_duration:.2f}s, target: {target_duration:.2f}s)"
        )

        # For .mov files or if stream_loop fails, use filter_complex approach
        input_ext = input_video.suffix.lower()

        try:
            if input_ext == ".mov" or num_loops > 100:
                # For MOV files, first convert to MP4, then loop
                # This is more reliable than stream_loop with MOV
                logger.info("Using filter-based looping for MOV file...")

                # Method: Use loop filter which is more compatible
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i", str(input_video),
                        "-filter_complex",
                        f"[0:v]loop=loop={num_loops}:size={int(video_duration * fps)}:start=0[v]",
                        "-map", "[v]",
                        "-t", str(target_duration),
                        "-c:v", "libx264",
                        "-preset", "fast",  # Faster encoding for long videos
                        "-crf", "23",
                        "-pix_fmt", "yuv420p",  # Ensure compatibility
                        "-movflags", "+faststart",
                        str(output_path)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=7200  # 2 hour timeout for long videos
                )

                # If loop filter fails, try concat approach
                if result.returncode != 0:
                    logger.warning(f"Loop filter failed, trying concat approach: {result.stderr}")
                    result = self._loop_with_concat(input_video, target_duration, output_path, num_loops)
            else:
                # Use stream_loop for MP4/other formats (faster)
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-stream_loop", str(num_loops - 1),
                        "-i", str(input_video),
                        "-t", str(target_duration),
                        "-c:v", "libx264",
                        "-preset", "fast",
                        "-crf", "23",
                        "-pix_fmt", "yuv420p",
                        "-an",
                        "-movflags", "+faststart",
                        str(output_path)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=7200
                )

            if result.returncode != 0:
                logger.error(f"FFmpeg loop error: {result.stderr}")
                raise VideoProcessingError(f"Video looping failed: {result.stderr[-500:]}")

            logger.info(f"Created looped video at {output_path}")
            return output_path

        except subprocess.TimeoutExpired:
            raise VideoProcessingError("Video looping timeout (exceeded 2 hours)")

    def _loop_with_concat(
        self,
        input_video: Path,
        target_duration: float,
        output_path: Path,
        num_loops: int
    ) -> subprocess.CompletedProcess:
        """
        Loop video using concat demuxer (fallback method).
        """
        # Create a concat file
        concat_file = self.work_dir / "concat_list.txt"
        with open(concat_file, "w") as f:
            for _ in range(num_loops):
                f.write(f"file '{input_video.absolute()}'\n")

        logger.info(f"Using concat method with {num_loops} repetitions...")

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-t", str(target_duration),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-an",
                "-movflags", "+faststart",
                str(output_path)
            ],
            capture_output=True,
            text=True,
            timeout=7200
        )

        # Cleanup concat file
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
        Combine video and audio into final output.

        Args:
            video_path: Path to the video file
            audio_path: Path to the audio file
            output_path: Path for the output file
            video_codec: Video codec to use (copy or libx264)

        Returns:
            Path to the combined file
        """
        try:
            # Build FFmpeg command
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(video_path),
                "-i", str(audio_path),
                "-c:v", video_codec,
                "-c:a", "aac",
                "-b:a", "320k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200  # 2 hour timeout
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg combine error: {result.stderr}")
                raise VideoProcessingError(f"Video/audio combining failed: {result.stderr}")

            logger.info(f"Created combined video at {output_path}")
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
        Complete video processing: loop video and combine with audio.

        Args:
            input_video: Original video file
            audio_path: Audio file to combine
            audio_duration: Duration of the audio in seconds
            output_path: Final output path

        Returns:
            Path to the final video
        """
        # Create looped video
        looped_video = self.work_dir / "looped_video.mp4"
        self.loop_video_to_duration(input_video, audio_duration, looped_video)

        # Combine with audio
        self.combine_video_audio(looped_video, audio_path, output_path)

        # Cleanup looped video
        if looped_video.exists():
            looped_video.unlink()

        return output_path

    def extract_thumbnail(
        self,
        video_path: Path,
        output_path: Path,
        timestamp: str = "00:00:05"
    ) -> Path:
        """
        Extract a thumbnail from the video.

        Args:
            video_path: Path to the video
            output_path: Path for the thumbnail
            timestamp: Timestamp to extract from

        Returns:
            Path to the thumbnail
        """
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
