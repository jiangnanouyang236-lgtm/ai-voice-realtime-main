use anyhow::{bail, Context, Result};
use image::codecs::jpeg::JpegEncoder;
use image::imageops::{overlay, FilterType};
use image::metadata::Orientation;
use image::{DynamicImage, GenericImageView, ImageDecoder, ImageFormat, Limits, Rgb, RgbImage};
use serde::Deserialize;
use std::fs;
use std::io::{Cursor, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEFAULT_WIDTH: u32 = 640;
pub const DEFAULT_HEIGHT: u32 = 360;
pub const DEFAULT_MAX_OUTPUT_BYTES: usize = 100 * 1024;
pub const DEFAULT_MAX_INPUT_BYTES: u64 = 5 * 1024 * 1024;
pub const VISION_DATA_CHANNEL_LABEL: &str = "vision-v1";
pub const VISION_CHUNK_HEADER_BYTES: usize = 32;
pub const VISION_CHUNK_PAYLOAD_BYTES: usize = 32 * 1024;
pub const VISION_MAX_CHUNKS: usize = 4;
const VISION_PROTOCOL_VERSION: u8 = 1;
const VISION_CHUNK_MAGIC: &[u8; 4] = b"VIS1";

#[derive(Debug, Clone)]
pub struct SnapshotPolicy {
    pub width: u32,
    pub height: u32,
    pub max_output_bytes: usize,
    pub max_input_bytes: u64,
    pub preferred_jpeg_quality: u8,
    pub minimum_jpeg_quality: u8,
}

impl Default for SnapshotPolicy {
    fn default() -> Self {
        Self {
            width: DEFAULT_WIDTH,
            height: DEFAULT_HEIGHT,
            max_output_bytes: DEFAULT_MAX_OUTPUT_BYTES,
            max_input_bytes: DEFAULT_MAX_INPUT_BYTES,
            preferred_jpeg_quality: 85,
            minimum_jpeg_quality: 40,
        }
    }
}

#[derive(Debug)]
pub struct PreparedSnapshot {
    pub jpeg: Vec<u8>,
    pub width: u32,
    pub height: u32,
    pub passthrough: bool,
}

#[derive(Debug)]
pub struct ConsumedSnapshot {
    pub snapshot: PreparedSnapshot,
    pub captured_at_ms: i64,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct VisionSnapshotAck {
    #[serde(rename = "type")]
    pub message_type: String,
    pub frame_id: Option<String>,
    pub status: String,
    pub reason: Option<String>,
    pub received_at_ms: i64,
}

/// Claims one producer-published image and removes the claimed file after processing.
///
/// The producer must first write a sibling temporary file, then atomically rename it
/// to `input_path`. This function renames the published file before reading it, so a
/// later producer write cannot replace the bytes being processed.
///
pub fn consume_snapshot(
    input_path: &Path,
    policy: &SnapshotPolicy,
) -> Result<Option<ConsumedSnapshot>> {
    let claimed_path = claim_path(input_path)?;
    let claim = match fs::rename(input_path, &claimed_path) {
        Ok(()) => ClaimedFile::new(claimed_path),
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("无法占用待处理图片 {}", input_path.display()))
        }
    };

    let metadata = fs::metadata(claim.path())
        .with_context(|| format!("无法读取图片元数据 {}", claim.path().display()))?;
    if metadata.len() > policy.max_input_bytes {
        bail!(
            "输入图片过大: {} bytes，限制为 {} bytes",
            metadata.len(),
            policy.max_input_bytes
        );
    }
    let captured_at_ms = metadata
        .modified()
        .ok()
        .and_then(system_time_millis)
        .unwrap_or_else(current_time_millis);

    let bytes = fs::read(claim.path())
        .with_context(|| format!("无法读取图片 {}", claim.path().display()))?;
    let snapshot = prepare_snapshot(&bytes, policy)?;
    Ok(Some(ConsumedSnapshot {
        snapshot,
        captured_at_ms,
    }))
}

