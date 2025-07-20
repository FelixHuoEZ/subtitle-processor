# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
A comprehensive subtitle processing service that downloads, transcribes, and manages video subtitles from various platforms. The system includes a web interface, Telegram bot, and automatic transcription capabilities using FunASR.

## Architecture
The project follows a microservices architecture with Docker containers and a modular Python application structure:

### Core Services
- **subtitle-processor** (`app/main.py`): Refactored Flask web service with modular architecture
- **transcribe-audio** (`transcribe-audio/app.py`): FunASR-based transcription service for audio files  
- **telegram-bot** (`telegram-bot/app.py`): Telegram bot interface for user interaction
- **deeplx**: Translation service for subtitle translation

### Refactored Application Structure
The main Flask application has been refactored into a modular architecture:

#### Services Layer (`app/services/`)
- **SubtitleService**: Handles subtitle parsing, SRT processing, and content cleaning
- **VideoService**: Manages video downloads, platform integrations (YouTube, Bilibili, AcFun)
- **TranscriptionService**: Interfaces with FunASR servers for audio transcription
- **TranslationService**: Handles text translation via DeepL, OpenAI APIs
- **ReadwiseService**: Manages Readwise Reader integration for content archiving

#### Configuration (`app/config/`)
- **ConfigManager**: Centralized configuration management with YAML support
- **Environment-aware**: Supports both container and local development configurations

#### Utilities (`app/utils/`)
- **logging_utils**: Colored logging formatter and setup utilities
- **file_utils**: File encoding detection and filename sanitization
- **time_utils**: Time formatting and parsing for subtitle timestamps

#### Routes (`app/routes/`)
- **upload_routes**: File upload handling endpoints
- **view_routes**: Content viewing and listing endpoints  
- **process_routes**: Video processing and status endpoints

### Key Components
- **Video Processing**: Uses yt-dlp for downloading videos from YouTube, Bilibili, etc.
- **Audio Transcription**: FunASR models for Chinese audio transcription with VAD and punctuation
- **Subtitle Management**: Processing, formatting, and storage of subtitle files
- **Readwise Integration**: Automatic article creation from processed subtitles
- **Firefox Cookie Support**: Enables downloading restricted YouTube videos using Firefox cookies

## Configuration
- Main config: `config/config.yml` (use `config/config-example.yml` as template)
- Environment variables set in `docker-compose.yml`
- Firefox profile in `firefox_profile/` for cookie-based authentication

## Development Commands

### Build and Run
```bash
# Build all Docker images
powershell .\build_images.ps1

# Start all services
docker-compose up --build

# Start specific service
docker-compose up subtitle-processor
```

### Testing
```bash
# Run integration tests
python tests/test_integration.py

# Test subtitle processing
python test_subtitles.py

# Test video download
python test_download.py
```

### Development
```bash
# Install Python dependencies
pip install -r requirements.txt

# Run refactored app locally (requires transcription service)
cd app && python main.py

# Or run original monolithic app
cd app && python app_original.py
```

## File Structure
- `app/`: Refactored Flask application with modular architecture
  - `main.py`: Application factory and entry point
  - `app_original.py`: Original monolithic version (backup)
  - `config/`: Configuration management module
  - `services/`: Business logic service modules
  - `utils/`: Utility functions and helpers
  - `routes/`: Flask blueprint route modules
  - `templates/`: HTML templates for web interface
- `transcribe-audio/`: FunASR transcription microservice
- `telegram-bot/`: Telegram bot implementation
- `config/`: Configuration files and examples
- `uploads/`: Processed files and metadata
- `outputs/`: Generated subtitle files
- `models/`: AI model storage for transcription
- `firefox_profile/`: Firefox cookies for restricted content access

## Key Technologies
- **Backend**: Flask (Python)
- **Transcription**: FunASR with Paraformer, VAD, and punctuation models
- **Video Processing**: yt-dlp
- **Translation**: DeepLX, OpenAI API integration
- **Containerization**: Docker with GPU support
- **Database**: JSON-based file system

## Notes
- GPU support required for transcription service (NVIDIA)
- Firefox profile setup needed for restricted YouTube content
- Configuration tokens required for Telegram, Readwise, and translation services

## Refactoring Notes
- The original monolithic `app.py` (2895 lines) has been refactored into a modular architecture
- Original file preserved as `app_original.py` for reference
- New architecture follows software engineering best practices:
  - Single Responsibility Principle
  - Separation of Concerns
  - Dependency Injection
  - Configuration Management
  - Service Layer Pattern
- The refactored version provides a foundation for easier testing, maintenance, and feature development
- Core functionality structure is preserved but organized into logical modules