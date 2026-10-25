package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.mau.fi/whatsmeow/proto/waE2E"
)

var errFake = errors.New("fake")

func structured(t *testing.T, res *mcp.CallToolResult) map[string]any {
	t.Helper()
	b, _ := json.Marshal(res.StructuredContent)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	return m
}

func keysOf(recs []any) []string {
	var out []string
	for _, r := range recs {
		m := r.(map[string]any)["message"].(map[string]any)
		out = append(out, m["key"].(string))
	}
	return out
}

func TestToolsAnnotations(t *testing.T) {
	setup(t)
	cs := session(t, server(t), readerKey)
	res, err := cs.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, tool := range res.Tools {
		seen[tool.Name] = true
		a := tool.Annotations
		if a == nil {
			t.Fatalf("%s has no annotations", tool.Name)
		}
		if !a.ReadOnlyHint || !a.IdempotentHint || a.OpenWorldHint == nil {
			t.Fatalf("%s annotations %+v", tool.Name, a)
		}
		if strings.ContainsAny(tool.Description, "\u2014\u2013") {
			t.Fatalf("%s: a dash in the description", tool.Name)
		}
	}
	want := []string{"list_chats", "read_messages", "read_around_message", "request_older_messages", "get_contact", "get_group"}
	for _, n := range want {
		if !seen[n] {
			t.Fatalf("missing %s", n)
		}
	}
	if len(seen) != len(want) {
		t.Fatalf("tools %v: every tool only reads", seen)
	}
}

func TestReadAroundAndChatsFromTheJournal(t *testing.T) {
	setup(t)
	loadFixtures(t)
	cs := session(t, server(t), readerKey)

	st := structured(t, call(t, cs, "list_chats", nil))
	chats := st["chats"].([]any)
	if len(chats) != 2 || chats[0].(map[string]any)["chat"] != ALEX ||
		chats[1].(map[string]any)["name"] != "Chess club" {
		t.Fatalf("chats %v", chats)
	}
	st = structured(t, call(t, cs, "list_chats", map[string]any{"query": "chess"}))
	if len(st["chats"].([]any)) != 1 {
		t.Fatal("query")
	}

	// Alex's chat, oldest first: A2, V1, Q1, A5, L1, L2 (A2 edited)
	res := call(t, cs, "read_messages", map[string]any{"chat": ALEX_LID, "limit": 3})
	st = structured(t, res)
	if got := strings.Join(keysOf(st["records"].([]any)), ","); got != "A5,L1,L2" {
		t.Fatalf("newest three: %s", got)
	}
	if st["earlier"].(map[string]any)["before"] != "A5" || st["later"] != nil ||
		st["where"] == "" || st["note"] == "" {
		t.Fatalf("handles %v", st)
	}
	st = structured(t, call(t, cs, "read_messages", map[string]any{"chat": ALEX, "before": "A5", "limit": 2}))
	if got := strings.Join(keysOf(st["records"].([]any)), ","); got != "V1,Q1" {
		t.Fatalf("before: %s", got)
	}
	if st["later"].(map[string]any)["after"] != "Q1" {
		t.Fatal("later handle")
	}
	st = structured(t, call(t, cs, "read_messages", map[string]any{"chat": ALEX, "after": "A2", "limit": 1}))
	if got := strings.Join(keysOf(st["records"].([]any)), ","); got != "V1" {
		t.Fatalf("after: %s", got)
	}
	res = call(t, cs, "read_messages", map[string]any{"chat": ALEX, "limit": 200})
	if !strings.Contains(text(res), "A2 ") || !strings.Contains(text(res), "no way!! (edited)") {
		t.Fatalf("edit not applied: %s", text(res))
	}
	for _, r := range structured(t, res)["records"].([]any) {
		if r.(map[string]any)["seq"] == nil {
			t.Fatal("a record without its seq")
		}
	}
	st = structured(t, call(t, cs, "read_around_message", map[string]any{"chat": ALEX, "message": "V1", "limit": 2}))
	if got := strings.Join(keysOf(st["records"].([]any)), ","); got != "A2,V1,Q1" {
		t.Fatalf("around: %s", got)
	}
	if !call(t, cs, "read_around_message", map[string]any{"chat": ALEX, "message": "nope"}).IsError {
		t.Fatal("unknown message")
	}
	if !call(t, cs, "read_messages", map[string]any{"chat": "4999@s.whatsapp.net"}).IsError {
		t.Fatal("unknown chat")
	}
}

