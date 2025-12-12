#!/bin/bash

# AI Music Video Generator - One-Click Installation Script
# For Ubuntu 22.04 VPS

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored message
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Header
echo ""
echo "======================================"
echo "  AI Music Video Generator Installer"
echo "  For Ubuntu 22.04 VPS"
echo "======================================"
echo ""

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then
    print_warning "Running without root privileges. Some operations may require sudo."
fi

# Detect installation method
INSTALL_METHOD=""
if command -v docker &> /dev/null && command -v docker-compose &> /dev/null; then
    INSTALL_METHOD="docker"
    print_status "Docker detected. Will use Docker installation method."
elif command -v docker &> /dev/null; then
    print_status "Docker detected but docker-compose not found. Installing docker-compose..."
    INSTALL_METHOD="docker"
else
    print_status "Docker not found. Will use native installation method."
    INSTALL_METHOD="native"
fi

echo ""
echo "Select installation method:"
echo "1) Docker (recommended) - Easier setup and management"
echo "2) Native - Direct installation on system"
echo ""
read -p "Enter choice [1-2] (default: 1): " choice
choice=${choice:-1}

case $choice in
    1) INSTALL_METHOD="docker" ;;
    2) INSTALL_METHOD="native" ;;
    *) INSTALL_METHOD="docker" ;;
esac

echo ""
print_status "Using $INSTALL_METHOD installation method..."
echo ""

# ===========================================
# Docker Installation
# ===========================================
if [ "$INSTALL_METHOD" == "docker" ]; then

    # Install Docker if not present
    if ! command -v docker &> /dev/null; then
        print_status "Installing Docker..."

        # Remove old versions
        sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

        # Install prerequisites
        sudo apt-get update
        sudo apt-get install -y \
            ca-certificates \
            curl \
            gnupg \
            lsb-release

        # Add Docker's official GPG key
        sudo mkdir -p /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

        # Set up repository
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
            $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

        # Install Docker Engine
        sudo apt-get update
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

        # Add current user to docker group
        sudo usermod -aG docker $USER

        print_success "Docker installed successfully!"
    fi

    # Install Docker Compose if not present
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        print_status "Installing Docker Compose..."
        sudo apt-get install -y docker-compose-plugin

        # Also install standalone docker-compose for compatibility
        sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
        sudo chmod +x /usr/local/bin/docker-compose

        print_success "Docker Compose installed successfully!"
    fi

    # Create directories
    print_status "Creating directories..."
    mkdir -p app/uploads app/output

    # Build and start containers
    print_status "Building and starting Docker containers..."

    # Use docker compose (V2) or docker-compose (V1)
    if docker compose version &> /dev/null; then
        docker compose up -d --build
    else
        docker-compose up -d --build
    fi

    print_success "Docker containers started!"

    # Show status
    echo ""
    print_status "Container status:"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

    echo ""
    print_success "Installation complete!"
    echo ""
    echo "======================================"
    echo "  Access your application at:"
    echo "  http://localhost:8000"
    echo ""
    echo "  Or if on a VPS:"
    echo "  http://YOUR_SERVER_IP:8000"
    echo "======================================"
    echo ""
    echo "Useful commands:"
    echo "  - View logs:    docker compose logs -f"
    echo "  - Stop:         docker compose down"
    echo "  - Restart:      docker compose restart"
    echo "  - Rebuild:      docker compose up -d --build"
    echo ""

# ===========================================
# Native Installation
# ===========================================
else

    print_status "Starting native installation..."

    # Update system
    print_status "Updating system packages..."
    sudo apt-get update
    sudo apt-get upgrade -y

    # Install system dependencies
    print_status "Installing system dependencies..."
    sudo apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg \
        redis-server \
        curl \
        git

    # Enable and start Redis
    print_status "Configuring Redis..."
    sudo systemctl enable redis-server
    sudo systemctl start redis-server

    # Create virtual environment
    print_status "Creating Python virtual environment..."
    python3 -m venv venv
    source venv/bin/activate

    # Install Python dependencies
    print_status "Installing Python dependencies..."
    pip install --upgrade pip
    pip install -r requirements.txt

    # Create directories
    print_status "Creating directories..."
    mkdir -p app/uploads app/output

    # Create systemd service files
    print_status "Creating systemd service files..."

    # Web service
    sudo tee /etc/systemd/system/music-video-web.service > /dev/null << EOF
[Unit]
Description=AI Music Video Generator - Web
After=network.target redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment="PATH=$(pwd)/venv/bin"
ExecStart=$(pwd)/venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # Celery worker service
    sudo tee /etc/systemd/system/music-video-worker.service > /dev/null << EOF
[Unit]
Description=AI Music Video Generator - Celery Worker
After=network.target redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment="PATH=$(pwd)/venv/bin"
ExecStart=$(pwd)/venv/bin/celery -A app.workers.celery_app worker --loglevel=info --concurrency=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # Celery beat service
    sudo tee /etc/systemd/system/music-video-beat.service > /dev/null << EOF
[Unit]
Description=AI Music Video Generator - Celery Beat
After=network.target redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment="PATH=$(pwd)/venv/bin"
ExecStart=$(pwd)/venv/bin/celery -A app.workers.celery_app beat --loglevel=info
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd and enable services
    print_status "Enabling services..."
    sudo systemctl daemon-reload
    sudo systemctl enable music-video-web music-video-worker music-video-beat
    sudo systemctl start music-video-web music-video-worker music-video-beat

    # Wait for services to start
    sleep 3

    # Check status
    print_status "Checking service status..."
    sudo systemctl status music-video-web --no-pager || true
    sudo systemctl status music-video-worker --no-pager || true
    sudo systemctl status music-video-beat --no-pager || true

    echo ""
    print_success "Installation complete!"
    echo ""
    echo "======================================"
    echo "  Access your application at:"
    echo "  http://localhost:8000"
    echo ""
    echo "  Or if on a VPS:"
    echo "  http://YOUR_SERVER_IP:8000"
    echo "======================================"
    echo ""
    echo "Useful commands:"
    echo "  - View web logs:     sudo journalctl -u music-video-web -f"
    echo "  - View worker logs:  sudo journalctl -u music-video-worker -f"
    echo "  - Restart services:  sudo systemctl restart music-video-web music-video-worker music-video-beat"
    echo "  - Stop services:     sudo systemctl stop music-video-web music-video-worker music-video-beat"
    echo ""

fi

# Final notes
echo ""
echo "======================================"
echo "  IMPORTANT NOTES"
echo "======================================"
echo ""
echo "1. Get your Kie AI API key at: https://kie.ai/api-key"
echo ""
echo "2. The API key is configured in the web interface,"
echo "   no environment variables needed!"
echo ""
echo "3. Maximum video upload size: 500MB"
echo ""
echo "4. Generated videos can be up to 2 hours long"
echo ""
echo "5. Files are automatically cleaned up after 24 hours"
echo ""
print_success "Enjoy your AI Music Video Generator!"
echo ""