pub fn encode_vision_chunks(
    frame_id: u64,
    captured_at_ms: i64,
    jpeg: &[u8],
) -> Result<Vec<Vec<u8>>> {
    if frame_id == 0 {
        bail!("vision frame_id 必须大于 0");
    }
    if captured_at_ms < 0 {
        bail!("vision captured_at_ms 不能为负数");
    }
    if jpeg.is_empty() || jpeg.len() > DEFAULT_MAX_OUTPUT_BYTES {
        bail!(
            "vision JPEG 体积无效: {} bytes，限制为 {} bytes",
            jpeg.len(),
            DEFAULT_MAX_OUTPUT_BYTES
        );
    }
    let chunk_count = jpeg.len().div_ceil(VISION_CHUNK_PAYLOAD_BYTES);
    if chunk_count == 0 || chunk_count > VISION_MAX_CHUNKS {
        bail!("vision 分片数量无效: {chunk_count}");
    }

    let mut chunks = Vec::with_capacity(chunk_count);
    for (chunk_index, payload) in jpeg.chunks(VISION_CHUNK_PAYLOAD_BYTES).enumerate() {
        let mut chunk = Vec::with_capacity(VISION_CHUNK_HEADER_BYTES + payload.len());
        chunk.extend_from_slice(VISION_CHUNK_MAGIC);
        chunk.push(VISION_PROTOCOL_VERSION);
        chunk.push(0);
        chunk.extend_from_slice(&(VISION_CHUNK_HEADER_BYTES as u16).to_be_bytes());
        chunk.extend_from_slice(&frame_id.to_be_bytes());
        chunk.extend_from_slice(&(captured_at_ms as u64).to_be_bytes());
        chunk.extend_from_slice(&(jpeg.len() as u32).to_be_bytes());
        chunk.extend_from_slice(&(chunk_index as u16).to_be_bytes());
        chunk.extend_from_slice(&(chunk_count as u16).to_be_bytes());
        debug_assert_eq!(chunk.len(), VISION_CHUNK_HEADER_BYTES);
        chunk.extend_from_slice(payload);
        chunks.push(chunk);
    }
    Ok(chunks)
}

pub fn parse_vision_ack(data: &[u8]) -> Result<VisionSnapshotAck> {
    let ack: VisionSnapshotAck =
        serde_json::from_slice(data).context("解析 vision_snapshot_ack 失败")?;
    if ack.message_type != "vision_snapshot_ack" {
        bail!("未知 vision ACK 类型: {}", ack.message_type);
    }
    if ack.status != "accepted" && ack.status != "dropped" {
        bail!("未知 vision ACK 状态: {}", ack.status);
    }
    Ok(ack)
}

pub fn prepare_snapshot(bytes: &[u8], policy: &SnapshotPolicy) -> Result<PreparedSnapshot> {
    validate_policy(policy)?;
    if bytes.is_empty() {
        bail!("输入图片为空");
    }
    if bytes.len() as u64 > policy.max_input_bytes {
        bail!(
            "输入图片过大: {} bytes，限制为 {} bytes",
            bytes.len(),
            policy.max_input_bytes
        );
    }

    let mut reader = image::ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .context("无法识别输入图片格式")?;
    let mut limits = Limits::default();
    limits.max_image_width = Some(8_192);
    limits.max_image_height = Some(8_192);
    limits.max_alloc = Some(64 * 1024 * 1024);
    reader.limits(limits);
    let format = reader.format();
    let mut decoder = reader.into_decoder().context("无法创建图片解码器")?;
    let orientation = decoder.orientation().context("无法读取图片方向")?;
    let mut decoded = DynamicImage::from_decoder(decoder).context("无法解码输入图片")?;
    decoded.apply_orientation(orientation);
    let (source_width, source_height) = decoded.dimensions();

    if format == Some(ImageFormat::Jpeg)
        && orientation == Orientation::NoTransforms
        && source_width == policy.width
        && source_height == policy.height
        && bytes.len() <= policy.max_output_bytes
    {
        return Ok(PreparedSnapshot {
            jpeg: bytes.to_vec(),
            width: policy.width,
            height: policy.height,
            passthrough: true,
        });
    }

    let normalized = letterbox(decoded, policy.width, policy.height);
    let jpeg = encode_with_size_limit(&normalized, policy)?;
    Ok(PreparedSnapshot {
        jpeg,
        width: policy.width,
        height: policy.height,
        passthrough: false,
    })
}

