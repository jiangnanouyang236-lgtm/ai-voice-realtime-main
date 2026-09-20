# Security Policy

这个仓库当前是私人仓库，但仍按“可能未来开放协作”的标准维护。

## Secrets

不要提交真实密钥或生产配置，包括但不限于：

- `LLM_API_KEY` / `LLM_ROUTER_API_KEY` / `WEBSEARCH_API_KEY` / `QWEN3_TTS_CUSTOM_VOICE_API_KEY` / `QWEN3_TTS_BASE_API_KEY`
- `CONFIG_DATABASE_URL`
- `ADMIN_PASSWORD` / `ADMIN_SESSION_SECRET`
- `ROBOT_SECRET`
- MQTT 用户名和密码
- 第三方服务 Token

真实值应放在部署环境变量或本地 `.env` 中。`.env` 已被 `.gitignore` 忽略。
包含真实凭据的本地环境文件必须使用 `0600` 权限；可用
`python scripts/ai_preflight.py --profile lan` 做脱敏检查。

## Runtime Exposure

上线前重点检查：

- Admin UI 必须开启登录鉴权，禁止 `ADMIN_AUTH_DISABLED=true`。
- 公网 HTTPS 部署时建议 `ADMIN_COOKIE_SECURE=true`。
- Gateway 如果暴露公网，建议开启 `GATEWAY_REQUIRE_ROBOT_SECRET=true` 并给每台 Robot 分配 secret。
- 本地控制接口、MQTT、MCP 服务端口默认不要暴露到公网。

## Reporting

如果发现敏感信息已进入 Git 历史：

1. 立即轮换对应密钥。
2. 对私人仓库，优先评估是否需要 history rewrite。
3. 对外部已分发仓库，必须视为密钥已泄露。

如果发现可被远程利用的问题，请先不要在公开 Issue 中粘贴可复现攻击细节，优先通过私下渠道同步维护者。
