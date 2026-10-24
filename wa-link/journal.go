// The journal: everything wa-link learns, as canonical records with a rising seq, in
// <state>/journal.db. Never pruned but by the user: it is the device's complete copy and
// the only source any app instance needs (GET /feed?after=0 gives the whole history),
// except the chats the user deleted and ignored (`ignored`: their records and files are
// gone, and nothing of them is journaled again until they are included again). The
// `sent` table holds every message id wa-link itself sent (the bridge's loop guard);
// `kv` holds the connector's own settings (the marker).
package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
)

const journalSchema = `
CREATE TABLE IF NOT EXISTS journal (
    seq         INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    chat_key    TEXT NOT NULL DEFAULT '',
    message_key TEXT NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL DEFAULT 0,
    hash        TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_key ON journal (kind, chat_key, message_key, seq);
CREATE INDEX IF NOT EXISTS journal_chat_ts ON journal (chat_key, ts);
CREATE TABLE IF NOT EXISTS sent (message_key TEXT PRIMARY KEY, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ignored (chat_key TEXT PRIMARY KEY, at INTEGER NOT NULL);
`

type Journal struct {
	db     *sql.DB
	mu     sync.Mutex
	head   int64
	wake   chan struct{} // closed when a row is appended, then replaced
	wakeMu sync.Mutex
}

