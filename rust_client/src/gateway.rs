use crate::protocol::{
    decode_audio_frame, encode_audio_frame, AudioFrame, AudioFrameHeader, ClientMessage,
    ServerMessage,
};
use anyhow::{anyhow, Context, Result};
use futures_util::{
    stream::{SplitSink, SplitStream},
    SinkExt, StreamExt,
};
use tokio::{
    net::TcpStream,
    sync::{mpsc, Mutex},
    time,
};
use tokio_tungstenite::{connect_async, tungstenite::Message, MaybeTlsStream, WebSocketStream};

use std::{sync::Arc, time::Duration};

pub type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;
pub type WsWrite = SplitSink<WsStream, Message>;
pub type WsRead = SplitStream<WsStream>;

#[derive(Debug)]
pub enum GatewayEvent {
    Message(ServerMessage),
    AudioFrame(AudioFrame),
    Closed,
    Error(anyhow::Error),
}

#[derive(Clone)]
pub struct GatewayConnection {
    write: Arc<Mutex<WsWrite>>,
    write_timeout: Duration,
}

impl GatewayConnection {
    pub async fn connect(
        url: &str,
        connect_timeout: Duration,
        write_timeout: Duration,
    ) -> Result<(Self, mpsc::Receiver<GatewayEvent>)> {
        let (stream, _) = time::timeout(connect_timeout, connect_async(url))
            .await
            .with_context(|| format!("WebSocket 连接超时: {url}"))?
            .with_context(|| format!("WebSocket 连接失败: {url}"))?;
        let (write, read) = stream.split();
        let write = Arc::new(Mutex::new(write));
        let (event_tx, event_rx) = mpsc::channel(128);

        tokio::spawn(read_loop(read, Arc::clone(&write), write_timeout, event_tx));

        Ok((
            Self {
                write,
                write_timeout,
            },
            event_rx,
        ))
    }

    pub async fn send(&self, message: &ClientMessage) -> Result<()> {
        let payload = serde_json::to_string(message).context("序列化客户端消息失败")?;
        time::timeout(self.write_timeout, async {
            let mut write = self.write.lock().await;
            write
                .send(Message::Text(payload))
                .await
                .context("发送 WebSocket 文本消息失败")
        })
        .await
        .context("发送 WebSocket 文本消息超时")?
    }

    pub async fn send_audio_frame(&self, header: &AudioFrameHeader, payload: &[u8]) -> Result<()> {
        let frame = encode_audio_frame(header, payload)?;
        time::timeout(self.write_timeout, async {
            let mut write = self.write.lock().await;
            write
                .send(Message::Binary(frame))
                .await
                .context("发送 WebSocket 二进制音频帧失败")
        })
        .await
        .context("发送 WebSocket 二进制音频帧超时")?
    }
}

async fn read_loop(
    mut read: WsRead,
    write: Arc<Mutex<WsWrite>>,
    write_timeout: Duration,
    event_tx: mpsc::Sender<GatewayEvent>,
) {
    loop {
        let Some(frame) = read.next().await else {
            let _ = event_tx.send(GatewayEvent::Closed).await;
            return;
        };

        let frame = match frame.context("读取 WebSocket 消息失败") {
            Ok(frame) => frame,
            Err(err) => {
                let _ = event_tx.send(GatewayEvent::Error(err)).await;
                return;
            }
        };

        match frame {
            Message::Text(text) => match serde_json::from_str::<ServerMessage>(&text)
                .with_context(|| format!("解析服务端消息失败: {text}"))
            {
                Ok(message) => {
                    if event_tx.send(GatewayEvent::Message(message)).await.is_err() {
                        return;
                    }
                }
                Err(err) => {
                    let _ = event_tx.send(GatewayEvent::Error(err)).await;
                    return;
                }
            },
            Message::Binary(frame) => match decode_audio_frame(&frame) {
                Ok(audio_frame) => {
                    if event_tx
                        .send(GatewayEvent::AudioFrame(audio_frame))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
                Err(err) => {
                    let _ = event_tx
                        .send(GatewayEvent::Error(anyhow!(
                            "解析 Gateway 二进制音频帧失败: {err}"
                        )))
                        .await;
                    return;
                }
            },
            Message::Ping(payload) => {
                let pong_result = time::timeout(write_timeout, async {
                    let mut write = write.lock().await;
                    write
                        .send(Message::Pong(payload))
                        .await
                        .context("回复 WebSocket Ping 失败")
                })
                .await
                .context("回复 WebSocket Ping 超时")
                .and_then(|result| result);

                if let Err(err) = pong_result {
                    let _ = event_tx.send(GatewayEvent::Error(err)).await;
                    return;
                }
            }
            Message::Pong(_) => {}
            Message::Frame(_) => {}
            Message::Close(_) => {
                let _ = event_tx.send(GatewayEvent::Closed).await;
                return;
            }
        }
    }
}
