package main

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	magicCookie            uint32 = 0x2112A442
	bindingRequest         uint16 = 0x0001
	bindingSuccessResponse uint16 = 0x0101
	attrMappedAddress      uint16 = 0x0001
	attrXorMappedAddress   uint16 = 0x0020
)

type args struct {
	server  string
	bind    string
	timeout time.Duration
	count   int
}

type bindingResponse struct {
	mappedAddr    *net.UDPAddr
	xorMappedAddr *net.UDPAddr
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "verdict=fail error=%v\n", err)
		os.Exit(1)
	}
}

func run() error {
	cfg := parseArgs()
	serverAddr, err := net.ResolveUDPAddr("udp", cfg.server)
	if err != nil {
		return err
	}
	bindAddr, err := net.ResolveUDPAddr("udp", cfg.bind)
	if err != nil {
		return err
	}

	fmt.Printf(
		"stun_probe runtime=go server=%s resolved=%s bind=%s timeout_ms=%d count=%d\n",
		cfg.server,
		serverAddr.String(),
		cfg.bind,
		cfg.timeout.Milliseconds(),
		cfg.count,
	)

	ok := 0
	for attempt := 1; attempt <= cfg.count; attempt++ {
		if err := probeOnce(cfg, bindAddr, serverAddr, attempt); err != nil {
			fmt.Printf("attempt=%d verdict=fail error=%v\n", attempt, err)
			continue
		}
		ok++
	}
	if ok == 0 {
		return errors.New("all STUN binding attempts failed")
	}
	fmt.Printf("summary verdict=ok success=%d/%d\n", ok, cfg.count)
	return nil
}

func parseArgs() args {
	server := flag.String("server", envString("STUN_SERVER", "stun.l.google.com:19302"), "STUN server host:port")
	bind := flag.String("bind", envString("STUN_BIND", "0.0.0.0:0"), "local UDP bind address ip:port")
	bindIP := flag.String("bind-ip", "", "local UDP bind IP; shorthand for ip:0")
	timeoutMS := flag.Int("timeout-ms", envInt("STUN_TIMEOUT_MS", 3000), "UDP timeout in milliseconds")
	count := flag.Int("count", envInt("STUN_PROBE_COUNT", 3), "number of attempts")
	flag.Parse()

	bindValue := *bind
	if strings.TrimSpace(*bindIP) != "" {
		bindValue = bindIPToSocket(strings.TrimSpace(*bindIP))
	}

	if *timeoutMS < 1 {
		*timeoutMS = 1
	}
	if *count < 1 {
		*count = 1
	}

	return args{
		server:  strings.TrimSpace(*server),
		bind:    bindValue,
		timeout: time.Duration(*timeoutMS) * time.Millisecond,
		count:   *count,
	}
}

func bindIPToSocket(value string) string {
	ip := net.ParseIP(value)
	if ip != nil && ip.To4() == nil {
		return "[" + value + "]:0"
	}
	return value + ":0"
}