func OpenJournal(path string) (*Journal, error) {
	db, err := sql.Open("sqlite", "file:"+path+
		"?_pragma=busy_timeout(10000)&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // one writer, and reads see every committed row
	if _, err := db.Exec(journalSchema); err != nil {
		db.Close()
		return nil, err
	}
	j := &Journal{db: db, wake: make(chan struct{})}
	if err := db.QueryRow("SELECT COALESCE(MAX(seq), 0) FROM journal").Scan(&j.head); err != nil {
		db.Close()
		return nil, err
	}
	return j, nil
}

func (j *Journal) Close() error { return j.db.Close() }

func (j *Journal) Head() int64 {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.head
}

// Wait returns a channel that is closed at the next append.
func (j *Journal) Wait() <-chan struct{} {
	j.wakeMu.Lock()
	defer j.wakeMu.Unlock()
	return j.wake
}

func (j *Journal) ring() {
	j.wakeMu.Lock()
	close(j.wake)
	j.wake = make(chan struct{})
	j.wakeMu.Unlock()
}

// recordKeys: the columns a record is found by.
func recordKeys(r Rec) (kind, chat, msg string, ts int64) {
	kind, _ = r["kind"].(string)
	switch kind {
	case "message":
		chat = str(obj(r, "chat"), "key")
		m := obj(r, "message")
		msg = str(m, "key")
		ts = tsMs(m["ts"])
	case "chat":
		chat = str(obj(r, "chat"), "key")
	case "person":
		msg = str(obj(r, "person"), "key")
	case "delete":
		chat = str(r, "chat_key")
		var keys []string
		for _, k := range list(r, "message_keys") {
			if s, ok := k.(string); ok {
				keys = append(keys, s)
			}
		}
		if ks, ok := r["message_keys"].([]string); ok {
			keys = ks
		}
		msg = strings.Join(keys, ",")
		ts = tsMs(r["ts"])
	default:
		chat = str(r, "chat_key")
		msg = str(r, "message_key")
		ts = tsMs(r["ts"])
	}
	return
}

// Append journals a record unless it is already there (see below). A message wa-link
// sent itself carries "from_link": true. Returns the seq (0 when nothing was appended).
func (j *Journal) Append(r Rec) (int64, error) {
	kind, chat, msg, ts := recordKeys(r)
	if kind == "" {
		return 0, errors.New("record without kind")
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if chat != "" {
		var one int
		if j.db.QueryRow("SELECT 1 FROM ignored WHERE chat_key = ?", chat).Scan(&one) == nil {
			return 0, nil // deleted and ignored: the phone's history of it stays out
		}
	}
	if kind == "message" && msg != "" {
		if m := obj(r, "message"); m != nil && truthy(m, "from_me") {
			var one int
			if j.db.QueryRow("SELECT 1 FROM sent WHERE message_key = ?", msg).Scan(&one) == nil {
				m["from_link"] = true
			}
		}
	}
	body, err := json.Marshal(r)
	if err != nil {
		return 0, err
	}
	sum := sha256.Sum256(body)
	hash := hex.EncodeToString(sum[:])
	// state-like records (a name) repeat only when they differ from the latest one, so a
	// name changed back is journaled; event-like ones (a message, an edit, the phone's
	// word on a chat) never repeat, so history the phone sends twice journals nothing
	q := `SELECT hash FROM journal WHERE kind = ? AND chat_key = ? AND message_key = ?
		AND hash = ? LIMIT 1`
	if kind == "person" || kind == "chat" {
		q = `SELECT hash FROM (SELECT hash FROM journal WHERE kind = ? AND chat_key = ?
			AND message_key = ? ORDER BY seq DESC LIMIT 1) WHERE hash = ?`
	}
	var same string
	if j.db.QueryRow(q, kind, chat, msg, hash).Scan(&same) == nil {
		return 0, nil
	}
	res, err := j.db.Exec(`INSERT INTO journal (kind, chat_key, message_key, ts, hash, body)
		VALUES (?, ?, ?, ?, ?, ?)`, kind, chat, msg, ts, hash, string(body))
	if err != nil {
		return 0, err
	}
	seq, _ := res.LastInsertId()
	j.head = seq
	j.ring()
	return seq, nil
}

type Row struct {
	Seq  int64
	Body string
}

// Line: the record as the feed sends it, seq first.
func (r Row) Line() string {
	if len(r.Body) < 2 || r.Body == "{}" {
		return fmt.Sprintf(`{"seq":%d}`, r.Seq)
	}
	return fmt.Sprintf(`{"seq":%d,%s`, r.Seq, r.Body[1:])
}

// Rec: the record with its seq.
func (r Row) Rec() Rec {
	var m Rec
	_ = json.Unmarshal([]byte(r.Body), &m)
	if m == nil {
		m = Rec{}
	}
	m["seq"] = r.Seq
	return m
}

func (j *Journal) rows(q string, args ...any) ([]Row, error) {
	rs, err := j.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rs.Close()
	var out []Row
	for rs.Next() {
		var r Row
		if err := rs.Scan(&r.Seq, &r.Body); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rs.Err()
}

func (j *Journal) After(after int64, limit int) ([]Row, error) {
	return j.rows("SELECT seq, body FROM journal WHERE seq > ? ORDER BY seq LIMIT ?", after, limit)
}

// ---- deleted and ignored chats ---------------------------------------------------------

// Ignore deletes every record of these chats and their kept files (a file another chat's
// record names stays), and keeps them out from now on. Returns the records deleted.
func (j *Journal) Ignore(chats []string) (int64, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	tx, err := j.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	files := map[string]bool{}
	var deleted int64
	for _, chat := range chats {
		rs, err := tx.Query("SELECT body FROM journal WHERE chat_key = ? AND kind = 'message'", chat)
		if err != nil {
			return 0, err
		}
		for rs.Next() {
			var body string
			var r Rec
			if rs.Scan(&body) == nil && json.Unmarshal([]byte(body), &r) == nil {
				if sha := str(obj(obj(obj(r, "message"), "media"), "ref"), "file_sha256"); shaRe.MatchString(sha) {
					files[sha] = true
				}
			}
		}
		rs.Close()
		res, err := tx.Exec("DELETE FROM journal WHERE chat_key = ?", chat)
		if err != nil {
			return 0, err
		}
		n, _ := res.RowsAffected()
		deleted += n
		if _, err := tx.Exec("INSERT OR IGNORE INTO ignored (chat_key, at) VALUES (?, ?)",
			chat, now()); err != nil {
			return 0, err
		}
	}
	var gone []string
	for sha := range files {
		var one int
		if tx.QueryRow("SELECT 1 FROM journal WHERE body LIKE ? LIMIT 1", "%"+sha+"%").Scan(&one) != nil {
			gone = append(gone, sha)
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	for _, sha := range gone {
		_ = os.Remove(filepath.Join(stateDir, "media", sha))
	}
	return deleted, nil
}

// Unignore: a chat included again; what comes of it from now on is journaled.
func (j *Journal) Unignore(chat string) error {
	j.mu.Lock()
	defer j.mu.Unlock()
	_, err := j.db.Exec("DELETE FROM ignored WHERE chat_key = ?", chat)
	return err
}

func (j *Journal) Ignored() ([]string, error) {
	rs, err := j.db.Query("SELECT chat_key FROM ignored ORDER BY chat_key")
	if err != nil {
		return nil, err
	}
	defer rs.Close()
	out := []string{}
	for rs.Next() {
		var k string
		if err := rs.Scan(&k); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, rs.Err()
}

// handleIgnored: the chats the user deleted and ignored (owner only).
//
//	GET    /admin/ignored              -> {"chats": [...]}
//	POST   /admin/ignored {"chats"}    -> {"deleted": n}: their records and files go, and
//	                                      nothing of them is journaled again; never the
//	                                      bridged chat
//	DELETE /admin/ignored?chat=<key>   -> {"ok": true}: included again
func (j *Journal) handleIgnored(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		chats, err := j.Ignored()
		if err != nil {
			writeJSON(w, 500, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"chats": chats})
	case http.MethodPost:
		var body struct {
			Chats []string `json:"chats"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Chats) == 0 {
			writeJSON(w, 400, map[string]string{"error": "need chats"})
			return
		}
		own, _ := ownJID()
		for _, c := range body.Chats {
			if c == "" || (own.User != "" && c == own.User+"@"+types.DefaultUserServer) {
				writeJSON(w, 400, map[string]string{"error": "This is the bridged chat. You cannot delete it."})
				return
			}
		}
		n, err := j.Ignore(body.Chats)
		if err != nil {
			writeJSON(w, 500, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"deleted": n})
	case http.MethodDelete:
		chat := r.URL.Query().Get("chat")
		if chat == "" {
			writeJSON(w, 400, map[string]string{"error": "need chat"})
			return
		}
		if err := j.Unignore(chat); err != nil {
			writeJSON(w, 500, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	default:
		writeJSON(w, 405, map[string]string{"error": "GET, POST or DELETE only"})
	}
}

// ---- sent ids and settings ------------------------------------------------------------

func (j *Journal) MarkSent(id string, at int64) error {
	_, err := j.db.Exec("INSERT OR IGNORE INTO sent (message_key, at) VALUES (?, ?)", id, at)
	return err
}

func (j *Journal) Unsent(id string) {
	_, _ = j.db.Exec("DELETE FROM sent WHERE message_key = ?", id)
}

func (j *Journal) IsSent(id string) bool {
	var one int
	return j.db.QueryRow("SELECT 1 FROM sent WHERE message_key = ?", id).Scan(&one) == nil
}

func (j *Journal) Get(key, def string) string {
	var v string
	if j.db.QueryRow("SELECT value FROM kv WHERE key = ?", key).Scan(&v) != nil {
		return def
	}
	return v
}

func (j *Journal) Set(key, value string) error {
	_, err := j.db.Exec(`INSERT INTO kv (key, value) VALUES (?, ?)
		ON CONFLICT (key) DO UPDATE SET value = excluded.value`, key, value)
	return err
}

// ---- reading chats back (the tools) ------------------------------------------------------

// Msg: a message as the tools show it: its latest record, edits and deletion applied.
type Msg struct {
	Seq     int64
	Key     string
	TS      int64
	Rec     Rec // the canonical message record, seq included
	Deleted bool
}

// ChatMessages: every message of a chat, oldest first.
func (j *Journal) ChatMessages(chat string) ([]Msg, error) {
	rows, err := j.rows(`SELECT seq, body FROM journal WHERE seq IN (
		SELECT MAX(seq) FROM journal WHERE kind = 'message' AND chat_key = ?
		GROUP BY message_key)`, chat)
	if err != nil {
		return nil, err
	}
	byKey := map[string]*Msg{}
	out := make([]Msg, 0, len(rows))
	for _, r := range rows {
		rec := r.Rec()
		m := obj(rec, "message")
		out = append(out, Msg{Seq: r.Seq, Key: str(m, "key"), TS: tsMs(m["ts"]), Rec: rec})
	}
	sort.Slice(out, func(a, b int) bool {
		if out[a].TS != out[b].TS {
			return out[a].TS < out[b].TS
		}
		return out[a].Seq < out[b].Seq
	})
	for i := range out {
		byKey[out[i].Key] = &out[i]
	}
	changes, err := j.rows(`SELECT seq, body FROM journal WHERE kind IN ('edit', 'delete')
		AND chat_key = ? ORDER BY seq`, chat)
	if err != nil {
		return nil, err
	}
	for _, c := range changes {
		rec := c.Rec()
		switch rec["kind"] {
		case "edit":
			if m := byKey[str(rec, "message_key")]; m != nil {
				mm := obj(m.Rec, "message")
				mm["text"] = str(rec, "text")
				mm["edited"] = true
			}
		case "delete":
			for _, k := range list(rec, "message_keys") {
				if s, ok := k.(string); ok && byKey[s] != nil {
					byKey[s].Deleted = true
				}
			}
		}
	}
	return out, nil
}

type ChatSummary struct {
	Key      string
	Kind     string
	Name     string
	LastTS   int64
	Messages int
}

// Chats: every chat with messages, most recently active first.
func (j *Journal) Chats() ([]ChatSummary, error) {
	rs, err := j.db.Query(`SELECT chat_key, MAX(ts), COUNT(DISTINCT message_key), MAX(seq)
		FROM journal WHERE kind = 'message' GROUP BY chat_key ORDER BY MAX(ts) DESC`)
	if err != nil {
		return nil, err
	}
	type agg struct {
		key     string
		ts, seq int64
		n       int
	}
	var all []agg
	for rs.Next() {
		var a agg
		if err := rs.Scan(&a.key, &a.ts, &a.n, &a.seq); err != nil {
			rs.Close()
			return nil, err
		}
		all = append(all, a)
	}
	rs.Close()
	out := make([]ChatSummary, 0, len(all))
	for _, a := range all {
		cs := ChatSummary{Key: a.key, LastTS: a.ts, Messages: a.n}
		var body string
		if j.db.QueryRow("SELECT body FROM journal WHERE seq = ?", a.seq).Scan(&body) == nil {
			var r Rec
			_ = json.Unmarshal([]byte(body), &r)
			cs.Kind, cs.Name = chatLabel(obj(r, "chat"))
		}
		// a later name change wins
		if j.db.QueryRow(`SELECT body FROM journal WHERE kind = 'chat' AND chat_key = ?
			ORDER BY seq DESC LIMIT 1`, a.key).Scan(&body) == nil {
			var r Rec
			_ = json.Unmarshal([]byte(body), &r)
			if k, n := chatLabel(obj(r, "chat")); n != "" {
				cs.Kind, cs.Name = k, n
			}
		}
		if cs.Name == "" {
			cs.Name = a.key
		}
		out = append(out, cs)
	}
	return out, nil
}

func chatLabel(c Rec) (kind, name string) {
	kind = str(c, "kind")
	name = str(c, "name")
	if name == "" {
		name = personName(obj(c, "peer"))
	}
	return
}

func personName(p Rec) string {
	return firstOf(str(p, "contact_name"), str(p, "push_name"), str(p, "display_name"),
		str(p, "phone"))
}

// ---- the feed door ------------------------------------------------------------------------

var heartbeat = 15 * time.Second

// handleFeed: GET /feed?after=N. NDJSON, every record after N in order, then new ones as
// they are journaled, an empty line every 15 s while idle. Stateless per client.
func (j *Journal) handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]string{"error": "GET only"})
		return
	}
	after, err := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
	if err != nil && r.URL.Query().Get("after") != "" {
		writeJSON(w, 400, map[string]string{"error": "after is a number"})
		return
	}
	flusher, _ := w.(http.Flusher)
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(200)
	if flusher != nil {
		flusher.Flush()
	}
	tick := time.NewTicker(heartbeat)
	defer tick.Stop()
	for {
		wake := j.Wait()
		for {
			rows, err := j.After(after, 500)
			if err != nil {
				return
			}
			for _, row := range rows {
				if _, err := fmt.Fprintln(w, row.Line()); err != nil {
					return
				}
				after = row.Seq
			}
			if flusher != nil {
				flusher.Flush()
			}
			if len(rows) < 500 {
				break
			}
		}
		select {
		case <-r.Context().Done():
			return
		case <-wake:
		case <-tick.C:
			if _, err := fmt.Fprintln(w); err != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}