fn validate_policy(policy: &SnapshotPolicy) -> Result<()> {
    if policy.width == 0 || policy.height == 0 {
        bail!("目标图片尺寸必须大于 0");
    }
    if policy.max_output_bytes == 0 || policy.max_input_bytes == 0 {
        bail!("图片体积限制必须大于 0");
    }
    if !(1..=100).contains(&policy.preferred_jpeg_quality)
        || !(1..=100).contains(&policy.minimum_jpeg_quality)
        || policy.minimum_jpeg_quality > policy.preferred_jpeg_quality
    {
        bail!("JPEG 质量范围无效");
    }
    Ok(())
}

fn letterbox(source: DynamicImage, width: u32, height: u32) -> DynamicImage {
    let (source_width, source_height) = source.dimensions();
    let scale = (width as f64 / source_width as f64)
        .min(height as f64 / source_height as f64)
        .min(1.0);
    let resized_width = ((source_width as f64 * scale).round() as u32).max(1);
    let resized_height = ((source_height as f64 * scale).round() as u32).max(1);
    let resized = if resized_width == source_width && resized_height == source_height {
        source.to_rgb8()
    } else {
        source
            .resize_exact(resized_width, resized_height, FilterType::Lanczos3)
            .to_rgb8()
    };

    let mut canvas = RgbImage::from_pixel(width, height, Rgb([0, 0, 0]));
    let x = i64::from((width - resized_width) / 2);
    let y = i64::from((height - resized_height) / 2);
    overlay(&mut canvas, &resized, x, y);
    DynamicImage::ImageRgb8(canvas)
}

fn encode_with_size_limit(image: &DynamicImage, policy: &SnapshotPolicy) -> Result<Vec<u8>> {
    let mut quality = policy.preferred_jpeg_quality;
    loop {
        let mut output = Vec::new();
        JpegEncoder::new_with_quality(&mut output, quality)
            .encode_image(image)
            .with_context(|| format!("JPEG 编码失败，quality={quality}"))?;
        if output.len() <= policy.max_output_bytes {
            return Ok(output);
        }
        if quality <= policy.minimum_jpeg_quality {
            bail!(
                "图片在最低 JPEG 质量 {} 下仍有 {} bytes，超过限制 {} bytes",
                quality,
                output.len(),
                policy.max_output_bytes
            );
        }
        quality = quality.saturating_sub(5).max(policy.minimum_jpeg_quality);
    }
}

fn claim_path(input_path: &Path) -> Result<PathBuf> {
    let parent = input_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let file_name = input_path
        .file_name()
        .context("待处理图片路径缺少文件名")?
        .to_string_lossy();
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    Ok(parent.join(format!(
        ".{file_name}.processing-{}-{nonce}",
        std::process::id()
    )))
}

fn current_time_millis() -> i64 {
    system_time_millis(SystemTime::now()).unwrap_or(0)
}

fn system_time_millis(value: SystemTime) -> Option<i64> {
    let millis = value.duration_since(UNIX_EPOCH).ok()?.as_millis();
    i64::try_from(millis).ok()
}

struct ClaimedFile {
    path: PathBuf,
}

