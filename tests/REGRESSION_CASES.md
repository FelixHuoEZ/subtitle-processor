# 回归用例

这个文件是 [`fixtures/regression_cases.json`](./fixtures/regression_cases.json) 的人读版补充说明。

- JSON 文件是机器可读版本，给自动化断言使用。
- 这个 Markdown 文件是人读版，用来说明用例意图、预期结果和边界语义。

## 用例列表

### `youtube_7R9H_EX6cnI`

- 输入 URL：`https://www.youtube.com/watch?v=7R9H-EX6cnI`
- 本地媒体提示：`test/再次改良英语：这能行吗？ [7R9H-EX6cnI].webm`

预期结果：

- `content_locale = zh`
- `readwise_mode = url_only`
- `readwise_reason = zh_locale_foreign_spoken`

为什么要保留这个用例：

- 这个视频整体是中文语境包装，面向中文用户。
- 口语内容不能简单归成纯 `zh`，它更接近“中文引入/收尾 + 中间长段英文主体”。
- 自动翻译出来的中文字幕轨不能被当成“原始中文字幕轨”。
- 这种内容不应该默认把英文主体正文直接作为完整文章发给 Readwise。

语义说明：

- 这个用例用于保护一个产品判断：`spoken_language` 和 `content_locale` 不是同一个概念。
- 这个用例也用于保护一条规则：“有中文字幕”只认原始中文轨，不认 `tlang=zh-*` 这类自动翻译轨。

## 后续新增用例建议格式

```md
### `case_id`

- 输入 URL：
- 本地媒体提示：

预期结果：

- `content_locale = ...`
- `readwise_mode = ...`
- `readwise_reason = ...`

为什么要保留这个用例：

- ...
- ...
```
