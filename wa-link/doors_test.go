package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

func decode(t *testing.T, r *http.Response) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestClientsParse(t *testing.T) {
	c, err := parseClients("live:" + ownerKey + ":owner, test:" + readerKey + ":reader\n")
	if err != nil || c[ownerKey].Name != "live" || c[readerKey].Role != "reader" {
		t.Fatal(c, err)
	}
	for _, bad := range []string{"", "a:b:owner", "a:" + ownerKey + ":admin", "nokey"} {
		if _, err := parseClients(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestEveryDoorNeedsAKeyAndOwnerDoorsNeedAnOwner(t *testing.T) {
	setup(t)
	s := server(t)
	for _, p := range []string{"/feed", "/admin/status", "/admin/info", "/admin/qr", "/mcp"} {
		if got := req(t, s, "GET", p, "", "").StatusCode; got != 401 {
			t.Errorf("%s without key: %d", p, got)
		}
		if got := req(t, s, "GET", p, "wrong-key-0123456789", "").StatusCode; got != 401 {
			t.Errorf("%s with a wrong key: %d", p, got)
		}
	}
	if r := req(t, s, "PUT", "/admin/bridge", readerKey, `{"marker":"X: "}`); r.StatusCode != 403 {
		t.Fatalf("reader changed the marker: %d", r.StatusCode)
	}
	if r := req(t, s, "POST", "/admin/bridge/lease", readerKey, ""); r.StatusCode != 403 {
		t.Fatalf("reader took the lease: %d", r.StatusCode)
	}
	if r := req(t, s, "GET", "/admin/status", readerKey, ""); r.StatusCode != 200 {
		t.Fatalf("reader can't read status: %d", r.StatusCode)
	}
}

func TestStatusInfoMarkerAndLease(t *testing.T) {
	setup(t)
	s := server(t)
	st := decode(t, req(t, s, "GET", "/admin/status", readerKey, ""))
	conn := st["connection"].(map[string]any)
	if conn["state"] != "connected" || conn["error"] != nil ||
		st["account"].(map[string]any)["id"] != OWN+"@s.whatsapp.net" {
		t.Fatalf("status %v", st)
	}
	update(func(s *Status) { s.State = "pairing"; s.QR = "2@abc"; s.QRAt = 5 })
	st = decode(t, req(t, s, "GET", "/admin/status", readerKey, ""))
	if st["connection"].(map[string]any)["state"] != "needs_login" || st["qr_at"] != float64(5) {
		t.Fatalf("pairing %v", st)
	}
	if r := req(t, s, "GET", "/admin/qr", readerKey, ""); r.StatusCode != 200 ||
		r.Header.Get("Content-Type") != "image/png" {
		t.Fatalf("qr %d", r.StatusCode)
	}
	update(func(s *Status) { s.State = "disconnected"; s.QR = ""; s.Error = "The connection to WhatsApp dropped." })
	conn = decode(t, req(t, s, "GET", "/admin/status", readerKey, ""))["connection"].(map[string]any)
	if conn["state"] != "unreachable" || conn["error"] != "The connection to WhatsApp dropped." {
		t.Fatalf("unreachable %v", conn)
	}
	if r := req(t, s, "GET", "/admin/qr", readerKey, ""); r.StatusCode != 404 {
		t.Fatalf("qr without a code: %d", r.StatusCode)
	}

	info := decode(t, req(t, s, "GET", "/admin/info", readerKey, ""))
	if info["write_chat"] != OWN+"@s.whatsapp.net" || info["marker"] != "Celine: " ||
		info["write_chat_name"] != "Message yourself" || info["part_chars"] != float64(partChars) {
		t.Fatalf("info %v", info)
	}
	for _, bad := range []string{`{"marker":""}`, `{"marker":"   "}`, `{"marker":"` + strings.Repeat("x", 41) + `"}`, `{}`} {
		if r := req(t, s, "PUT", "/admin/bridge", ownerKey, bad); r.StatusCode != 400 {
			t.Errorf("%s: %d", bad, r.StatusCode)
		}
	}
	info = decode(t, req(t, s, "PUT", "/admin/bridge", ownerKey, `{"marker":"C: "}`))
	if info["marker"] != "C: " || marker() != "C: " {
		t.Fatalf("marker %v", info)
	}

	base := time.Unix(1_800_000_000, 0)
	clock = func() time.Time { return base }
	if r := req(t, s, "POST", "/admin/bridge/lease", ownerKey, ""); r.StatusCode != 200 {
		t.Fatalf("claim %d", r.StatusCode)
	}
	r := req(t, s, "POST", "/admin/bridge/lease", owner2Key, "")
	if r.StatusCode != 409 || decode(t, r)["error"] != "Another instance holds the bridge." {
		t.Fatal("a second owner took a held lease")
	}
	b := decode(t, req(t, s, "GET", "/admin/info", readerKey, ""))["bridge"].(map[string]any)
	if b["holder"] != "live" {
		t.Fatalf("bridge %v", b)
	}
	clock = func() time.Time { return base.Add(61 * time.Second) }
	if r := req(t, s, "POST", "/admin/bridge/lease", owner2Key, ""); r.StatusCode != 200 {
		t.Fatalf("after the TTL: %d", r.StatusCode)
	}
	if decode(t, req(t, s, "DELETE", "/admin/bridge/lease", ownerKey, ""))["released"] != false {
		t.Fatal("a non-holder released the lease")
	}
	if decode(t, req(t, s, "DELETE", "/admin/bridge/lease", owner2Key, ""))["released"] != true {
		t.Fatal("the holder could not release")
	}
}

// ---- the feed -------------------------------------------------------------------------------

func readLines(t *testing.T, r *http.Response, n int) []string {
	t.Helper()
	sc := bufio.NewScanner(r.Body)
	var out []string
	for len(out) < n && sc.Scan() {
		out = append(out, sc.Text())
	}
	return out
}

func TestFeedFromAnyCursorThenLiveThenHeartbeat(t *testing.T) {
	setup(t)
	loadFixtures(t)
	head := jr.Head()
	s := server(t)
	all := readLines(t, req(t, s, "GET", "/feed?after=0", readerKey, ""), int(head))
	if len(all) != int(head) {
		t.Fatalf("after=0 gave %d of %d", len(all), head)
	}
	for i, l := range all {
		var m map[string]any
		if json.Unmarshal([]byte(l), &m) != nil || m["seq"] != float64(i+1) || m["kind"] == nil {
			t.Fatalf("line %d: %s", i, l)
		}
	}
	// a second client at another cursor gets the same rows from there
	part := readLines(t, req(t, s, "GET", "/feed?after=10", ownerKey, ""), int(head)-10)
	if len(part) != int(head)-10 || part[0] != all[10] {
		t.Fatal("cursor 10")
	}
	// a stream stays open: new rows arrive, then heartbeats
	old := heartbeat
	heartbeat = 50 * time.Millisecond
	defer func() { heartbeat = old }()
	r := req(t, s, "GET", "/feed?after="+itoa(head), readerKey, "")
	go func() {
		time.Sleep(30 * time.Millisecond)
		emit(Rec{"kind": "person", "person": Rec{"key": "4915550000009@s.whatsapp.net", "push_name": "new"}})
	}()
	got := readLines(t, r, 2)
	if !strings.Contains(got[0], `"seq":`+itoa(head+1)) || got[1] != "" {
		t.Fatalf("live then heartbeat: %q", got)
	}
	// nothing was pruned
	if n := len(readLines(t, req(t, s, "GET", "/feed?after=0", readerKey, ""), int(head)+1)); n != int(head)+1 {
		t.Fatalf("journal lost rows: %d", n)
	}
	if req(t, s, "GET", "/feed?after=x", readerKey, "").StatusCode != 400 {
		t.Fatal("bad cursor accepted")
	}
}

func itoa(n int64) string { b, _ := json.Marshal(n); return string(b) }

// ---- older messages and media ---------------------------------------------------------------

func TestOlderAnchorsOnTheOldestKnownMessageAndIsRateLimited(t *testing.T) {
	log := setup(t)
	loadFixtures(t)
	s := server(t)
	r := req(t, s, "POST", "/admin/older", readerKey, `{"chat":"`+ALEX+`","count":500}`)
	if r.StatusCode != 202 || decode(t, r)["request_id"] != "REQ1" {
		t.Fatalf("older %d", r.StatusCode)
	}
	h := log.history[0].GetProtocolMessage().GetPeerDataOperationRequestMessage().GetHistorySyncOnDemandRequest()
	if h.GetChatJID() != ALEX || h.GetOldestMsgID() != "A2" || !h.GetOldestMsgFromMe() ||
		h.GetOldestMsgTimestampMS() != (T0+60_000)/1000 || h.GetOnDemandMsgCount() != 50 {
		t.Fatalf("request %v", h)
	}
	if pm := log.history[0].GetProtocolMessage(); pm.GetType() != waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE {
		t.Fatal("not a peer data request")
	}
	r = req(t, s, "POST", "/admin/older", readerKey, `{"chat":"`+ALEX+`"}`)
	if r.StatusCode != 429 || decode(t, r)["retry_after_ms"] == nil {
		t.Fatal("a second request right away went out")
	}
	historyLast = time.Time{}
	if req(t, s, "POST", "/admin/older", readerKey, `{"chat":"4999@s.whatsapp.net"}`).StatusCode != 404 {
		t.Fatal("a chat without messages")
	}
	body := `{"chat":"` + ALEX + `","before":{"id":"X9","from_me":false,"ts":1780000000}}`
	if req(t, s, "POST", "/admin/older", readerKey, body).StatusCode != 202 {
		t.Fatal("explicit anchor")
	}
	isConnected = func() bool { return false }
	historyLast = time.Time{}
	r = req(t, s, "POST", "/admin/older", readerKey, body)
	if r.StatusCode != 503 || decode(t, r)["error"] != "WhatsApp is not reachable." {
		t.Fatal("offline")
	}
	if len(log.history) != 2 {
		t.Fatalf("sent %d requests", len(log.history))
	}
}

func TestMediaDownloadKeepsTheFileAndRetryAnswersAreListed(t *testing.T) {
	log := setup(t)
	s := server(t)
	if req(t, s, "POST", "/admin/media/download", readerKey, `{}`).StatusCode != 400 {
		t.Fatal("empty ref")
	}
	r := req(t, s, "POST", "/admin/media/download", readerKey, `{"direct_path":"/v/x","media_key":"a2V5","mtype":"photo"}`)
	data, _ := io.ReadAll(r.Body)
	sha := r.Header.Get("X-Sha256")
	if r.StatusCode != 200 || string(data) != "picture" || len(sha) != 64 {
		t.Fatalf("download %d %q", r.StatusCode, data)
	}
	r = req(t, s, "GET", "/admin/media/"+sha, readerKey, "")
	data, _ = io.ReadAll(r.Body)
	if r.StatusCode != 200 || string(data) != "picture" {
		t.Fatal("kept file")
	}
	// the mux cleans a dot-dot path before any handler runs, so the handler's own check
	// is driven directly with the raw path
	esc := httptest.NewRecorder()
	handleMediaFile(esc, &http.Request{Method: "GET", URL: &url.URL{Path: "/admin/media/../journal.db"}})
	if esc.Code != 404 || !strings.Contains(esc.Body.String(), "no such file") {
		t.Fatalf("path escape %d %s", esc.Code, esc.Body.String())
	}
	key := strings.Repeat("A", 43) + "="
	for body, want := range map[string]int{
		`{}`: 400,
		`{"message_id":"X","chat":"1@s.whatsapp.net","media_key":"a2V5"}`:        400,
		`{"message_id":"X","chat":"1-2@g.us","media_key":"` + key + `"}`:         400,
		`{"message_id":"X","chat":"1@s.whatsapp.net","media_key":"` + key + `"}`: 202,
	} {
		if got := req(t, s, "POST", "/admin/media/retry", readerKey, body).StatusCode; got != want {
			t.Errorf("%s: %d, want %d", body, got, want)
		}
	}
	if _, retries, _ := log.kinds(); retries != 1 {
		t.Fatalf("retries %d", retries)
	}
	onMediaRetry(&events.MediaRetry{MessageID: "NOT-OURS"})
	onMediaRetry(&events.MediaRetry{MessageID: "X", Error: &events.MediaRetryError{Code: 2}})
	ans := decode(t, req(t, s, "GET", "/admin/media/retry", readerKey, ""))["answers"].(map[string]any)
	if len(ans) != 1 || ans["X"].(map[string]any)["result"] != "ERROR" {
		t.Fatalf("answers %v", ans)
	}
	if len(retryPending) != 0 {
		t.Fatal("answered request still pending")
	}
	_ = types.JID{}
}

func TestMediaOverTheCapIsRefusedBeforeItIsDownloaded(t *testing.T) {
	setup(t)
	s := server(t)
	old := maxMediaBytes
	maxMediaBytes = 10
	defer func() { maxMediaBytes = old }()
	calls, got := 0, []byte("0123456789")
	download = func(_ context.Context, _ mediaRef, _ []byte) ([]byte, error) {
		calls++
		return got, nil
	}
	ref := `{"direct_path":"/v/x","media_key":"a2V5","mtype":"document","file_length":`
	r := req(t, s, "POST", "/admin/media/download", readerKey, ref+`11}`)
	if r.StatusCode != 413 || calls != 0 || !strings.Contains(decode(t, r)["error"].(string), "larger") {
		t.Fatalf("over the cap: %d, %d downloads", r.StatusCode, calls)
	}
	if r := req(t, s, "POST", "/admin/media/download", readerKey, ref+`10}`); r.StatusCode != 200 {
		t.Fatalf("at the cap: %d", r.StatusCode)
	}
	// the message said less than came: refused, and not kept
	got = []byte("0123456789A")
	r = req(t, s, "POST", "/admin/media/download", readerKey, ref+`5}`)
	if r.StatusCode != 413 {
		t.Fatalf("a file larger than its message said: %d", r.StatusCode)
	}
	if entries, _ := os.ReadDir(filepath.Join(stateDir, "media")); len(entries) != 1 {
		t.Fatalf("kept %d files", len(entries))
	}
}

func TestTheMediaCapComesFromTheEnvironment(t *testing.T) {
	t.Setenv("WA_LINK_MAX_MEDIA_MB", "")
	if envMB("WA_LINK_MAX_MEDIA_MB", 100) != 100<<20 {
		t.Fatal("default")
	}
	t.Setenv("WA_LINK_MAX_MEDIA_MB", "7")
	if envMB("WA_LINK_MAX_MEDIA_MB", 100) != 7<<20 {
		t.Fatal("set")
	}
	t.Setenv("WA_LINK_MAX_MEDIA_MB", "lots")
	if envMB("WA_LINK_MAX_MEDIA_MB", 100) != 100<<20 {
		t.Fatal("not a number")
	}
}

func TestAnExpiredQRCodeRestartsForANewOne(t *testing.T) {
	setup(t)
	exited := make(chan int, 1)
	oldExit, oldDelay := exitFn, restartDelay
	exitFn, restartDelay = func(code int) { exited <- code }, time.Millisecond
	defer func() { exitFn, restartDelay = oldExit, oldDelay }()
	codes := make(chan whatsmeow.QRChannelItem, 2)
	codes <- whatsmeow.QRChannelItem{Event: whatsmeow.QRChannelEventCode, Code: "2@abc"}
	codes <- whatsmeow.QRChannelTimeout
	close(codes)
	watchQR(codes)
	select {
	case code := <-exited:
		if code != 0 {
			t.Fatalf("exit %d", code)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("wa-link stayed up without a code to scan")
	}
	if s := snapshot(); s.QR != "" || s.Error != "The QR code expired. A new one comes up in a moment." {
		t.Fatalf("status %+v", s)
	}
}

// mediaMsg: a message record in chat with a file whose plain sha256 is sha.
func mediaMsg(chat, key, sha string, ts int64) Rec {
	return Rec{"kind": "message", "chat": Rec{"key": chat, "kind": "group"},
		"message": Rec{"key": key, "ts": ts, "type": "media", "text": "",
			"media": Rec{"type": "photo", "ref": Rec{"file_sha256": sha}}}}
}

func TestDeleteAndIgnoreTakesAChatOutOfTheJournalForGood(t *testing.T) {
	setup(t)
	loadFixtures(t)
	s := server(t)
	other := BEA
	shaOf := func(b string) string { sum := sha256.Sum256([]byte(b)); return hex.EncodeToString(sum[:]) }
	gone, kept, shared := keepFile([]byte("group photo")), keepFile([]byte("bea photo")),
		keepFile([]byte("forwarded"))
	if gone != shaOf("group photo") {
		t.Fatal("kept files are named by their sha256")
	}
	for _, r := range []Rec{mediaMsg(GROUP, "G1", gone, T0+1), mediaMsg(GROUP, "G2", shared, T0+2),
		mediaMsg(other, "B1", kept, T0+3), mediaMsg(other, "B2", shared, T0+4)} {
		if _, err := jr.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	body := `{"chats":["` + GROUP + `"]}`
	if r := req(t, s, "POST", "/admin/ignored", readerKey, body); r.StatusCode != 403 {
		t.Fatalf("a reader deleted a chat: %d", r.StatusCode)
	}
	own := `{"chats":["` + OWN + `@s.whatsapp.net"]}`
	if r := req(t, s, "POST", "/admin/ignored", ownerKey, own); r.StatusCode != 400 {
		t.Fatalf("the bridged chat was deleted: %d", r.StatusCode)
	}
	r := req(t, s, "POST", "/admin/ignored", ownerKey, body)
	if r.StatusCode != 200 || decode(t, r)["deleted"].(float64) < 2 {
		t.Fatalf("ignore: %d", r.StatusCode)
	}
	for _, rec := range journalRecs(t) {
		if k, chat, _, _ := recordKeys(rec); chat == GROUP {
			t.Fatalf("a %s record of the chat is left", k)
		}
	}
	files := func(sha string) bool { _, err := os.Stat(filepath.Join(stateDir, "media", sha)); return err == nil }
	if files(gone) || !files(kept) || !files(shared) {
		t.Fatalf("files: its own %v, another chat's %v, shared %v", files(gone), files(kept), files(shared))
	}
	// what the phone sends again of it, a history sync or a live message, stays out
	if seq, err := jr.Append(mediaMsg(GROUP, "G3", gone, T0+5)); seq != 0 || err != nil {
		t.Fatalf("journaled again: %d %v", seq, err)
	}
	loadFixtures(t)
	for _, rec := range journalRecs(t) {
		if _, chat, _, _ := recordKeys(rec); chat == GROUP {
			t.Fatal("a history sync brought it back")
		}
	}
	got := decode(t, req(t, s, "GET", "/admin/ignored", ownerKey, ""))["chats"].([]any)
	if len(got) != 1 || got[0] != GROUP {
		t.Fatalf("ignored %v", got)
	}
	// included again: what comes from now on is journaled
	if r := req(t, s, "DELETE", "/admin/ignored?chat="+url.QueryEscape(GROUP), ownerKey, ""); r.StatusCode != 200 {
		t.Fatalf("include again: %d", r.StatusCode)
	}
	if seq, _ := jr.Append(mediaMsg(GROUP, "G4", gone, T0+6)); seq == 0 {
		t.Fatal("still ignored")
	}
}
