# Changelog

All notable changes to this project will be documented in this file.

## [2025-07-20] - PowerShell Script Optimization & Project Cleanup

### Added
- **Chrome Extension**: New browser extension for one-click video URL processing
  - `chrome-extension/background.js`: Core extension functionality
  - `chrome-extension/manifest-v2.json`: Extension configuration
- **Enhanced PowerShell Script**: New `quicker_transcribe_url.ps1` with improved functionality
- **Project Documentation**: Comprehensive CHANGELOG.md for tracking changes

### Fixed
- **PowerShell Script Flash-Exit Issue**: Resolved script execution problems
  - Fixed encoding issues causing syntax errors
  - Implemented proper timeout handling for video processing
  - Added user-friendly timeout messages (timeout indicates successful submission)
  - Enhanced error handling and debugging capabilities

### Removed
- **Code Cleanup**: Removed 20+ redundant files including:
  - Duplicate PowerShell scripts and test files
  - Temporary documentation files
  - Legacy backup files and development artifacts
- **File Deletions**:
  - `app/app.py`: Removed duplicate monolithic version
  - `quicker_action_final2.ps1`: Replaced with optimized version
  - `config/transcribe_servers.json.excample`: Fixed typo in filename

### Changed
- **Docker Configuration**: Updated `Dockerfile` and `supervisord.conf`
- **Project Structure**: Streamlined for production readiness
- **Git Ignore**: Added `.claude/` directory and log files to ignore list

### Technical Details
- **Commits**: `ea7f89b`, `4cd13fd`
- **Files Changed**: 9 files modified, 419 insertions, 3196 deletions
- **Impact**: Significantly cleaner project structure, improved script reliability

---

## [2025-07-20] - Telegram Bot Stability Fix

### Fixed
- **Telegram Bot Connection Issues**: Resolved long-running container unresponsiveness
  - Added comprehensive timeout configurations (connect, read, write, pool timeouts)
  - Implemented automatic reconnection mechanism with error-specific retry intervals
  - Improved error handling to avoid additional network requests during failures
  - Added Docker resource limits (512M memory limit, 256M reservation)
  - Added logging configuration with rotation (10MB max, 3 files)

### Technical Details
- **Root Cause**: `httpcore.RemoteProtocolError: Server disconnected without sending a response`
- **Files Modified**:
  - `telegram-bot/app.py`: Connection stability improvements
  - `docker-compose.yml`: Resource limits and logging configuration
- **Commit**: `bcc19be` - fix: resolve telegram-bot connection stability issues

### Impact
- Telegram bot now handles network disconnections gracefully
- Automatic recovery from connection failures
- Better resource management prevents memory leaks
- Improved logging for troubleshooting

---

## Previous Changes

### [2025-01-XX] - Modular Architecture Refactoring
- Refactored monolithic `app.py` (2895 lines) into modular architecture
- Introduced service layer pattern with separate modules
- Enhanced configuration management with YAML support
- Improved error handling and logging utilities

### [2024-XX-XX] - Initial Project Setup
- Created subtitle processing microservices
- Implemented FunASR transcription service
- Added Telegram bot interface
- Integrated with Readwise and translation services