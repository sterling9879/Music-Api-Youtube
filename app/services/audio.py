"""
Audio processing service for concatenating and managing audio files.
"""

import subprocess
import logging
import json
from pathlib import Path
from typing import List, Tuple, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class TrackMetadata:
    """Metadata for a single track."""
    id: int
    start: str
    end: str
    duration: str
    duration_seconds: float
    title: Optional[str] = None
    kie_track_id: Optional[str] = None


@dataclass
class AudioResult:
    """Result of audio processing."""
    output_path: Path
    total_duration_seconds: float
    total_duration_formatted: str
    tracks: List[TrackMetadata]


def format_duration(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def get_audio_duration(file_path: Path) -> float:
    """
    Get duration of an audio file in seconds using ffprobe.

    Args:
        file_path: Path to the audio file

    Returns:
        Duration in seconds
    """
    try:
        result = subprocess.run(
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

        if result.returncode == 0:
            return float(result.stdout.strip())
        else:
            logger.error(f"ffprobe error: {result.stderr}")
            raise AudioProcessingError(f"Failed to get duration: {result.stderr}")

    except subprocess.TimeoutExpired:
        raise AudioProcessingError("ffprobe timeout")
    except ValueError as e:
        raise AudioProcessingError(f"Invalid duration value: {e}")


class AudioProcessingError(Exception):
    """Custom exception for audio processing errors."""
    pass


class AudioProcessor:
    """Service for processing and concatenating audio files."""

    def __init__(self, work_dir: Path):
        """
        Initialize the audio processor.

        Args:
            work_dir: Working directory for temporary files
        """
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def concatenate_audio_files(
        self,
        audio_files: List[Tuple[Path, Optional[str], Optional[str]]],
        output_path: Path
    ) -> AudioResult:
        """
        Concatenate multiple audio files into one using FFmpeg.

        Args:
            audio_files: List of tuples (file_path, title, kie_track_id)
            output_path: Path for the output file

        Returns:
            AudioResult with concatenated file info and tracklist
        """
        if not audio_files:
            raise AudioProcessingError("No audio files to concatenate")

        if len(audio_files) == 1:
            # Single file - just copy and return
            file_path, title, kie_id = audio_files[0]
            duration = get_audio_duration(file_path)
            subprocess.run(
                ["cp", str(file_path), str(output_path)],
                check=True
            )
            track = TrackMetadata(
                id=1,
                start="00:00:00",
                end=format_duration(duration),
                duration=format_duration(duration),
                duration_seconds=duration,
                title=title,
                kie_track_id=kie_id
            )
            return AudioResult(
                output_path=output_path,
                total_duration_seconds=duration,
                total_duration_formatted=format_duration(duration),
                tracks=[track]
            )

        # Create concat file for FFmpeg
        concat_file = self.work_dir / "concat_list.txt"
        tracks: List[TrackMetadata] = []
        current_position = 0.0

        with open(concat_file, "w") as f:
            for idx, (file_path, title, kie_id) in enumerate(audio_files, 1):
                # Escape single quotes in file path
                escaped_path = str(file_path).replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")

                # Get duration for tracklist
                duration = get_audio_duration(file_path)
                start_time = current_position
                end_time = current_position + duration

                track = TrackMetadata(
                    id=idx,
                    start=format_duration(start_time),
                    end=format_duration(end_time),
                    duration=format_duration(duration),
                    duration_seconds=duration,
                    title=title,
                    kie_track_id=kie_id
                )
                tracks.append(track)
                current_position = end_time

        # Run FFmpeg concat
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",  # Overwrite output
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-c", "copy",  # Copy without re-encoding
                    str(output_path)
                ],
                capture_output=True,
                text=True,
                timeout=600  # 10 minutes timeout
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg concat error: {result.stderr}")
                raise AudioProcessingError(f"FFmpeg concat failed: {result.stderr}")

            # Verify output
            total_duration = get_audio_duration(output_path)

            return AudioResult(
                output_path=output_path,
                total_duration_seconds=total_duration,
                total_duration_formatted=format_duration(total_duration),
                tracks=tracks
            )

        except subprocess.TimeoutExpired:
            raise AudioProcessingError("FFmpeg concat timeout")
        finally:
            # Cleanup concat file
            if concat_file.exists():
                concat_file.unlink()

    def generate_tracklist_json(
        self,
        audio_result: AudioResult,
        output_path: Path
    ) -> Path:
        """
        Generate a JSON tracklist file.

        Args:
            audio_result: Result from audio concatenation
            output_path: Path for the JSON file

        Returns:
            Path to the generated JSON file
        """
        tracklist = {
            "total_duration": audio_result.total_duration_formatted,
            "total_duration_seconds": audio_result.total_duration_seconds,
            "total_tracks": len(audio_result.tracks),
            "tracks": [asdict(track) for track in audio_result.tracks]
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(tracklist, f, indent=2, ensure_ascii=False)

        logger.info(f"Generated tracklist at {output_path}")
        return output_path

    def normalize_audio(self, input_path: Path, output_path: Path) -> Path:
        """
        Normalize audio levels using FFmpeg.

        Args:
            input_path: Input audio file
            output_path: Output normalized file

        Returns:
            Path to normalized file
        """
        try:
            # First pass - analyze
            analyze_result = subprocess.run(
                [
                    "ffmpeg",
                    "-i", str(input_path),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                    "-f", "null",
                    "-"
                ],
                capture_output=True,
                text=True,
                timeout=300
            )

            # Second pass - apply normalization
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i", str(input_path),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "44100",
                    "-ac", "2",
                    str(output_path)
                ],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                logger.warning(f"Normalization failed, copying original: {result.stderr}")
                subprocess.run(["cp", str(input_path), str(output_path)], check=True)

            return output_path

        except subprocess.TimeoutExpired:
            logger.warning("Normalization timeout, copying original")
            subprocess.run(["cp", str(input_path), str(output_path)], check=True)
            return output_path

    def convert_to_mp3(
        self,
        input_path: Path,
        output_path: Path,
        bitrate: str = "320k"
    ) -> Path:
        """
        Convert audio file to MP3 format.

        Args:
            input_path: Input audio file
            output_path: Output MP3 file
            bitrate: Output bitrate

        Returns:
            Path to converted file
        """
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i", str(input_path),
                    "-codec:a", "libmp3lame",
                    "-b:a", bitrate,
                    str(output_path)
                ],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                raise AudioProcessingError(f"MP3 conversion failed: {result.stderr}")

            return output_path

        except subprocess.TimeoutExpired:
            raise AudioProcessingError("MP3 conversion timeout")


def create_audio_processor(work_dir: Path) -> AudioProcessor:
    """Factory function to create an AudioProcessor instance."""
    return AudioProcessor(work_dir)
