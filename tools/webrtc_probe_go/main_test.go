package main

import (
	"testing"

	"github.com/pion/webrtc/v4"
)

func TestParseICEServersFromCSV(t *testing.T) {
	servers, err := parseICEServers("stun:one.example:3478, turn:two.example:3478?transport=udp")
	if err != nil {
		t.Fatal(err)
	}
	if len(servers) != 2 {
		t.Fatalf("len(servers) = %d, want 2", len(servers))
	}
	if servers[0].URLs[0] != "stun:one.example:3478" {
		t.Fatalf("first URL = %q", servers[0].URLs[0])
	}
	if servers[1].URLs[0] != "turn:two.example:3478?transport=udp" {
		t.Fatalf("second URL = %q", servers[1].URLs[0])
	}
}

func TestParseICEServersFromJSONArray(t *testing.T) {
	raw := `[
		"stun:stun.example.com:3478",
		{
			"urls": [
				"turn:turn.example.com:3478?transport=udp",
				"turns:turn.example.com:5349?transport=tcp"
			],
			"username": "short_user",
			"credential": "short_secret"
		}
	]`

	servers, err := parseICEServers(raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(servers) != 2 {
		t.Fatalf("len(servers) = %d, want 2", len(servers))
	}
	if servers[0].URLs[0] != "stun:stun.example.com:3478" {
		t.Fatalf("stun URL = %q", servers[0].URLs[0])
	}
	if got := len(servers[1].URLs); got != 2 {
		t.Fatalf("turn URL count = %d, want 2", got)
	}
	if servers[1].Username != "short_user" {
		t.Fatalf("username = %q", servers[1].Username)
	}
	if servers[1].Credential != "short_secret" {
		t.Fatalf("credential = %v", servers[1].Credential)
	}
}

func TestParseICEServersFromJSONObject(t *testing.T) {
	servers, err := parseICEServers(`{"urls":"stun:one.example:3478"}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(servers) != 1 {
		t.Fatalf("len(servers) = %d, want 1", len(servers))
	}
	if servers[0].URLs[0] != "stun:one.example:3478" {
		t.Fatalf("URL = %q", servers[0].URLs[0])
	}
	if servers[0].Username != "" {
		t.Fatalf("username = %q, want empty", servers[0].Username)
	}
}

func TestParseICETransportPolicy(t *testing.T) {
	if _, err := parseICETransportPolicy("all"); err != nil {
		t.Fatal(err)
	}
	if _, err := parseICETransportPolicy("relay"); err != nil {
		t.Fatal(err)
	}
	if _, err := parseICETransportPolicy("bad"); err == nil {
		t.Fatal("expected unsupported policy error")
	}
}

func TestParseOptionalICETransportPolicyFallsBack(t *testing.T) {
	policy, err := parseOptionalICETransportPolicy("", mustPolicy(t, "relay"))
	if err != nil {
		t.Fatal(err)
	}
	if policy != mustPolicy(t, "relay") {
		t.Fatalf("policy = %s, want relay", policy.String())
	}

	policy, err = parseOptionalICETransportPolicy("all", mustPolicy(t, "relay"))
	if err != nil {
		t.Fatal(err)
	}
	if policy != mustPolicy(t, "all") {
		t.Fatalf("policy = %s, want all", policy.String())
	}
}

func mustPolicy(t *testing.T, raw string) webrtc.ICETransportPolicy {
	t.Helper()
	policy, err := parseICETransportPolicy(raw)
	if err != nil {
		t.Fatal(err)
	}
	return policy
}
