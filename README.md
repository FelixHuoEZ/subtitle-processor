# Subtitle Processing Service 字幕处理服务

[English](#english) | [中文](#chinese)

<a name="english"></a>
## 🌍 English

### Overview
A comprehensive subtitle processing service that automatically downloads, transcribes, and manages video subtitles from various platforms. Features a Telegram bot interface and a web management portal.

### 🚀 Features
- **Multi-Platform Support**
  - YouTube video subtitle extraction
  - Bilibili video subtitle processing
  - Automatic fallback to audio transcription
  
- **Subtitle Processing**
  - Direct subtitle download from platforms
  - Audio transcription using FunASR
  - Support for multiple subtitle formats (SRT, VTT, JSON3)
  
- **User Interfaces**
  - Telegram Bot for easy access
  - Web interface for subtitle management
  - Real-time subtitle viewing and searching
  
- **File Management**
  - Automatic file organization
  - Metadata tracking
  - Timeline visualization

- **Readwise Integration**
  - Automatic article creation from subtitles
  - Rich text formatting support
  - Seamless sync with Readwise Reader
  - Smart content segmentation for long videos

### 🛠️ Technical Stack
- Backend: Python Flask
- Frontend: HTML/CSS/JavaScript
- Transcription: FunASR
- Container: Docker
- Storage: JSON-based file system

### 📦 Installation
1. Clone the repository
2. Install Docker and Docker Compose
3. Configure environment variables:
   ```bash
   TELEGRAM_TOKEN=your_telegram_bot_token
   READWISE_TOKEN=your_readwise_token
   ```
4. Start the services:
   ```bash
   docker-compose up --build
   ```

### 🔧 Usage
1. **Telegram Bot**
   - Send video URL to the bot
   - Receive processed subtitle file
   
2. **Web Interface**
   - Access `http://localhost:5000`
   - Upload video files or URLs
   - View and search subtitles

3. **Readwise Integration**
   - Automatically creates articles in Readwise Reader
   - Preserves video metadata (title, URL, publish date)
   - Intelligently splits long content into readable segments
   - Access transcripts alongside your other reading materials

### 📝 License
MIT License

---

<a name="chinese"></a>
## 🌏 中文

### 概述
一个综合性的字幕处理服务，可以自动下载、转录和管理来自各种平台的视频字幕。提供 Telegram 机器人接口和网页管理门户。

### 🚀 功能特点
- **多平台支持**
  - YouTube 视频字幕提取
  - Bilibili 视频字幕处理
  - 自动音频转录备选方案
  
- **字幕处理**
  - 直接从平台下载字幕
  - 使用 FunASR 进行音频转录
  - 支持多种字幕格式（SRT、VTT、JSON3）
  
- **用户界面**
  - Telegram 机器人便捷访问
  - 网页字幕管理界面
  - 实时字幕查看和搜索
  
- **文件管理**
  - 自动文件组织
  - 元数据跟踪
  - 时间轴可视化

- **Readwise 集成**
  - 自动从字幕创建文章
  - 支持富文本格式
  - 与 Readwise Reader 无缝同步
  - 智能分段处理长视频内容

### 🛠️ 技术栈
- 后端：Python Flask
- 前端：HTML/CSS/JavaScript
- 转录：FunASR
- 容器：Docker
- 存储：基于 JSON 的文件系统

### 📦 安装步骤
1. 克隆仓库
2. 安装 Docker 和 Docker Compose
3. 配置环境变量：
   ```bash
   TELEGRAM_TOKEN=你的_telegram_机器人_token
   READWISE_TOKEN=你的_readwise_token
   ```
4. 启动服务：
   ```bash
   docker-compose up --build
   ```

### 🔧 使用方法
1. **Telegram 机器人**
   - 向机器人发送视频 URL
   - 接收处理好的字幕文件
   
2. **网页界面**
   - 访问 `http://localhost:5000`
   - 上传视频文件或 URL
   - 查看和搜索字幕

3. **Readwise 集成**
   - 自动在 Readwise Reader 中创建文章
   - 保留视频元数据（标题、URL、发布日期）
   - 智能分割长内容为易读片段
   - 在其他阅读材料旁边访问转录文本

### 📝 许可证
MIT 许可证
