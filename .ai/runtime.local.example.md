# Machine-local Runtime Context

复制为 `.ai/runtime.local.md`，填写当前机器事实。该文件被 Git 忽略，供本机上的 AI 和协作者读取，不要在这里重复保存密钥值。

## Current role

- Machine role: `<development | model-host | robot-host>`
- Network path: `<LAN | ZeroTier | FRP | public>`
- Last verified: `<YYYY-MM-DD HH:mm TZ>`
- Verified commit: `<git sha>`

## Server registry

| Service | Host | Port | Source | Last live check |
|---|---|---:|---|---|
| Model host | `<host>` |  | `.env` | UNKNOWN |
| Gateway | `<host>` | `<port>` | `rust_client/.env.local` | UNKNOWN |
| MQTT | `<host>` | `<port>` | `.env` | UNKNOWN |

## Access

- SSH alias: `<none or alias>`
- Working directory: `<remote path>`
- Restrictions: `<no sudo / no prune / do not restart unrelated services>`

## Proxy

- Preferred HTTP proxy: `<host:port or none>`
- Alternative proxy: `<host:port or none>`
- `NO_PROXY`: `<internal hosts>`
- Verification command: `<redacted command>`

## Hardware

- Robot: `<id stored in env; do not paste secret>`
- Audio frontend: `<mode and host:port>`
- Camera snapshot path: `<path>`

## Known limitations

- `<fact, inference or unknown>`
