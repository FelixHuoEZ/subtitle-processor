# Changelog

All notable changes to this project will be documented in this file.

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