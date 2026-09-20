use std::{
    io,
    net::{TcpStream, ToSocketAddrs},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

pub const TCP_CONNECT_TIMEOUT: Duration = Duration::from_millis(500);
pub const TCP_RECONNECT_INTERVAL: Duration = Duration::from_millis(500);

pub fn connect_with_timeout(address: &str, timeout: Duration) -> io::Result<TcpStream> {
    let mut last_error = None;
    let mut resolved = false;
    for socket_address in address.to_socket_addrs()? {
        resolved = true;
        match TcpStream::connect_timeout(&socket_address, timeout) {
            Ok(stream) => return Ok(stream),
            Err(err) => last_error = Some(err),
        }
    }
    Err(last_error.unwrap_or_else(|| {
        io::Error::new(
            io::ErrorKind::AddrNotAvailable,
            if resolved {
                "TCP address did not accept connections"
            } else {
                "TCP address resolved to no socket addresses"
            },
        )
    }))
}

pub fn wait_interruptibly(stop: &AtomicBool, duration: Duration) {
    let deadline = Instant::now() + duration;
    while !stop.load(Ordering::Acquire) {
        let now = Instant::now();
        if now >= deadline {
            return;
        }
        std::thread::park_timeout((deadline - now).min(Duration::from_millis(50)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn connects_to_resolved_loopback_address() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let address = listener.local_addr().expect("listener address");
        let client = connect_with_timeout(&address.to_string(), Duration::from_secs(1))
            .expect("connect client");
        let (_server, _) = listener.accept().expect("accept client");
        drop(client);
    }

    #[test]
    fn interruptible_wait_observes_stop_quickly() {
        let stop = AtomicBool::new(true);
        let started = Instant::now();
        wait_interruptibly(&stop, Duration::from_secs(1));
        assert!(started.elapsed() < Duration::from_millis(100));
    }
}
