package main

import (
	"encoding/binary"
	"net"
	"testing"
)

func TestParseXORMappedIPv4Response(t *testing.T) {
	txID := [12]byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}
	mappedIP := []byte{203, 0, 113, 10}
	mappedPort := uint16(54321)

	attrValue := []byte{0, 0x01, 0, 0}
	binary.BigEndian.PutUint16(attrValue[2:4], mappedPort^uint16(magicCookie>>16))
	cookie := make([]byte, 4)
	binary.BigEndian.PutUint32(cookie, magicCookie)
	for idx, b := range mappedIP {
		attrValue = append(attrValue, b^cookie[idx])
	}

	packet := make([]byte, 0, 32)
	packet = appendUint16(packet, bindingSuccessResponse)
	packet = appendUint16(packet, 12)
	packet = appendUint32(packet, magicCookie)
	packet = append(packet, txID[:]...)
	packet = appendUint16(packet, attrXorMappedAddress)
	packet = appendUint16(packet, uint16(len(attrValue)))
	packet = append(packet, attrValue...)

	response, err := parseBindingResponse(packet, txID)
	if err != nil {
		t.Fatal(err)
	}
	want := &net.UDPAddr{IP: net.IPv4(203, 0, 113, 10), Port: int(mappedPort)}
	if response.xorMappedAddr.String() != want.String() {
		t.Fatalf("mapped addr = %s, want %s", response.xorMappedAddr, want)
	}
}

func appendUint16(packet []byte, value uint16) []byte {
	var buf [2]byte
	binary.BigEndian.PutUint16(buf[:], value)
	return append(packet, buf[:]...)
}

func appendUint32(packet []byte, value uint32) []byte {
	var buf [4]byte
	binary.BigEndian.PutUint32(buf[:], value)
	return append(packet, buf[:]...)
}