impl ClaimedFile {
    fn new(path: PathBuf) -> Self {
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for ClaimedFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{ImageBuffer, RgbImage};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_ID: AtomicU64 = AtomicU64::new(0);

    fn test_dir() -> PathBuf {
        let id = TEST_ID.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "rust-client-snapshot-test-{}-{id}",
            std::process::id()
        ));
        fs::create_dir_all(&path).expect("create test directory");
        path
    }

    fn encode_jpeg(image: &DynamicImage, quality: u8) -> Vec<u8> {
        let mut output = Vec::new();
        JpegEncoder::new_with_quality(&mut output, quality)
            .encode_image(image)
            .expect("encode jpeg");
        output
    }

    fn encode_png(image: &DynamicImage) -> Vec<u8> {
        let mut output = Cursor::new(Vec::new());
        image
            .write_to(&mut output, ImageFormat::Png)
            .expect("encode png");
        output.into_inner()
    }

    fn solid_image(width: u32, height: u32, rgb: [u8; 3]) -> DynamicImage {
        DynamicImage::ImageRgb8(ImageBuffer::from_pixel(width, height, Rgb(rgb)))
    }

    #[test]
    fn compliant_jpeg_is_not_reencoded() {
        let source = encode_jpeg(&solid_image(640, 360, [20, 80, 140]), 75);
        let prepared = prepare_snapshot(&source, &SnapshotPolicy::default()).unwrap();

        assert!(prepared.passthrough);
        assert_eq!(prepared.jpeg, source);
        assert_eq!((prepared.width, prepared.height), (640, 360));
    }

    #[test]
    fn png_is_normalized_to_bounded_jpeg() {
        let image = RgbImage::from_fn(1672, 941, |x, y| {
            Rgb([(x % 256) as u8, (y % 256) as u8, ((x + y) % 256) as u8])
        });
        let source = encode_png(&DynamicImage::ImageRgb8(image));
        let policy = SnapshotPolicy::default();
        let prepared = prepare_snapshot(&source, &policy).unwrap();

        assert!(!prepared.passthrough);
        assert!(prepared.jpeg.len() <= policy.max_output_bytes);
        assert_eq!(
            image::load_from_memory(&prepared.jpeg)
                .unwrap()
                .dimensions(),
            (640, 360)
        );
    }

    #[test]
    fn portrait_source_is_letterboxed_without_stretching() {
        let source = encode_png(&solid_image(180, 320, [240, 40, 40]));
        let prepared = prepare_snapshot(&source, &SnapshotPolicy::default()).unwrap();
        let decoded = image::load_from_memory(&prepared.jpeg).unwrap().to_rgb8();

        let corner = decoded.get_pixel(5, 5).0;
        let center = decoded.get_pixel(320, 180).0;
        assert!(corner.iter().all(|value| *value < 12), "{corner:?}");
        assert!(center[0] > 200 && center[1] < 70, "{center:?}");
    }

    #[test]
    fn consume_claims_and_deletes_source() {
        let directory = test_dir();
        let input = directory.join("pic.jpeg");
        fs::write(&input, encode_png(&solid_image(800, 600, [10, 100, 200]))).unwrap();

        let consumed = consume_snapshot(&input, &SnapshotPolicy::default())
            .unwrap()
            .expect("snapshot should exist");

        assert!(!input.exists());
        assert!(!consumed.snapshot.jpeg.is_empty());
        assert!(consumed.captured_at_ms > 0);
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 0);
        fs::remove_dir(&directory).unwrap();
    }

    #[test]
    fn invalid_claimed_input_is_also_deleted() {
        let directory = test_dir();
        let input = directory.join("pic.jpeg");
        fs::write(&input, b"not an image").unwrap();

        assert!(consume_snapshot(&input, &SnapshotPolicy::default()).is_err());
        assert!(!input.exists());
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 0);
        fs::remove_dir(&directory).unwrap();
    }

    #[test]
    fn oversized_claimed_input_is_deleted() {
        let directory = test_dir();
        let input = directory.join("pic.jpeg");
        let policy = SnapshotPolicy {
            max_input_bytes: 8,
            ..Default::default()
        };
        fs::write(&input, vec![0u8; 9]).unwrap();

        let error = consume_snapshot(&input, &policy).unwrap_err();

        assert!(error.to_string().contains("输入图片过大"));
        assert!(!input.exists());
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 0);
        fs::remove_dir(&directory).unwrap();
    }

    #[test]
    fn invalid_snapshot_policy_is_rejected() {
        let bytes = encode_png(&solid_image(10, 10, [1, 2, 3]));
        let invalid_policies = [
            SnapshotPolicy {
                width: 0,
                ..Default::default()
            },
            SnapshotPolicy {
                max_output_bytes: 0,
                ..Default::default()
            },
            SnapshotPolicy {
                preferred_jpeg_quality: 39,
                minimum_jpeg_quality: 40,
                ..Default::default()
            },
            SnapshotPolicy {
                preferred_jpeg_quality: 101,
                ..Default::default()
            },
        ];
        for policy in invalid_policies {
            assert!(prepare_snapshot(&bytes, &policy).is_err());
        }
    }

    #[test]
    fn missing_input_is_not_an_error() {
        let directory = test_dir();
        let result =
            consume_snapshot(&directory.join("pic.jpeg"), &SnapshotPolicy::default()).unwrap();

        assert!(result.is_none());
        fs::remove_dir(&directory).unwrap();
    }

    #[test]
    fn vision_chunks_match_gateway_v1_header() {
        let jpeg = vec![0x5a; VISION_CHUNK_PAYLOAD_BYTES * 2 + 17];
        let chunks = encode_vision_chunks(42, 1_742_134_800_000, &jpeg).unwrap();

        assert_eq!(chunks.len(), 3);
        for (index, chunk) in chunks.iter().enumerate() {
            assert_eq!(&chunk[0..4], b"VIS1");
            assert_eq!(chunk[4], 1);
            assert_eq!(chunk[5], 0);
            assert_eq!(
                u16::from_be_bytes(chunk[6..8].try_into().unwrap()),
                VISION_CHUNK_HEADER_BYTES as u16
            );
            assert_eq!(u64::from_be_bytes(chunk[8..16].try_into().unwrap()), 42);
            assert_eq!(
                u64::from_be_bytes(chunk[16..24].try_into().unwrap()),
                1_742_134_800_000
            );
            assert_eq!(
                u32::from_be_bytes(chunk[24..28].try_into().unwrap()),
                jpeg.len() as u32
            );
            assert_eq!(
                u16::from_be_bytes(chunk[28..30].try_into().unwrap()),
                index as u16
            );
            assert_eq!(u16::from_be_bytes(chunk[30..32].try_into().unwrap()), 3);
            assert!(chunk.len() <= VISION_CHUNK_HEADER_BYTES + VISION_CHUNK_PAYLOAD_BYTES);
        }
        assert_eq!(
            chunks
                .iter()
                .flat_map(|chunk| chunk[VISION_CHUNK_HEADER_BYTES..].iter().copied())
                .collect::<Vec<_>>(),
            jpeg
        );
    }

    #[test]
    fn vision_ack_parser_accepts_only_known_ack_shape() {
        let ack = parse_vision_ack(
            br#"{"type":"vision_snapshot_ack","frame_id":"42","status":"accepted","received_at_ms":1742134800100}"#,
        )
        .unwrap();
        assert_eq!(ack.frame_id.as_deref(), Some("42"));
        assert_eq!(ack.status, "accepted");
        assert!(ack.reason.is_none());

        assert!(parse_vision_ack(
            br#"{"type":"heartbeat_ack","status":"accepted","received_at_ms":1}"#
        )
        .is_err());
        assert!(parse_vision_ack(
            br#"{"type":"vision_snapshot_ack","status":"unknown","received_at_ms":1}"#
        )
        .is_err());
    }

    #[test]
    fn vision_chunks_reject_invalid_boundaries() {
        let valid = vec![0xff; 16];
        assert!(encode_vision_chunks(0, 1, &valid).is_err());
        assert!(encode_vision_chunks(1, -1, &valid).is_err());
        assert!(encode_vision_chunks(1, 1, &[]).is_err());
        assert!(encode_vision_chunks(1, 1, &vec![0xff; DEFAULT_MAX_OUTPUT_BYTES + 1]).is_err());
    }
}
