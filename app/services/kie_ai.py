"""
Kie AI API integration service for music generation.
"""

import httpx
import asyncio
import logging
from typing import Optional, Tuple, List
from pathlib import Path
import time

from app.config import (
    KIE_AI_GENERATE_ENDPOINT,
    KIE_AI_STATUS_ENDPOINT,
    MUSIC_GENERATION_TIMEOUT,
    MAX_RETRIES,
    POLLING_INTERVAL,
)

logger = logging.getLogger(__name__)


class KieAIError(Exception):
    """Custom exception for Kie AI API errors."""
    def __init__(self, message: str, code: Optional[int] = None):
        self.message = message
        self.code = code
        super().__init__(self.message)


class KieAIService:
    """Service for interacting with Kie AI music generation API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    async def generate_music(
        self,
        prompt: str,
        custom_mode: bool = False,
        instrumental: bool = True,
        model: str = "V4",
        style: Optional[str] = None,
        title: Optional[str] = None,
    ) -> str:
        """
        Start music generation and return task ID.

        Args:
            prompt: Description or lyrics for the music
            custom_mode: Use custom mode for more control
            instrumental: Generate without vocals
            model: AI model to use (V4, V4_5, V5, etc.)
            style: Music style (required for custom mode)
            title: Title for the generated track

        Returns:
            Task ID for polling status
        """
        payload = {
            "prompt": prompt,
            "customMode": custom_mode,
            "instrumental": instrumental,
            "model": model,
        }

        if custom_mode and style:
            payload["style"] = style

        if title:
            payload["title"] = title

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        KIE_AI_GENERATE_ENDPOINT,
                        headers=self.headers,
                        json=payload
                    )

                    data = response.json()

                    if response.status_code == 200 and data.get("code") == 200:
                        task_id = data.get("data", {}).get("taskId")
                        if task_id:
                            logger.info(f"Music generation started with task ID: {task_id}")
                            return task_id
                        raise KieAIError("No task ID returned from API")

                    # Handle specific error codes
                    error_code = data.get("code", response.status_code)
                    error_msg = data.get("msg", "Unknown error")

                    if error_code == 401:
                        raise KieAIError("Invalid API key", code=401)
                    elif error_code == 402:
                        raise KieAIError("Insufficient credits", code=402)
                    elif error_code == 429:
                        # Rate limit - wait and retry
                        wait_time = 2 ** attempt * 5
                        logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise KieAIError(f"API error: {error_msg}", code=error_code)

            except httpx.TimeoutException:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise KieAIError("Request timeout after max retries")
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise KieAIError(f"Request failed: {str(e)}")

        raise KieAIError("Failed after maximum retries")

    async def check_status(self, task_id: str) -> Tuple[str, Optional[List[dict]]]:
        """
        Check the status of a music generation task.

        Args:
            task_id: The task ID to check

        Returns:
            Tuple of (status, tracks_data) where tracks_data is None if not ready
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    KIE_AI_STATUS_ENDPOINT,
                    params={"taskId": task_id},
                    headers=self.headers
                )

                data = response.json()

                if response.status_code != 200 or data.get("code") != 200:
                    error_msg = data.get("msg", "Unknown error")
                    raise KieAIError(f"Status check failed: {error_msg}")

                task_data = data.get("data", {})
                status = task_data.get("status", "UNKNOWN")

                if status == "SUCCESS":
                    response_data = task_data.get("response", {})
                    tracks = response_data.get("sunoData", [])
                    return status, tracks
                elif status == "FAILED":
                    raise KieAIError("Music generation failed", code=501)
                else:
                    return status, None

        except httpx.TimeoutException:
            raise KieAIError("Status check timeout")
        except httpx.RequestError as e:
            raise KieAIError(f"Status check failed: {str(e)}")

    async def wait_for_completion(
        self,
        task_id: str,
        timeout: int = MUSIC_GENERATION_TIMEOUT,
        progress_callback=None
    ) -> List[dict]:
        """
        Wait for music generation to complete.

        Args:
            task_id: The task ID to wait for
            timeout: Maximum time to wait in seconds
            progress_callback: Optional callback for progress updates

        Returns:
            List of generated track data
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            status, tracks = await self.check_status(task_id)

            if progress_callback:
                await progress_callback(status, time.time() - start_time)

            if status == "SUCCESS" and tracks:
                return tracks
            elif status in ("PENDING", "PROCESSING"):
                await asyncio.sleep(POLLING_INTERVAL)
            else:
                raise KieAIError(f"Unexpected status: {status}")

        raise KieAIError(f"Generation timeout after {timeout} seconds")

    async def download_audio(self, audio_url: str, output_path: Path) -> Path:
        """
        Download audio file from URL.

        Args:
            audio_url: URL of the audio file
            output_path: Path to save the file

        Returns:
            Path to the downloaded file
        """
        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.get(audio_url)

                    if response.status_code == 200:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(response.content)
                        logger.info(f"Downloaded audio to {output_path}")
                        return output_path
                    else:
                        raise KieAIError(f"Download failed with status {response.status_code}")

            except httpx.TimeoutException:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise KieAIError("Download timeout after max retries")
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise KieAIError(f"Download failed: {str(e)}")

        raise KieAIError("Download failed after maximum retries")


def create_kie_service(api_key: str) -> KieAIService:
    """Factory function to create a KieAI service instance."""
    return KieAIService(api_key)
