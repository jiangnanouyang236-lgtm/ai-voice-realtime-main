# Docker Compose V4 使用说明

V4 使用根 `.env`、一个共享 Python 镜像和多个独立服务容器。专用天气 MCP 已移除；
实时天气继续通过数据库配置的 Websearch MCP 查询。

所有 V4 容器统一使用 Docker `json-file` 日志滚动：单文件最多 100 MiB，最多保留 5 个
文件。该限制只管理 Docker stdout/stderr 日志；应用自身若写文件日志，仍遵循应用日志配置。

## 服务

默认核心服务：

- `wzk-stt-grpc`
- `wzk-llm-grpc`
- `wzk-tts-grpc`
- `wzk-python-gateway`

可选 profile：

- `admin`：`wzk-admin-api`
- `mcp`：`wzk-mcp-utils`、`wzk-mcp-robot`、`wzk-mcp-singing`

Go Gateway 不在 V4 Compose 中，继续使用 host-network 入口：

```bash
make compose-go-up
```

生产歌曲 WAV 不进入 Git，也不会进入 Python 镜像。设置
`SINGING_AUDIO_V4_HOST_DIR=/absolute/path/to/singing/audio` 后，
`wzk-python-gateway` 会只读挂载到 `/app/singing/audio`。默认相对路径只适用于仓库根目录本地测试。

## 正式部署：不挂载源码

正式部署使用不可变镜像。建议把 `WZK_IMAGE_TAG` 设置为 Git commit，而不是长期复用
`dev` 或 `latest`。

```bash
docker compose --env-file .env -f docker-compose.v4.yml build
docker compose --env-file .env -f docker-compose.v4.yml up -d
```

启用 Admin 和本地 MCP：

```bash
docker compose --env-file .env -f docker-compose.v4.yml \
  --profile admin --profile mcp up -d
```

单独更新服务：

```bash
docker compose --env-file .env -f docker-compose.v4.yml build wzk-llm-grpc
docker compose --env-file .env -f docker-compose.v4.yml up -d --no-deps wzk-llm-grpc
```

## 开发与联调：挂载源码

开发 override 只读挂载源码目录和两个根 Python 模块，不挂载根 `.env`。修改普通 Python
代码后只需重启对应服务，不需要重建镜像：

```bash
docker compose --env-file .env \
  -f docker-compose.v4.yml \
  -f docker-compose.v4.dev.yml \
  up -d --build

docker compose --env-file .env \
  -f docker-compose.v4.yml \
  -f docker-compose.v4.dev.yml \
  restart wzk-llm-grpc
```

修改 `requirements.txt`、系统库或 Dockerfile 后仍必须重新构建。正式生产不要使用 dev
override：源码挂载会让运行结果依赖服务器工作目录，无法仅凭镜像 digest 回滚。

## 查询

```bash
docker compose --env-file .env -f docker-compose.v4.yml ps
docker compose --env-file .env -f docker-compose.v4.yml logs -f wzk-python-gateway
docker ps --filter label=com.wzk.stack=ai-voice
```

服务间使用 Compose DNS：

```text
grpc://wzk-stt-grpc:50054
grpc://wzk-llm-grpc:50053
grpc://wzk-tts-grpc:50052
http://wzk-mcp-utils:5004/mcp
http://wzk-mcp-robot:5003/mcp
http://wzk-mcp-singing:5005/mcp
```

Bot/MCP/Agent/TTS Profile 仍以 MySQL Runtime Snapshot 为准；Compose 不复制这些业务绑定。
迁移时必须在 Runtime Snapshot 中停用或删除旧 `weather_remote`，并确认实时天气只绑定
Websearch MCP；仓库删除本地服务不会自动修改线上数据库。

如果根 `.env` 中数据库、模型或 MCP URL 使用 `127.0.0.1`，迁入容器后该地址指向容器
自身。迁移前必须逐项改为真实远程地址、Compose service name，或经过审核的
`host.docker.internal`，不能直接照搬旧进程配置。
