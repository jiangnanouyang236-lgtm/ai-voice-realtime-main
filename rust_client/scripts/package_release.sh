#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <binary-path> <output-dir>" >&2
  exit 2
fi

BIN_SRC="$1"
OUT_DIR="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ ! -f "$BIN_SRC" ]]; then
  echo "binary not found: $BIN_SRC" >&2
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

cp "$BIN_SRC" "$OUT_DIR/rust_client"
chmod +x "$OUT_DIR/rust_client"

DING_SRC="$ROOT_DIR/assets/wake_audio/ding.wav"
if [[ -f "$DING_SRC" ]]; then
  mkdir -p "$OUT_DIR/assets/wake_audio"
  cp "$DING_SRC" "$OUT_DIR/assets/wake_audio/ding.wav"
else
  echo "warning: ding sound not found: $DING_SRC" >&2
fi

cat > "$OUT_DIR/run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

: "${ROBOT_ID:?ROBOT_ID is required}"

export GATEWAY_URL="${GATEWAY_URL:-ws://127.0.0.1:8282/ws}"
export TRANSPORT_POLICY="${TRANSPORT_POLICY:-webrtc_only}"
export WEBRTC_ENABLED="${WEBRTC_ENABLED:-true}"
export TRANSPORT_FALLBACK_ENABLED="${TRANSPORT_FALLBACK_ENABLED:-false}"
export WEBRTC_OFFER_FACTORY="${WEBRTC_OFFER_FACTORY:-native}"
export RTC_AUDIO_UPLINK="${RTC_AUDIO_UPLINK:-rtp_only}"
export WEBRTC_NATIVE_RTP_PROBE_ENABLED="${WEBRTC_NATIVE_RTP_PROBE_ENABLED:-false}"
export WEBRTC_CONNECT_TIMEOUT_MS="${WEBRTC_CONNECT_TIMEOUT_MS:-15000}"
export ASR_DING_SOUND_PATH="${ASR_DING_SOUND_PATH:-$DIR/assets/wake_audio/ding.wav}"
export OPUS_BITRATE_BPS="${OPUS_BITRATE_BPS:-16000}"
export TTS_PREBUFFER_SEC="${TTS_PREBUFFER_SEC:-0.10}"

exec "$DIR/rust_client" "$@"
EOF
chmod +x "$OUT_DIR/run.sh"

ARCHIVE="${OUT_DIR%/}.tar.gz"
rm -f "$ARCHIVE"
tar -C "$(dirname "$OUT_DIR")" -czf "$ARCHIVE" "$(basename "$OUT_DIR")"

echo "package directory: $OUT_DIR"
echo "package archive:   $ARCHIVE"
