# AI Music Video Generator

Generate 2-hour music videos with AI-generated music using Kie AI (Suno).

## Features

- Generate AI music with custom prompts using Kie AI
- Automatically generate music tracks until reaching 2 hours of content
- Loop any video to match the audio duration
- Combine video and audio into a final MP4 file
- Download separate audio, video, and tracklist files
- Automatic cleanup of old files after 24 hours
- Modern web interface with real-time progress tracking
- API key configured in frontend (no server-side configuration needed)

## Quick Start (One-Click Installation)

### For Ubuntu 22.04 VPS

```bash
# Clone the repository
git clone https://github.com/yourusername/Music-Api-Youtube.git
cd Music-Api-Youtube

# Make install script executable
chmod +x install.sh

# Run installation
./install.sh
```

The installer will:
1. Detect your environment
2. Install Docker or native dependencies
3. Set up and start all services
4. Provide access URL

## Manual Installation

### Option 1: Docker (Recommended)

```bash
# Install Docker (if not installed)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Clone and start
git clone https://github.com/yourusername/Music-Api-Youtube.git
cd Music-Api-Youtube
docker compose up -d --build
```

Access at: `http://localhost:8000`

### Option 2: Native Installation

```bash
# Install system dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg redis-server

# Start Redis
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Clone repository
git clone https://github.com/yourusername/Music-Api-Youtube.git
cd Music-Api-Youtube

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Create directories
mkdir -p app/uploads app/output

# Start services (in separate terminals)

# Terminal 1 - Web server
uvicorn app.api.main:app --host 0.0.0.0 --port 8000

# Terminal 2 - Celery worker
celery -A app.workers.celery_app worker --loglevel=info

# Terminal 3 - Celery beat (for cleanup tasks)
celery -A app.workers.celery_app beat --loglevel=info
```

## Configuration

### API Key

The Kie AI API key is configured directly in the web interface. No environment variables needed!

Get your API key at: https://kie.ai/api-key

### Environment Variables (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `HOST` | `0.0.0.0` | Server host |
| `PORT` | `8000` | Server port |

## Usage

1. Open `http://localhost:8000` in your browser
2. Enter your Kie AI API key
3. Write a music prompt describing the style you want
4. Upload a video file (MP4, MOV, AVI, MKV, WebM - max 500MB)
5. Click "Generate Music Video"
6. Wait for processing (can take 1-3 hours depending on content)
7. Download your video, audio, and tracklist

## API Endpoints

### POST /generate
Start a new video generation job.

**Form Data:**
- `video` (file): Video file to loop
- `prompt` (string): Music generation prompt
- `api_key` (string): Kie AI API key
- `custom_mode` (boolean, optional): Enable custom mode
- `instrumental` (boolean, optional): Generate instrumental music
- `model` (string, optional): AI model (V4, V4_5, V5)
- `style` (string, optional): Music style
- `title` (string, optional): Track title prefix

**Response:**
```json
{
  "job_id": "uuid",
  "message": "Generation started...",
  "status": "pending"
}
```

### GET /status/{job_id}
Check job status.

**Response:**
```json
{
  "job_id": "uuid",
  "status": "generating_audio",
  "progress": {
    "current_track": 5,
    "total_duration_seconds": 1200,
    "total_duration_formatted": "00:20:00",
    "stage": "generating_audio",
    "message": "Generating track 5..."
  }
}
```

### GET /download/{job_id}/video
Download the final video file.

### GET /download/{job_id}/audio
Download the audio file (MP3).

### GET /download/{job_id}/tracklist
Download the tracklist (JSON).

### DELETE /job/{job_id}
Delete a job and its files.

### GET /health
Health check endpoint.

## Tracklist Format

```json
{
  "total_duration": "02:00:00",
  "total_duration_seconds": 7200,
  "total_tracks": 35,
  "tracks": [
    {
      "id": 1,
      "start": "00:00:00",
      "end": "00:03:24",
      "duration": "00:03:24",
      "duration_seconds": 204,
      "title": "Track 1",
      "kie_track_id": "abc123"
    }
  ]
}
```

## Project Structure

```
/app
├── api/
│   ├── __init__.py
│   ├── main.py          # FastAPI endpoints
│   └── models.py        # Pydantic models
├── services/
│   ├── __init__.py
│   ├── kie_ai.py        # Kie AI integration
│   ├── audio.py         # Audio processing
│   └── video.py         # Video processing
├── workers/
│   ├── __init__.py
│   ├── celery_app.py    # Celery configuration
│   └── tasks.py         # Background tasks
├── static/
│   └── index.html       # Web interface
├── uploads/             # Uploaded videos
├── output/              # Generated files
└── config.py            # Configuration
```

## Technical Details

- **Audio Generation**: Uses Kie AI API (Suno) to generate 2-4 minute tracks
- **Target Duration**: Generates music until reaching 110 minutes (1:50)
- **Maximum Duration**: 120 minutes (2:00)
- **Video Loop**: Uses FFmpeg stream_loop for efficient looping
- **Audio Concat**: Uses FFmpeg concat demuxer
- **Final Output**: H.264 MP4 with AAC audio at 320kbps

## Troubleshooting

### Job stuck at "Generation in progress"
- Check worker logs: `docker compose logs -f celery-worker`
- Ensure Redis is running: `redis-cli ping`
- Verify API key is valid at https://kie.ai

### Video upload fails
- Check file size (max 500MB)
- Verify format is supported (MP4, MOV, AVI, MKV, WebM)
- Check available disk space

### FFmpeg errors
- Ensure FFmpeg is installed: `ffmpeg -version`
- Check video file isn't corrupted

## Docker Commands

```bash
# View all logs
docker compose logs -f

# View specific service logs
docker compose logs -f web
docker compose logs -f celery-worker

# Restart services
docker compose restart

# Stop all services
docker compose down

# Rebuild and restart
docker compose up -d --build

# Check container status
docker compose ps
```

## Native Service Commands

```bash
# View logs
sudo journalctl -u music-video-web -f
sudo journalctl -u music-video-worker -f

# Restart services
sudo systemctl restart music-video-web music-video-worker music-video-beat

# Stop services
sudo systemctl stop music-video-web music-video-worker music-video-beat

# Check status
sudo systemctl status music-video-web
```

## License

MIT License

## Credits

- [Kie AI](https://kie.ai) for music generation API
- [FastAPI](https://fastapi.tiangolo.com/) for the web framework
- [Celery](https://docs.celeryq.dev/) for task processing
- [FFmpeg](https://ffmpeg.org/) for audio/video processing
