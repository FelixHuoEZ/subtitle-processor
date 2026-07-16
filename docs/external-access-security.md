# 公网访问安全边界

公网部署使用三条独立认证路径，避免把浏览器登录凭据、扩展凭据和容器内部凭据混用。

## 访问模型

| 入口 | Cloudflare Access 策略 | 应用端权限 |
| --- | --- | --- |
| `https://readwise.gauss.surf` | 身份登录 Allow | 完整网页与 API 权限 |
| `https://readwise-api.gauss.surf` | Service Auth | 仅任务提交、任务状态和 YouTube Reader 状态查询 |
| Docker 内网 `http://subtitle-processor:5000` | 不经过 Access | `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>` |

`GET /health` 专供容器健康检查，不要求应用端认证。`/health/metrics`、设置接口、任务详情和管理页面都需要认证。

## Cloudflare 配置

1. 创建 Web 类型的 Self-hosted application，域名为 `readwise.gauss.surf`，只允许指定身份登录。
2. 创建 API 类型的 Self-hosted application，域名为 `readwise-api.gauss.surf`，策略动作为 `Service Auth`，只包含专用 Service Token。
3. API 应用为扩展预检请求返回 `GET, POST, OPTIONS` 和 Access Service Token 请求头。实际请求仍必须通过 Service Auth，应用端 API AUD 只允许 `POST /process`、`GET /process/status/<id>` 和 `GET /process/reader-status/youtube/<video_id>`。
4. 让 Cloudflare Tunnel 的两个公开主机名都指向同一个字幕服务源站。
5. 分别记录两个应用的 Application Audience (AUD) Tag。不要把 Service Token Secret 或内部 Token 写入仓库。

应用端配置：

```env
ACCESS_AUTH_ENABLED=true
ACCESS_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
ACCESS_WEB_APPLICATION_AUD=web-application-aud
ACCESS_API_APPLICATION_AUD=api-application-aud
ACCESS_ALLOWED_ORIGINS=https://readwise.gauss.surf
ACCESS_JWT_LEEWAY_SECONDS=60
INTERNAL_SERVICE_TOKEN=long-random-value
```

启用前必须同时配置两个 AUD、Team Domain 和内部 Token，否则服务会拒绝启动，避免误以为源站已经受保护。
`ACCESS_JWT_LEEWAY_SECONDS` 用于容忍 Cloudflare 与源站之间的小幅时钟偏差，默认 60 秒，允许范围为 0-300 秒；签名、issuer 和 AUD 校验不受影响。

## Chrome 扩展

扩展使用独立的 API 地址和网页地址：

- API 地址：`https://readwise-api.gauss.surf`
- 网页地址：`https://readwise.gauss.surf`

Access Client ID 和 Secret 默认保存在 `chrome.storage.local`，不会通过 Chrome Sync 同步。也可以从
`chrome-extension/local-settings.json.example` 复制本地配置模板；实际的
`chrome-extension/local-settings.json` 已被 Git 忽略，插件会在 Chrome 存储缺少凭据时读取该文件。
旧版扩展保存的 `readwiseToken` 会在升级后删除；Readwise Token 只由后端持有。

Service Token 应单独签发、设置到期时间，并在设备丢失或扩展凭据疑似泄漏时立即吊销。API AUD 不依赖动态的扩展 ID，但只能访问上述三个扩展接口。扩展升级后需要在 `chrome://extensions` 重新加载一次未打包扩展。

YouTube 页面打开后，扩展按视频 ID 查询 Reader 状态：`检查中…`、`已剪藏 ↗`、`剪藏` 或 `状态未知`。`已剪藏 ↗` 直接打开 Reader 文档，并显示次级 `重新处理`。状态查询超过 2 秒时会额外显示 `直接剪藏`，允许用户跳过状态确认并进入正常处理流程；查询仍会在后台继续，避免短暂延迟被误判为未剪藏。该按钮只绕过 Reader 状态查询，不是离线操作；Cloudflare 或 NAS API 本身不可达时仍会提交失败。Reader 索引预热最多等待约 120 秒，再次检查预热状态时不会重复启动同一轮刷新。

查询由后端先验证本地任务保存的 Reader 文档 ID；没有本地记录时，再使用 Reader `video` 文档索引按规范化 YouTube URL 匹配。索引完整状态持久化到 `uploads/cache`，默认每 30 分钟由首个状态查询在后台触发一次 `updatedAfter` 增量同步，并回看 5 分钟以避免游标边界漏项；强制检查也优先走增量同步。每 24 小时由首个查询触发一次限速全量校准，用于收敛 Reader 删除、分类变更等增量结果无法可靠表达的变化。刷新期间继续使用 last-known-good；旧映射未命中时返回预热状态，不会把未完成索引误判为未剪藏。已确认删除或查无此文档的本地 Reader ID 会从缓存匹配中排除。Reader Token 始终只存在于后端。

### 为什么同时有网页登录和 Service Token

两种认证保护的是不同入口，不要求用户对同一次操作重复认证：

- Cloudflare 身份登录用于本人访问 `readwise.gauss.surf` 网页，包括任务列表、详情和受保护的管理界面。
- Access Client ID 和 Secret 是扩展的机器凭据，用于后台访问 `readwise-api.gauss.surf`，使扩展能在网页未打开时提交并轮询任务。

仅使用 Cloudflare 身份登录在技术上可行，但扩展需要依赖跨域 Access Cookie，并额外处理交互式登录跳转、CORS、Cookie 发送和会话过期。Access Cookie 是 HttpOnly 且受应用和域名范围约束，扩展不能把网页登录状态当作稳定的后台 API 凭据。该方案会增加周期性重新登录和后台请求失败的概率，因此当前保留身份登录与 Service Token 分离的设计。

对用户而言仍然只有网页登录是交互步骤。扩展凭据由被 Git 忽略的 `chrome-extension/local-settings.json` 在本机管理，不需要日常手工填写；popup 后续可将凭据字段收纳到高级设置或仅显示本地配置状态。Service Token 的 API audience 在应用层仅允许任务提交和状态查询，不能访问指标或设置接口。

## 发布顺序

1. 先发布支持认证但保持 `ACCESS_AUTH_ENABLED=false` 的镜像。
2. 创建 Cloudflare Web/API 应用和 Service Token，验证两个域名的边缘策略。
3. 配置 NAS Compose、Telegram 内部 Token 和扩展本地凭据。
4. 设置 `ACCESS_AUTH_ENABLED=true`，仅重建主服务和 Telegram bot。
5. 验证匿名 Web/API 均被拒绝、身份登录可访问网页、Service Token 可提交和查询任务、Service Token 无法读取指标。
