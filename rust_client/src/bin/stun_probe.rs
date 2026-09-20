use std::{
    env,
    error::Error,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, ToSocketAddrs, UdpSocket},
    process,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const MAGIC_COOKIE: u32 = 0x2112_A442;
const BINDING_REQUEST: u16 = 0x0001;
const BINDING_SUCCESS_RESPONSE: u16 = 0x0101;
const ATTR_MAPPED_ADDRESS: u16 = 0x0001;
const ATTR_XOR_MAPPED_ADDRESS: u16 = 0x0020;

#[derive(Debug)]
struct Args {
    server: String,
    bind: String,
    timeout: Duration,
    count: u32,
}

#[derive(Debug)]
struct StunBindingResponse {
    mapped_addr: Option<SocketAddr>,
    xor_mapped_addr: Option<SocketAddr>,
}

fn main() {
    if let Err(err) = run() {
        eprintln!("verdict=fail error={err}");
        process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let args = parse_args(env::args().skip(1))?;
    let server = resolve_server(&args.server)?;

    println!(
        "stun_probe runtime=rust server={} resolved={} bind={} timeout_ms={} count={}",
        args.server,
        server,
        args.bind,
        args.timeout.as_millis(),
        args.count
    );

    let mut ok = 0u32;
    for attempt in 1..=args.count {
        match probe_once(&args, server, attempt) {
            Ok(()) => ok += 1,
            Err(err) => println!("attempt={attempt} verdict=fail error={err}"),
        }
    }

    if ok == 0 {
        return Err("all STUN binding attempts failed".into());
    }

    println!("summary verdict=ok success={ok}/{}", args.count);
    Ok(())
}

fn parse_args(args: impl Iterator<Item = String>) -> Result<Args, Box<dyn Error>> {
    let mut server =
        env::var("STUN_SERVER").unwrap_or_else(|_| "stun.l.google.com:19302".to_string());
    let mut bind = env::var("STUN_BIND").unwrap_or_else(|_| "0.0.0.0:0".to_string());
    let mut timeout_ms = env_u64("STUN_TIMEOUT_MS", 3_000);
    let mut count = env_u32("STUN_PROBE_COUNT", 3);

    let mut args = args.peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--server" => server = next_value(&mut args, "--server")?,
            "--bind" => bind = next_value(&mut args, "--bind")?,
            "--bind-ip" => bind = bind_ip_to_socket(&next_value(&mut args, "--bind-ip")?)?,
            "--timeout-ms" => timeout_ms = next_value(&mut args, "--timeout-ms")?.parse()?,
            "--count" => count = next_value(&mut args, "--count")?.parse()?,
            "--help" | "-h" => {
                print_help();
                process::exit(0);
            }
            other => return Err(format!("unknown argument: {other}").into()),
        }
    }

    Ok(Args {
        server,
        bind,
        timeout: Duration::from_millis(timeout_ms.max(1)),
        count: count.max(1),
    })
}

fn next_value(
    args: &mut std::iter::Peekable<impl Iterator<Item = String>>,
    name: &str,
) -> Result<String, Box<dyn Error>> {
    args.next()
        .ok_or_else(|| format!("{name} requires a value").into())
}

fn env_u64(name: &str, default: u64) -> u64 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn env_u32(name: &str, default: u32) -> u32 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn print_help() {
    println!(
        "Usage: stun_probe [--server host:port] [--bind ip:port] [--bind-ip ip] [--timeout-ms ms] [--count n]"
    );
    println!("Env: STUN_SERVER, STUN_BIND, STUN_TIMEOUT_MS, STUN_PROBE_COUNT");
}

fn bind_ip_to_socket(ip: &str) -> Result<String, Box<dyn Error>> {
    match ip.trim().parse::<IpAddr>()? {
        IpAddr::V4(addr) => Ok(format!("{addr}:0")),
        IpAddr::V6(addr) => Ok(format!("[{addr}]:0")),
    }
}

fn resolve_server(server: &str) -> Result<SocketAddr, Box<dyn Error>> {
    server
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| format!("failed to resolve STUN server: {server}").into())
}

fn probe_once(args: &Args, server: SocketAddr, attempt: u32) -> Result<(), Box<dyn Error>> {
    let socket = UdpSocket::bind(&args.bind)?;
    socket.set_read_timeout(Some(args.timeout))?;
    socket.set_write_timeout(Some(args.timeout))?;

    let tx_id = make_transaction_id(attempt);
    let request = build_binding_request(&tx_id);
    let started = Instant::now();
    socket.send_to(&request, server)?;

    let mut buf = [0u8; 1500];
    let (len, from) = socket.recv_from(&mut buf)?;
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
    let response = parse_binding_response(&buf[..len], &tx_id)?;

    let mapped = response
        .xor_mapped_addr
        .or(response.mapped_addr)
        .ok_or("STUN response did not include mapped address")?;

    println!(
        "attempt={attempt} verdict=ok local={} from={} mapped={} rtt_ms={elapsed_ms:.1}",
        socket.local_addr()?,
        from,
        mapped
    );
    Ok(())
}

fn build_binding_request(tx_id: &[u8; 12]) -> [u8; 20] {
    let mut packet = [0u8; 20];
    packet[0..2].copy_from_slice(&BINDING_REQUEST.to_be_bytes());
    packet[2..4].copy_from_slice(&0u16.to_be_bytes());
    packet[4..8].copy_from_slice(&MAGIC_COOKIE.to_be_bytes());
    packet[8..20].copy_from_slice(tx_id);
    packet
}

