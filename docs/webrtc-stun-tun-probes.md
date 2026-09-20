# WebRTC STUN/TURN 探测

术语：STUN 用于发现公网映射，TURN 是中继服务；TUN/utun 是本机虚拟网卡，不是 WebRTC relay。

## 1. STUN Binding

Rust：

```bash
cd rust_client
STUN_SERVER=stun.example.com:3478 cargo run --bin stun_probe
```

Go：

```bash
cd tools/stun_probe_go
go run . -server stun.example.com:3478 -count 3
```

两端都失败时先查 DNS、UDP、防火墙和 NAT；只有一端失败时再比较运行时权限、路由和绑定地址。

## 2. WebRTC/ICE

```bash
cd tools/webrtc_probe_go
go run . \
  -stun stun:stun.example.com:3478 \
  -turn turn:turn.example.com:3478 \
  -user "$TURN_USER" \
  -credential "$TURN_PASSWORD"
```

TURN 凭据只从环境变量读取，不写入命令历史、文档或仓库文件。

## 3. 真实 Gateway route

```bash
cd go_voice_gateway
RTC_ROUTE_PROBE_WS_URL=wss://gateway.example.com/ws \
RTC_ROUTE_PROBE_ROBOT_ID="$ROBOT_ID" \
RTC_ROUTE_PROBE_ROBOT_SECRET="$ROBOT_SECRET" \
go run ./cmd/rtc_route_probe
```

关注：

- selected candidate pair 是 host/srflx 还是 relay。
- Go Gateway UDP 范围是否映射/放行。
- Rust 和 Go 是否都只采集预期的 `udp4`/`udp6`。
- 强制 `relay` 时是否能收集 relay candidate 并完成 DTLS/DataChannel。

## 4. 判读

- STUN 成功、ICE direct 失败：检查 Gateway 公网 UDP 映射、容器网络和候选地址。
- direct 失败、TURN UDP 成功：网络需要 relay，性能基线应单独记录。
- TURN UDP 失败、TURN TCP/TLS URL 被 Rust 跳过：当前 Rust native 主链只验证 UDP，应先解决 UDP 或单独实现/验证 TCP/TLS。
- 所有 route 都失败：核对 credential、realm、时钟、防火墙和证书。

探测结果必须记录时间、客户端/服务端网络、ICE policy、candidate pair、RTT/丢包和对应 commit，避免把一次环境结果写成永久默认。