func envString(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func probeOnce(cfg args, bindAddr, serverAddr *net.UDPAddr, attempt int) error {
	conn, err := net.ListenUDP("udp", bindAddr)
	if err != nil {
		return err
	}
	defer conn.Close()

	txID, err := makeTransactionID()
	if err != nil {
		return err
	}
	request := buildBindingRequest(txID)
	started := time.Now()
	if _, err := conn.WriteToUDP(request, serverAddr); err != nil {
		return err
	}
	if err := conn.SetReadDeadline(time.Now().Add(cfg.timeout)); err != nil {
		return err
	}

	buf := make([]byte, 1500)
	n, from, err := conn.ReadFromUDP(buf)
	if err != nil {
		return err
	}
	response, err := parseBindingResponse(buf[:n], txID)
	if err != nil {
		return err
	}
	mapped := response.xorMappedAddr
	if mapped == nil {
		mapped = response.mappedAddr
	}
	if mapped == nil {
		return errors.New("STUN response did not include mapped address")
	}

	fmt.Printf(
		"attempt=%d verdict=ok local=%s from=%s mapped=%s rtt_ms=%.1f\n",
		attempt,
		conn.LocalAddr().String(),
		from.String(),
		mapped.String(),
		float64(time.Since(started).Microseconds())/1000.0,
	)
	return nil
}

func buildBindingRequest(txID [12]byte) []byte {
	packet := make([]byte, 20)
	binary.BigEndian.PutUint16(packet[0:2], bindingRequest)
	binary.BigEndian.PutUint16(packet[2:4], 0)
	binary.BigEndian.PutUint32(packet[4:8], magicCookie)
	copy(packet[8:20], txID[:])
	return packet
}

func makeTransactionID() ([12]byte, error) {
	var txID [12]byte
	_, err := rand.Read(txID[:])
	return txID, err
}

func parseBindingResponse(packet []byte, expectedTxID [12]byte) (bindingResponse, error) {
	if len(packet) < 20 {
		return bindingResponse{}, errors.New("STUN packet too short")
	}
	msgType := binary.BigEndian.Uint16(packet[0:2])
	if msgType != bindingSuccessResponse {
		return bindingResponse{}, fmt.Errorf("unexpected STUN message type: 0x%04x", msgType)
	}
	msgLen := int(binary.BigEndian.Uint16(packet[2:4]))
	if len(packet) < 20+msgLen {
		return bindingResponse{}, errors.New("STUN packet truncated")
	}
	if binary.BigEndian.Uint32(packet[4:8]) != magicCookie {
		return bindingResponse{}, errors.New("STUN magic cookie mismatch")
	}
	if !bytes.Equal(packet[8:20], expectedTxID[:]) {
		return bindingResponse{}, errors.New("STUN transaction id mismatch")
	}

	var response bindingResponse
	offset := 20
	end := 20 + msgLen
	for offset+4 <= end {
		attrType := binary.BigEndian.Uint16(packet[offset : offset+2])
		attrLen := int(binary.BigEndian.Uint16(packet[offset+2 : offset+4]))
		offset += 4
		if offset+attrLen > end {
			return bindingResponse{}, errors.New("STUN attribute truncated")
		}
		value := packet[offset : offset+attrLen]
		switch attrType {
		case attrMappedAddress:
			if addr, err := parseAddress(value); err == nil {
				response.mappedAddr = addr
			}
		case attrXorMappedAddress:
			if addr, err := parseXorAddress(value, expectedTxID); err == nil {
				response.xorMappedAddr = addr
			}
		}
		offset += (attrLen + 3) &^ 3
	}
	return response, nil
}

func parseAddress(value []byte) (*net.UDPAddr, error) {
	if len(value) < 4 || value[0] != 0 {
		return nil, errors.New("invalid MAPPED-ADDRESS attribute")
	}
	port := int(binary.BigEndian.Uint16(value[2:4]))
	return parseAddressBody(value[1], port, value[4:])
}

func parseXorAddress(value []byte, txID [12]byte) (*net.UDPAddr, error) {
	if len(value) < 4 || value[0] != 0 {
		return nil, errors.New("invalid XOR-MAPPED-ADDRESS attribute")
	}
	port := int(binary.BigEndian.Uint16(value[2:4]) ^ uint16(magicCookie>>16))
	body := append([]byte(nil), value[4:]...)
	cookie := make([]byte, 4)
	binary.BigEndian.PutUint32(cookie, magicCookie)
	for idx := range body {
		var mask byte
		if idx < 4 {
			mask = cookie[idx]
		} else {
			mask = txID[idx-4]
		}
		body[idx] ^= mask
	}
	return parseAddressBody(value[1], port, body)
}

func parseAddressBody(family byte, port int, body []byte) (*net.UDPAddr, error) {
	switch family {
	case 0x01:
		if len(body) < 4 {
			return nil, errors.New("short IPv4 address")
		}
		return &net.UDPAddr{IP: net.IPv4(body[0], body[1], body[2], body[3]), Port: port}, nil
	case 0x02:
		if len(body) < 16 {
			return nil, errors.New("short IPv6 address")
		}
		return &net.UDPAddr{IP: net.IP(append([]byte(nil), body[:16]...)), Port: port}, nil
	default:
		return nil, fmt.Errorf("unsupported STUN address family: %d", family)
	}
}
