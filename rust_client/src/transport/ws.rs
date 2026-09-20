use super::{TransportEvent, TransportKind, VoiceTransport};
use crate::{
    gateway::{GatewayConnection, GatewayEvent},
    protocol::{AudioFrameHeader, ClientMessage},
};
use anyhow::Result;
use futures_util::future::BoxFuture;
use std::time::Duration;
use tokio::sync::mpsc;

#[derive(Clone)]
pub struct WsVoiceTransport {
    connection: GatewayConnection,
}

impl WsVoiceTransport {
    pub async fn connect(
        url: &str,
        connect_timeout: Duration,
        write_timeout: Duration,
    ) -> Result<(Self, mpsc::Receiver<TransportEvent>)> {
        let (connection, gateway_events) =
            GatewayConnection::connect(url, connect_timeout, write_timeout).await?;
        let transport_events = spawn_event_adapter(gateway_events);
        Ok((Self { connection }, transport_events))
    }
}

impl VoiceTransport for WsVoiceTransport {
    fn kind(&self) -> TransportKind {
        TransportKind::WebSocket
    }

    fn send_control<'a>(&'a self, message: &'a ClientMessage) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move { self.connection.send(message).await })
    }

    fn send_audio_frame<'a>(
        &'a self,
        header: &'a AudioFrameHeader,
        payload: &'a [u8],
    ) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move { self.connection.send_audio_frame(header, payload).await })
    }
}

fn spawn_event_adapter(
    mut gateway_events: mpsc::Receiver<GatewayEvent>,
) -> mpsc::Receiver<TransportEvent> {
    let (transport_tx, transport_rx) = mpsc::channel(128);
    tokio::spawn(async move {
        while let Some(event) = gateway_events.recv().await {
            let event = match event {
                GatewayEvent::Message(message) => TransportEvent::Message(message),
                GatewayEvent::AudioFrame(frame) => TransportEvent::AudioFrame(frame),
                GatewayEvent::Closed => TransportEvent::Closed,
                GatewayEvent::Error(err) => TransportEvent::Error(err),
            };
            if transport_tx.send(event).await.is_err() {
                break;
            }
        }
    });
    transport_rx
}
