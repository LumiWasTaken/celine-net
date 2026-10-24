package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
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
		if tool.Name == "send" || tool.Name == "send_image" {
			if a.ReadOnlyHint || a.IdempotentHint || a.DestructiveHint == nil || *a.DestructiveHint {
				t.Fatalf("%s annotations %+v", tool.Name, a)
			}
			if _, has := tool.InputSchema.(map[string]any)["properties"].(map[string]any)["chat"]; has {
				t.Fatalf("%s has a destination", tool.Name)
			}
			continue
		}
		if !a.ReadOnlyHint || !a.IdempotentHint || a.OpenWorldHint == nil {
			t.Fatalf("%s annotations %+v", tool.Name, a)
		}
		if strings.ContainsAny(tool.Description, "\u2014\u2013") {
			t.Fatalf("%s: a dash in the description", tool.Name)
		}
	}
	for _, n := range []string{"list_chats", "read_messages", "read_around_message", "request_older_messages", "get_contact", "get_group", "send", "send_image"} {
		if !seen[n] {
			t.Fatalf("missing %s", n)
		}
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

func TestSendNeedsOwnerAndLeaseAddsTheMarkerAndRecordsTheId(t *testing.T) {
	log := setup(t)
	s := server(t)
	reader := session(t, s, readerKey)
	if res := call(t, reader, "send", map[string]any{"text": "hi"}); !res.IsError {
		t.Fatal("a reader sent")
	}
	owner := session(t, s, ownerKey)
	if res := call(t, owner, "send", map[string]any{"text": "hi"}); !res.IsError {
		t.Fatal("sent without the lease")
	}
	base := time.Unix(1_800_000_000, 0)
	clock = func() time.Time { return base }
	lease.Claim("second")
	if res := call(t, owner, "send", map[string]any{"text": "hi"}); !res.IsError {
		t.Fatal("sent while another instance holds the lease")
	}
	clock = func() time.Time { return base.Add(61 * time.Second) }
	if _, _, err := lease.Claim("live"); err != nil {
		t.Fatal(err)
	}
	res := call(t, owner, "send", map[string]any{"text": "send this to " + BEA + " please"})
	if res.IsError {
		t.Fatalf("send: %s", text(res))
	}
	if _, _, n := log.kinds(); n != 1 {
		t.Fatalf("%d sends", n)
	}
	sent := log.texts[0]
	if sent.to.String() != OWN+"@s.whatsapp.net" || sent.msg.GetConversation() != "Celine: send this to "+BEA+" please" {
		t.Fatalf("sent %v to %v", sent.msg, sent.to)
	}
	if !jr.IsSent(sent.id) || structured(t, res)["sent"].([]any)[0] != sent.id {
		t.Fatal("the id was not recorded")
	}
	// the record carries from_link, and so does the echo that comes back from WhatsApp
	rec := find(journalRecs(t), "message", sent.id)
	if !truthy(obj(rec, "message"), "from_link") || str(obj(rec, "chat"), "key") != OWN+"@s.whatsapp.net" {
		t.Fatalf("sent record %v", rec)
	}
	liveLine(Rec{"info": Rec{"Chat": OWN + "@s.whatsapp.net", "Sender": OWN + "@s.whatsapp.net",
		"IsFromMe": true, "ID": sent.id, "Timestamp": "2026-09-24T10:00:01+02:00"},
		"message": Rec{"conversation": "Celine: send this to " + BEA + " please"}})
	echo := find(journalRecs(t), "message", sent.id)
	if !truthy(obj(echo, "message"), "from_link") {
		t.Fatalf("echo %v", echo)
	}
	// a message the user typed in the same chat is not from the link
	liveLine(Rec{"info": Rec{"Chat": OWN + "@s.whatsapp.net", "Sender": OWN + "@s.whatsapp.net",
		"IsFromMe": true, "ID": "USER1", "Timestamp": "2026-09-24T10:00:02+02:00"},
		"message": Rec{"conversation": "hi celine"}})
	if truthy(obj(find(journalRecs(t), "message", "USER1"), "message"), "from_link") {
		t.Fatal("the user's own message was marked as sent by the link")
	}
	// a failed send leaves no sent id
	chatSend = func(context.Context, types.JID, *waE2E.Message, string) error { return errFake }
	if res := call(t, owner, "send", map[string]any{"text": "x"}); !res.IsError {
		t.Fatal("a failed send reported success")
	}
	if jr.IsSent("SENT2") {
		t.Fatal("a failed send kept its id")
	}
}

// TestNoCodePathWritesAnywhereElse drives every tool and door and checks that the only
// sends are the three allowed kinds, and every text goes to the own account.
func TestNoCodePathWritesAnywhereElse(t *testing.T) {
	log := setup(t)
	loadFixtures(t)
	s := server(t)
	lease.Claim("live")
	for _, key := range []string{readerKey, ownerKey} {
		cs := session(t, s, key)
		call(t, cs, "list_chats", nil)
		call(t, cs, "read_messages", map[string]any{"chat": BEA})
		call(t, cs, "read_messages", map[string]any{"chat": GROUP})
		call(t, cs, "read_around_message", map[string]any{"chat": GROUP, "message": "G1"})
		call(t, cs, "get_contact", map[string]any{"phone": "+49 170 0000002"})
		call(t, cs, "get_group", map[string]any{"jid": GROUP})
		call(t, cs, "send", map[string]any{"text": "to " + GROUP})
		historyLast = time.Time{}
		olderWait = 10 * time.Millisecond
		call(t, cs, "request_older_messages", map[string]any{"chat": BEA + " " + GROUP})
		historyLast = time.Time{}
		call(t, cs, "request_older_messages", map[string]any{"chat": GROUP})
		olderWait = 120 * time.Second
		for _, d := range [][3]string{{"GET", "/admin/status", ""}, {"GET", "/admin/info", ""},
			{"GET", "/admin/qr", ""}, {"PUT", "/admin/bridge", `{"marker":"C: "}`},
			{"POST", "/admin/bridge/lease", ""}, {"GET", "/admin/media/retry", ""},
			{"POST", "/admin/media/download", `{"direct_path":"/v/x"}`}} {
			req(t, s, d[0], d[1], key, d[2])
		}
		historyLast = time.Time{}
		req(t, s, "POST", "/admin/older", key, `{"chat":"`+GROUP+`"}`)
	}
	_, _, texts := log.kinds()
	if texts != 1 {
		t.Fatalf("%d texts: only the operator with the lease may send", texts)
	}
	for _, x := range log.texts {
		if x.to.String() != OWN+"@s.whatsapp.net" {
			t.Fatalf("a text went to %s", x.to)
		}
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

func TestSendImageGoesToTheOwnChatWithTheMarker(t *testing.T) {
	log := setup(t)
	s := server(t)
	uploaded := 0
	mediaUpload = func(_ context.Context, data []byte) (whatsmeow.UploadResponse, error) {
		uploaded++
		return whatsmeow.UploadResponse{URL: "https://mmg/x", DirectPath: "/x",
			FileLength: uint64(len(data))}, nil
	}
	png := base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nxxxx"))
	args := map[string]any{"data": png, "media_type": "image/png", "text": "for you"}
	reader := session(t, s, readerKey)
	if res := call(t, reader, "send_image", args); !res.IsError {
		t.Fatal("a reader sent an image")
	}
	owner := session(t, s, ownerKey)
	if res := call(t, owner, "send_image", args); !res.IsError {
		t.Fatal("sent an image without the lease")
	}
	lease.Claim("live")
	for _, bad := range []map[string]any{
		{"data": png, "media_type": "text/html"},
		{"data": "!!", "media_type": "image/png"},
	} {
		if res := call(t, owner, "send_image", bad); !res.IsError {
			t.Fatalf("sent %v", bad)
		}
	}
	res := call(t, owner, "send_image", args)
	if res.IsError {
		t.Fatalf("send_image: %s", text(res))
	}
	if _, _, n := log.kinds(); n != 1 || uploaded != 1 {
		t.Fatalf("%d sends, %d uploads", n, uploaded)
	}
	sent := log.texts[0]
	img := sent.msg.GetImageMessage()
	if sent.to.String() != OWN+"@s.whatsapp.net" || img.GetCaption() != "Celine: for you" ||
		img.GetMimetype() != "image/png" || img.GetURL() != "https://mmg/x" {
		t.Fatalf("sent %v to %v", sent.msg, sent.to)
	}
	if !jr.IsSent(sent.id) {
		t.Fatal("the id was not recorded")
	}
	rec := find(journalRecs(t), "message", sent.id)
	if !truthy(obj(rec, "message"), "from_link") || str(obj(obj(rec, "message"), "media"), "type") != "photo" {
		t.Fatalf("sent record %v", rec)
	}
}