fn make_transaction_id(sequence: u32) -> [u8; 12] {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos() as u64)
        .unwrap_or(0);
    let mut seed = nanos ^ ((process::id() as u64) << 32) ^ sequence as u64;
    let mut tx_id = [0u8; 12];
    for byte in &mut tx_id {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        *byte = (seed & 0xff) as u8;
    }
    tx_id
}

fn parse_binding_response(
    packet: &[u8],
    expected_tx_id: &[u8; 12],
) -> Result<StunBindingResponse, Box<dyn Error>> {
    if packet.len() < 20 {
        return Err("STUN packet too short".into());
    }

    let msg_type = u16::from_be_bytes([packet[0], packet[1]]);
    if msg_type != BINDING_SUCCESS_RESPONSE {
        return Err(format!("unexpected STUN message type: 0x{msg_type:04x}").into());
    }

    let msg_len = u16::from_be_bytes([packet[2], packet[3]]) as usize;
    if packet.len() < 20 + msg_len {
        return Err("STUN packet truncated".into());
    }

    let cookie = u32::from_be_bytes([packet[4], packet[5], packet[6], packet[7]]);
    if cookie != MAGIC_COOKIE {
        return Err("STUN magic cookie mismatch".into());
    }
    if &packet[8..20] != expected_tx_id {
        return Err("STUN transaction id mismatch".into());
    }

    let mut response = StunBindingResponse {
        mapped_addr: None,
        xor_mapped_addr: None,
    };

    let mut offset = 20usize;
    let end = 20 + msg_len;
    while offset + 4 <= end {
        let attr_type = u16::from_be_bytes([packet[offset], packet[offset + 1]]);
        let attr_len = u16::from_be_bytes([packet[offset + 2], packet[offset + 3]]) as usize;
        offset += 4;
        if offset + attr_len > end {
            return Err("STUN attribute truncated".into());
        }

        let value = &packet[offset..offset + attr_len];
        match attr_type {
            ATTR_MAPPED_ADDRESS => response.mapped_addr = parse_address(value).ok(),
            ATTR_XOR_MAPPED_ADDRESS => {
                response.xor_mapped_addr = parse_xor_address(value, expected_tx_id).ok()
            }
            _ => {}
        }
        offset += (attr_len + 3) & !3;
    }

    Ok(response)
}

fn parse_address(value: &[u8]) -> Result<SocketAddr, Box<dyn Error>> {
    if value.len() < 4 || value[0] != 0 {
        return Err("invalid MAPPED-ADDRESS attribute".into());
    }
    let port = u16::from_be_bytes([value[2], value[3]]);
    parse_address_body(value[1], port, &value[4..])
}

fn parse_xor_address(value: &[u8], tx_id: &[u8; 12]) -> Result<SocketAddr, Box<dyn Error>> {
    if value.len() < 4 || value[0] != 0 {
        return Err("invalid XOR-MAPPED-ADDRESS attribute".into());
    }
    let port = u16::from_be_bytes([value[2], value[3]]) ^ ((MAGIC_COOKIE >> 16) as u16);
    let mut body = value[4..].to_vec();
    let cookie_bytes = MAGIC_COOKIE.to_be_bytes();
    for (idx, byte) in body.iter_mut().enumerate() {
        let mask = if idx < 4 {
            cookie_bytes[idx]
        } else {
            tx_id[idx - 4]
        };
        *byte ^= mask;
    }
    parse_address_body(value[1], port, &body)
}

fn parse_address_body(family: u8, port: u16, body: &[u8]) -> Result<SocketAddr, Box<dyn Error>> {
    match family {
        0x01 if body.len() >= 4 => Ok(SocketAddr::new(
            IpAddr::V4(Ipv4Addr::new(body[0], body[1], body[2], body[3])),
            port,
        )),
        0x02 if body.len() >= 16 => {
            let mut addr = [0u8; 16];
            addr.copy_from_slice(&body[..16]);
            Ok(SocketAddr::new(IpAddr::V6(Ipv6Addr::from(addr)), port))
        }
        other => Err(format!("unsupported STUN address family: {other}").into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_xor_mapped_ipv4_response() {
        let tx_id = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
        let mapped_ip = [203, 0, 113, 10];
        let mapped_port = 54_321u16;
        let mut attr_value = vec![0, 0x01];
        attr_value.extend_from_slice(&(mapped_port ^ ((MAGIC_COOKIE >> 16) as u16)).to_be_bytes());
        for (idx, byte) in mapped_ip.iter().enumerate() {
            attr_value.push(byte ^ MAGIC_COOKIE.to_be_bytes()[idx]);
        }

        let mut packet = Vec::new();
        packet.extend_from_slice(&BINDING_SUCCESS_RESPONSE.to_be_bytes());
        packet.extend_from_slice(&(12u16).to_be_bytes());
        packet.extend_from_slice(&MAGIC_COOKIE.to_be_bytes());
        packet.extend_from_slice(&tx_id);
        packet.extend_from_slice(&ATTR_XOR_MAPPED_ADDRESS.to_be_bytes());
        packet.extend_from_slice(&(attr_value.len() as u16).to_be_bytes());
        packet.extend_from_slice(&attr_value);

        let response = parse_binding_response(&packet, &tx_id).unwrap();

        assert_eq!(
            response.xor_mapped_addr,
            Some(SocketAddr::new(
                IpAddr::V4(Ipv4Addr::from(mapped_ip)),
                mapped_port
            ))
        );
    }
}
