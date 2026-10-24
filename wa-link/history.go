// Older messages: ask the user's OWN phone for older messages of one chat. Nothing is sent to any contact: the request is a peer message to our own
// account (whatsmeow BuildHistorySyncRequest + SendPeerMessage).
//
//	POST /admin/older {"chat": "<jid>", "count": 50,
//	                   "before": {"id": "<message id>", "from_me": bool, "ts": <unix seconds>}}
//	  -> 202 {"request_id", "sent_at"}   the request went out
//	     400 bad body | 404 no message of that chat to anchor on | 429 {"error",
//	     "retry_after_ms"} too soon | 503 {"error": "WhatsApp is not reachable."}
//
// Without "before" the anchor is the oldest journaled message of that chat. The phone
// answers "up to count messages immediately before that message" as a HistorySync of
// type ON_DEMAND, which the event handler journals like any history chunk; its
// per-chat end ("complete", "more", "no_access") is a "history" record.
//
// At most one request every 3 seconds, whatever the app does, so a bug there cannot
// flood the phone.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

const (
	historyMaxCount = 50
	historySpacing  = 3 * time.Second
)

type anchorMsg struct {
	ID     string `json:"id"`
	FromMe bool   `json:"from_me"`
	TS     int64  `json:"ts"` // unix seconds of the anchor message
}

type olderReq struct {
	Chat   string     `json:"chat"`
	Count  int        `json:"count"`
	Before *anchorMsg `json:"before"`
}

var (
	historyMu   sync.Mutex
	historyLast time.Time
	// swapped by setRealSends (main.go) and by tests
	historySend = func(ctx context.Context, msg *waE2E.Message) (string, error) {
		return "", errors.New("not connected")
	}
	isConnected = func() bool { return false }
)

// buildHistoryRequest: BuildHistorySyncRequest uses only the anchor's chat, id, side
// and time, and nothing of the client, so it works without one (tests).
func buildHistoryRequest(chat string, a anchorMsg, count int) (*waE2E.Message, error) {
	jid, err := types.ParseJID(chat)
	if err != nil {
		return nil, err
	}
	info := &types.MessageInfo{ID: a.ID, Timestamp: time.Unix(a.TS, 0),
		MessageSource: types.MessageSource{Chat: jid, IsFromMe: a.FromMe,
			IsGroup: jid.Server == types.GroupServer}}
	return (*whatsmeow.Client)(nil).BuildHistorySyncRequest(info, count), nil
}

// oldestAnchor: the oldest journaled message of a chat.
func oldestAnchor(chat string) (anchorMsg, bool) {
	msgs, err := jr.ChatMessages(chat)
	if err != nil || len(msgs) == 0 {
		return anchorMsg{}, false
	}
	m := obj(msgs[0].Rec, "message")
	return anchorMsg{ID: msgs[0].Key, FromMe: truthy(m, "from_me"), TS: msgs[0].TS / 1000}, true
}

type olderErr struct {
	code int
	body map[string]any
}

// askOlder sends one history request. The error carries the door's answer.
func askOlder(ctx context.Context, q olderReq) (string, int64, *olderErr) {
	if q.Chat == "" {
		return "", 0, &olderErr{400, map[string]any{"error": "need chat"}}
	}
	if q.Count <= 0 || q.Count > historyMaxCount {
		q.Count = historyMaxCount
	}
	var a anchorMsg
	if q.Before != nil {
		a = *q.Before
		if a.ID == "" || a.TS <= 0 {
			return "", 0, &olderErr{400, map[string]any{"error": "before needs id and ts (unix seconds)"}}
		}
	} else {
		var ok bool
		if a, ok = oldestAnchor(q.Chat); !ok {
			return "", 0, &olderErr{404, map[string]any{"error": "No message of this chat is known yet."}}
		}
	}
	msg, err := buildHistoryRequest(q.Chat, a, q.Count)
	if err != nil {
		return "", 0, &olderErr{400, map[string]any{"error": "bad chat jid"}}
	}
	if !isConnected() {
		return "", 0, &olderErr{503, map[string]any{"error": "WhatsApp is not reachable."}}
	}
	historyMu.Lock() // one request in flight
	defer historyMu.Unlock()
	if wait := historySpacing - time.Since(historyLast); wait > 0 {
		return "", 0, &olderErr{429, map[string]any{"error": "Too soon; ask again in a few seconds.",
			"retry_after_ms": wait.Milliseconds()}}
	}
	historyLast = time.Now()
	id, err := historySend(ctx, msg)
	if err != nil {
		return "", 0, &olderErr{503, map[string]any{"error": "WhatsApp is not reachable."}}
	}
	return id, historyLast.UnixMilli(), nil
}

func handleOlder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]string{"error": "POST only"})
		return
	}
	var q olderReq
	if err := json.NewDecoder(r.Body).Decode(&q); err != nil {
		writeJSON(w, 400, map[string]string{"error": "need chat, count, optional before"})
		return
	}
	id, at, e := askOlder(r.Context(), q)
	if e != nil {
		writeJSON(w, e.code, e.body)
		return
	}
	writeJSON(w, 202, map[string]any{"request_id": id, "sent_at": at})
}

// ---- waiting for the phone's answer (the `older` tool) --------------------------------------

var (
	waitMu  sync.Mutex
	waiters = map[string][]chan struct{}{}
)

// waitAnswer: a channel closed when an ON_DEMAND answer for this chat is journaled.
func waitAnswer(chat string) chan struct{} {
	ch := make(chan struct{})
	waitMu.Lock()
	waiters[chat] = append(waiters[chat], ch)
	waitMu.Unlock()
	return ch
}

func dropWaiter(chat string, ch chan struct{}) {
	waitMu.Lock()
	defer waitMu.Unlock()
	list := waiters[chat]
	for i, c := range list {
		if c == ch {
			waiters[chat] = append(list[:i], list[i+1:]...)
			break
		}
	}
	if len(waiters[chat]) == 0 {
		delete(waiters, chat)
	}
}

// answered: an ON_DEMAND chunk about these chats is in the journal now.
func answered(chats []string) {
	waitMu.Lock()
	defer waitMu.Unlock()
	for _, c := range chats {
		for _, ch := range waiters[c] {
			close(ch)
		}
		delete(waiters, c)
	}
}
