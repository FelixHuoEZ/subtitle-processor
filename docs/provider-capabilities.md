# 字幕翻译与音频语言探测 Provider 边界

本文档定义两个彼此独立的产品需求，并说明当前实现支持哪些 provider。两项能力都不应绑定到 OpenAI；OpenAI 只是现有 adapter 之一，也可以由满足同一输入输出契约的其他接口实现。

## 1. 两个需求不是同一个处理阶段

| 能力 | 输入 | 输出 | 当前触发方式 | 是否属于自动主流程 |
| --- | --- | --- | --- | --- |
| 字幕文本翻译 | 已完成的字幕文本、源语言、目标语言 | 保留字幕结构的目标语言文本 | 手动 API；可选自动阶段 | 可选，默认关闭 |
| 音频语言探测 | 从音频抽取的短采样片段 | `spoken_language`、置信度和 provider 元数据 | 主语言证据不足时由处理流程调用 | 是 |

两个需求都可能使用 OpenAI，但调用的能力不同：

- 字幕翻译使用文本生成接口，当前 OpenAI adapter 调用 Chat Completions。
- 音频语言探测先将采样音频转成文本，当前 OpenAI adapter 使用固定模型 `whisper-1` 调用 Whisper 兼容的 Audio Transcriptions，再用本地规则判断语言。

因此，不能把一组“OpenAI 翻译配置”直接当作音频语言探测配置，也不能因为音频探测使用 FunASR 就认为字幕翻译必须使用 FunASR。OpenAI-compatible 服务也可能只实现文本或音频 API 中的一种，必须按能力分别验证。

## 2. 字幕文本翻译

### 2.1 产品需求

- 将已有字幕从源语言翻译到调用方指定的目标语言。
- 尽量保留 SRT 时间戳、序号和分段结构。
- provider 应可替换；可以使用传统翻译 API，也可以使用 OpenAI 兼容文本模型。
- provider 失败时允许按顺序回退，但不能把部分分块成功误报为整份字幕成功。

### 2.2 当前实现

入口：`TranslationService.translate_subtitle_content()`。

当前 adapter 包括 `DeepL API`、`DeepLX` 和 OpenAI-compatible Chat Completions。实际启用项和回退顺序由 `translation.services` 的 `enabled`、`priority` 与 `config_name` 驱动，不绑定到某一家服务商。

OpenAI-compatible adapter 直接调用 HTTP API，不依赖 `openai` Python SDK。`tokens.openai` 可以保存多个具名文本 provider，`translation.services` 决定使用哪个配置。

当前 Web 后端只提供手动接口：

```http
POST /process/translate/<file_id>
Content-Type: application/json

{"source_lang": "auto", "target_lang": "zh"}
```

`target_lang` 未提供时使用 `translation.default_target_language`，默认是 `zh`。翻译只有在全部分块成功时才返回完整内容；部分成功会返回失败元数据，不会保存混合语言文件。

### 2.3 自动流程边界

自动翻译是独立且默认关闭的阶段：

- `AUTO_TRANSLATE_NON_TARGET_LANGUAGE=true`：启用“非目标语言字幕自动翻译”。
- `AUTO_TRANSLATE_TARGET_LANGUAGE=zh`：配置目标语言，不限定为中文。
- `AUTO_TRANSLATE_MIN_SOURCE_CONFIDENCE=0.75`：口语检测作为字幕语言来源时的最低置信度。
- 已下载字幕优先使用所选字幕轨语言；转录生成字幕使用最终口语语言。
- 源语言等于目标语言时跳过；`mixed`、未知语言和低置信度结果也保守跳过。
- 翻译成功时保留原字幕文件，并将新文件用于 Readwise；任何分块失败都会停止发送，避免混合语言正文。

热词后处理仍是本地规则替换，不属于翻译 provider。

## 3. 音频语言探测

### 3.1 产品需求

- 在字幕和平台元数据不足以可靠判断时，对音频采样并推断主体口语语言。
- provider 应可替换；任何能够把采样音频转换成可判定文本、并返回必要元数据的 ASR 接口都可以实现该能力。
- 多 provider 应按顺序尝试，并对低置信度、单语模型偏置和混合语言结果做保护。

### 3.2 当前实现

入口：`TranscriptionService.detect_audio_language()`。

当前实现的 provider adapter 只有：

1. `configured_funasr`：调用配置的 FunASR 转录服务。
2. `openai`：调用 OpenAI Whisper 兼容的 Audio Transcriptions 接口。

顺序由 `AUDIO_PROBE_PROVIDERS` 或 `audio_probe.providers` 控制，默认仅启用 `configured_funasr`；最低可接受置信度由 `AUDIO_PROBE_MIN_CONFIDENCE` 或 `audio_probe.min_confidence` 控制。只有显式加入 `openai` 且配置独立的 `audio_probe.openai.api_key` 时才会调用 Audio Transcriptions。

OpenAI-compatible 音频 adapter 使用 HTTP multipart 请求，不依赖 `openai` Python SDK，也不会读取文本翻译 provider 的 key、模型或端点。

这里的 OpenAI 不是硬性要求。接入其他 ASR 服务时，应新增独立 adapter，并统一返回语言探测所需的转录文本、provider 名称和模型元数据。

## 4. 当前实现与目标架构的差距

- 两项能力仍没有统一的 provider 协议或注册机制。
- 字幕翻译可配置现有 adapter 的顺序，但新增 provider 仍需增加代码 adapter。
- 音频语言探测只识别 `configured_funasr` 和 `openai` 两个名称。
- 自动翻译开关是部署级配置，尚未提供 Web UI 的单任务覆盖。

后续抽象 provider 时，应分别定义 `SubtitleTranslationProvider` 和 `AudioLanguageProbeProvider`。两者可以复用同一家服务商，但不能共用错误的请求格式或配置契约。

## 5. 代码入口

- 手动翻译 API：`app/routes/process_routes.py::translate_subtitle`
- 字幕翻译服务：`app/services/translation_service.py`
- 音频语言探测：`app/services/transcription_service.py::detect_audio_language`
- 语言决策与下游分支：[`language-decision-logic.md`](language-decision-logic.md)