func TestOlderWaitsForThePhonesAnswer(t *testing.T) {
	log := setup(t)
	loadFixtures(t)
	historySend = func(_ context.Context, m *waE2E.Message) (string, error) {
		log.history = append(log.history, m)
		go func() {
			time.Sleep(20 * time.Millisecond)
			chats := historyChunk(Rec{"syncType": "ON_DEMAND", "conversations": []any{Rec{
				"ID": ALEX, "messages": []any{Rec{"message": Rec{
					"key": Rec{"ID": "OLD1", "fromMe": false}, "messageTimestamp": "1600000000",
					"message": Rec{"conversation": "long ago"}}}}}}})
			answered(chats)
		}()
		return "R", nil
	}
	cs := session(t, server(t), readerKey)
	res := call(t, cs, "request_older_messages", map[string]any{"chat": ALEX})
	if res.IsError || strings.Join(keysOf(structured(t, res)["records"].([]any)), ",") != "OLD1" {
		t.Fatalf("older: %s", text(res))
	}
	historySend = func(context.Context, *waE2E.Message) (string, error) { return "R", nil }
	historyLast = time.Time{}
	olderWait = 50 * time.Millisecond
	defer func() { olderWait = 120 * time.Second }()
	if res := call(t, cs, "request_older_messages", map[string]any{"chat": ALEX}); !res.IsError ||
		!strings.Contains(text(res), "did not answer") {
		t.Fatalf("silent phone: %s", text(res))
	}
}

// TestNoCodePathWritesAnywhereElse drives every tool and door and checks that the only
// sends are the two allowed kinds, each to the own account: a request for older
// messages and a media retry receipt.
func TestNoCodePathWritesAnywhereElse(t *testing.T) {
	log := setup(t)
	loadFixtures(t)
	s := server(t)
	for _, key := range []string{readerKey, ownerKey} {
		cs := session(t, s, key)
		call(t, cs, "list_chats", nil)
		call(t, cs, "read_messages", map[string]any{"chat": BEA})
		call(t, cs, "read_messages", map[string]any{"chat": GROUP})
		call(t, cs, "read_around_message", map[string]any{"chat": GROUP, "message": "G1"})
		call(t, cs, "get_contact", map[string]any{"phone": "+49 170 0000002"})
		call(t, cs, "get_group", map[string]any{"jid": GROUP})
		historyLast = time.Time{}
		olderWait = 10 * time.Millisecond
		call(t, cs, "request_older_messages", map[string]any{"chat": BEA + " " + GROUP})
		historyLast = time.Time{}
		call(t, cs, "request_older_messages", map[string]any{"chat": GROUP})
		olderWait = 120 * time.Second
		for _, d := range [][3]string{{"GET", "/admin/status", ""}, {"GET", "/admin/qr", ""},
			{"GET", "/admin/media/retry", ""}, {"POST", "/admin/media/download", `{"direct_path":"/v/x"}`}} {
			req(t, s, d[0], d[1], key, d[2])
		}
		historyLast = time.Time{}
		req(t, s, "POST", "/admin/older", key, `{"chat":"`+GROUP+`"}`)
	}
	for _, m := range log.history {
		if m.GetProtocolMessage().GetType() != waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE {
			t.Fatal("a history send that is not a request to the own phone")
		}
	}
	if len(log.history) == 0 {
		t.Fatal("the older paths were not driven")
	}
}
