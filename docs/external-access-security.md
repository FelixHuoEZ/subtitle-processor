# 公网访问安全边界

公网部署使用三条独立认证路径，避免把浏览器登录凭据、扩展凭据和容器内部凭据混用。

## 访问模型

| 入口 | Cloudflare Access 策略 | 应用端权限 |
| --- | --- | --- |
| `https://readwise.gauss.surf` | 身份登录 Allow | 完整网页与 API 权限 |
| `https://readwise-api.gauss.surf` | Service Auth | 仅 `POST /process` 和 `GET /process/status/<id>` |
| Docker 内网 `http://subtitle-processor:5000` | 不经过 Access | `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>` |

`GET /health` 专供容器健康检查，不要求应用端认证。`/health/metrics`、设置接口、任务详情和管理页面都需要认证。

## Cloudflare 配置

1. 创建 Web 类型的 Self-hosted application，域名为 `readwise.gauss.surf`，只允许指定身份登录。
2. 创建 API 类型的 Self-hosted application，域名为 `readwise-api.gauss.surf`，策略动作为 `Service Auth`，只包含专用 Service Token。
3. API 应用为扩展预检请求返回 `GET, POST, OPTIONS` 和 Access Service Token 请求头。实际请求仍必须通过 Service Auth，应用端 API AUD 也只允许提交和查询状态。
4. 让 Cloudflare Tunnel 的两个公开主机名都指向同一个字幕服务源站。
5. 分别记录两个应用的 Application Audience (AUD) Tag。不要把 Service Token Secret 或内部 Token 写入仓库。

应用端配置：

```env
ACCESS_AUTH_ENABLED=true
ACCESS_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
ACCESS_WEB_APPLICATION_AUD=web-application-aud
ACCESS_API_APPLICATION_AUD=api-application-aud
ACCESS_ALLOWED_ORIGINS=https://readwise.gauss.surf
INTERNAL_SERVICE_TOKEN=long-random-value
```

启用前必须同时配置两个 AUD、Team Domain 和内部 Token，否则服务会拒绝启动，避免误以为源站已经受保护。

## Chrome 扩展

扩展使用独立的 API 地址和网页地址：

- API 地址：`https://readwise-api.gauss.surf`
- 网页地址：`https://readwise.gauss.surf`

Access Client ID 和 Secret 保存在 `chrome.storage.local`，不会通过 Chrome Sync 同步。旧版扩展保存的 `readwiseToken` 会在升级后删除；Readwise Token 只由后端持有。

Service Token 应单独签发、设置到期时间，并在设备丢失或扩展凭据疑似泄漏时立即吊销。API AUD 不依赖动态的扩展 ID，但只能访问提交和状态查询接口。扩展升级后需要在 `chrome://extensions` 重新加载一次未打包扩展。

## 发布顺序

1. 先发布支持认证但保持 `ACCESS_AUTH_ENABLED=false` 的镜像。
2. 创建 Cloudflare Web/API 应用和 Service Token，验证两个域名的边缘策略。
3. 配置 NAS Compose、Telegram 内部 Token 和扩展本地凭据。
4. 设置 `ACCESS_AUTH_ENABLED=true`，仅重建主服务和 Telegram bot。
5. 验证匿名 Web/API 均被拒绝、身份登录可访问网页、Service Token 可提交和查询任务、Service Token 无法读取指标。
