package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

const (
	ALEX     = "491700000001@s.whatsapp.net"
	ALEX_LID = "99990001@lid"
	BEA      = "491700000002@s.whatsapp.net"
	GROUP    = "12345-678@g.us"
	OWN      = "491700009999"
	T0       = int64(1_700_000_000_000)
	DAY      = int64(86_400_000)

	ownerKey  = "owner-key-0123456789"
	owner2Key = "owner2-key-0123456789"
	readerKey = "reader-key-0123456789"
)

// fakeNames: whatsmeow's store: one LID it can resolve, one contact.
type fakeNames struct{}

func (fakeNames) PNForLID(lid string) string {
	if lid == "99990001" {
		return "491700000001"
	}
	return ""
}

func (fakeNames) Contact(jid string) (string, string, string, string) {
	if jid == BEA {
		return "Bea Example", "Bea", "bea!", ""
	}
	return "", "", "", ""
}

// sendLog: every send any code path made, through the two fakes.
type sendLog struct {
	mu      sync.Mutex
	history []*waE2E.Message
	retries []*types.MessageInfo
}

func (l *sendLog) kinds() (int, int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.history), len(l.retries)
}

var clients = map[string]Client{
	ownerKey:  {Name: "live", Role: "owner"},
	owner2Key: {Name: "second", Role: "owner"},
	readerKey: {Name: "test", Role: "reader"},
}

// setup: a fresh state directory, fake sends and a connected fake account.
func setup(t *testing.T) *sendLog {
	t.Helper()
	dir := t.TempDir()
	lock, err := openState(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { jr.Close(); lock.Close() })
	norm = NewNormalizer(fakeNames{}, emit)
	historyLast = time.Time{}
	status = Status{State: "connected", Since: now()}
	log := &sendLog{}
	isConnected = func() bool { return true }
	ownJID = func() (types.JID, bool) {
		return types.JID{User: OWN, Server: types.DefaultUserServer, Device: 3}.ToNonAD(), true
	}
	historySend = func(_ context.Context, m *waE2E.Message) (string, error) {
		log.mu.Lock()
		log.history = append(log.history, m)
		log.mu.Unlock()
		return "REQ1", nil
	}
	retrySend = func(_ context.Context, info *types.MessageInfo, _ []byte) error {
		log.mu.Lock()
		log.retries = append(log.retries, info)
		log.mu.Unlock()
		return nil
	}
	lookupContact = func(_ context.Context, j types.JID) (Rec, error) {
		return Rec{"jid": j.String(), "push_name": "kai"}, nil
	}
	lookupGroup = func(_ context.Context, j types.JID) (Rec, error) {
		return Rec{"jid": j.String(), "subject": "Project", "participants": []Rec{{"jid": BEA}}}, nil
	}
	download = func(_ context.Context, _ mediaRef, _ []byte) ([]byte, error) {
		return []byte("picture"), nil
	}
	return log
}

// loadFixtures feeds testdata through the handler's two paths, in order: each
// testdata/history/*.json is one HistorySync as protojson, each line of
// testdata/messages/*.jsonl one live message {"info", "message"}. Returns the counts.
func loadFixtures(t *testing.T) (history, live int) {
	t.Helper()
	files, _ := filepath.Glob("testdata/history/*.json")
	sort.Strings(files)
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var data Rec
		if err := json.Unmarshal(b, &data); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		historyChunk(data)
		history++
	}
	days, _ := filepath.Glob("testdata/messages/*.jsonl")
	sort.Strings(days)
	for _, f := range days {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		for _, l := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			var m Rec
			if err := json.Unmarshal([]byte(l), &m); err != nil {
				t.Fatalf("%s: %v", f, err)
			}
			liveLine(m)
			live++
		}
	}
	return history, live
}

// server: all doors on a test server.
func server(t *testing.T) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(routes(clients))
	t.Cleanup(s.Close)
	return s
}

func req(t *testing.T, s *httptest.Server, method, path, key, body string) *http.Response {
	t.Helper()
	r, _ := http.NewRequest(method, s.URL+path, strings.NewReader(body))
	if key != "" {
		r.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

type keyed struct {
	key string
	rt  http.RoundTripper
}

func (k keyed) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	r.Header.Set("Authorization", "Bearer "+k.key)
	return k.rt.RoundTrip(r)
}

// session: an MCP client session with a key.
func session(t *testing.T, s *httptest.Server, key string) *mcp.ClientSession {
	t.Helper()
	c := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil)
	tr := &mcp.StreamableClientTransport{Endpoint: s.URL + "/mcp",
		HTTPClient: &http.Client{Transport: keyed{key, http.DefaultTransport}}}
	cs, err := c.Connect(context.Background(), tr, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cs.Close() })
	return cs
}

func call(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any) *mcp.CallToolResult {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	return res
}

func text(res *mcp.CallToolResult) string {
	var b strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			b.WriteString(tc.Text)
		}
	}
	return b.String()
}

// journalRecs: every journaled record, seq included.
func journalRecs(t *testing.T) []Rec {
	t.Helper()
	rows, err := jr.After(0, 100000)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]Rec, 0, len(rows))
	for _, r := range rows {
		out = append(out, r.Rec())
	}
	return out
}

func find(recs []Rec, kind, key string) Rec {
	for i := len(recs) - 1; i >= 0; i-- {
		r := recs[i]
		if r["kind"] != kind {
			continue
		}
		switch kind {
		case "message":
			if str(obj(r, "message"), "key") == key {
				return r
			}
		case "person":
			if str(obj(r, "person"), "key") == key {
				return r
			}
		default:
			if str(r, "message_key") == key || str(r, "chat_key") == key {
				return r
			}
		}
	}
	return nil
}
