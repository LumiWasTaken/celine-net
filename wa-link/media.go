// Media: downloading attachments by reference, and asking the user's OWN phone to
// re-upload media whose CDN copy expired. The app drives both from
// server/celine/kb/media_wa.py.
//
//	POST /admin/media/download {direct_path, media_key, file_hash, enc_file_hash, mtype}
//	                      -> 200 the decrypted file (also kept in <state>/media/<sha256>)
//	                         | 410 {"error"} expired | 503 not reachable
//	POST /admin/media/retry {message_id, chat, sender, from_me, is_group, media_key}
//	                      -> 202 {"requested": true}. Sends one media retry receipt to the
//	                      user's own account (whatsmeow SendMediaRetryReceipt); nothing goes
//	                      to any contact. The phone's answer arrives as a MediaRetry event.
//	GET  /admin/media/retry -> {"answers": {"<message id>": {"at", "result",
//	                      "direct_path", "error"}}}
//	GET  /admin/media/<sha256> -> a kept file
package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

var (
	retryMu      sync.Mutex
	retryPending = map[string][]byte{} // message id -> media key, until the phone answers
	// swapped by setRealSends (main.go) and by tests
	retrySend = func(ctx context.Context, info *types.MessageInfo, key []byte) error {
		return errors.New("not connected")
	}
	download = func(ctx context.Context, ref mediaRef, fileHash []byte) ([]byte, error) {
		return waClient.DownloadMediaWithPath(ctx, ref.DirectPath, b64(ref.EncFileHash),
			fileHash, b64(ref.MediaKey), mediaTypeOf(ref.MType), "", fileHash == nil)
	}
)

type mediaRef struct {
	DirectPath  string `json:"direct_path"`
	MediaKey    string `json:"media_key"`     // base64
	FileHash    string `json:"file_hash"`     // hex sha256 of the plain file
	EncFileHash string `json:"enc_file_hash"` // base64
	MType       string `json:"mtype"`         // photo | video | gif | audio | voice | document | sticker
}

type retryReq struct {
	MessageID string `json:"message_id"`
	Chat      string `json:"chat"`
	Sender    string `json:"sender"`
	FromMe    bool   `json:"from_me"`
	IsGroup   bool   `json:"is_group"`
	MediaKey  string `json:"media_key"` // base64
}

func b64(s string) []byte {
	if s == "" {
		return nil
	}
	for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.RawStdEncoding,
		base64.URLEncoding, base64.RawURLEncoding} {
		if b, err := enc.DecodeString(s); err == nil {
			return b
		}
	}
	return nil
}

func mediaTypeOf(t string) whatsmeow.MediaType {
	switch t {
	case "video", "gif":
		return whatsmeow.MediaVideo
	case "audio", "voice":
		return whatsmeow.MediaAudio
	case "document", "file":
		return whatsmeow.MediaDocument
	default: // photo, sticker
		return whatsmeow.MediaImage
	}
}

func keepFile(data []byte) string {
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	p := filepath.Join(stateDir, "media", sha)
	if _, err := os.Stat(p); err != nil {
		tmp := p + ".tmp"
		if os.WriteFile(tmp, data, 0o600) == nil {
			_ = os.Rename(tmp, p)
		}
	}
	return sha
}

func handleDownload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]string{"error": "POST only"})
		return
	}
	var ref mediaRef
	if err := json.NewDecoder(r.Body).Decode(&ref); err != nil || ref.DirectPath == "" {
		writeJSON(w, 400, map[string]string{"error": "need direct_path, media_key, hashes"})
		return
	}
	if !isConnected() {
		writeJSON(w, 503, map[string]string{"error": "WhatsApp is not reachable."})
		return
	}
	var fileHash []byte
	if ref.FileHash != "" {
		fileHash, _ = hex.DecodeString(ref.FileHash)
	}
	data, err := download(r.Context(), ref, fileHash)
	if err != nil {
		if errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
			errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410) ||
			errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) {
			writeJSON(w, 410, map[string]string{"error": "expired on the CDN"})
			return
		}
		writeJSON(w, 502, map[string]string{"error": err.Error()})
		return
	}
	w.Header().Set("X-Sha256", keepFile(data))
	w.Header().Set("Content-Type", "application/octet-stream")
	_, _ = w.Write(data)
}

func handleRetry(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		writeJSON(w, 200, map[string]any{"answers": retryAnswers()})
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]string{"error": "GET or POST only"})
		return
	}
	var q retryReq
	if err := json.NewDecoder(r.Body).Decode(&q); err != nil || q.MessageID == "" || q.Chat == "" {
		writeJSON(w, 400, map[string]string{"error": "need message_id, chat, media_key"})
		return
	}
	key := b64(q.MediaKey)
	if len(key) != 32 {
		writeJSON(w, 400, map[string]string{"error": "media_key must be 32 bytes, base64"})
		return
	}
	chat, err := types.ParseJID(q.Chat)
	if err != nil {
		writeJSON(w, 400, map[string]string{"error": "bad chat jid"})
		return
	}
	isGroup := q.IsGroup || chat.Server == types.GroupServer
	info := &types.MessageInfo{ID: q.MessageID,
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: q.FromMe, IsGroup: isGroup}}
	if q.Sender != "" {
		if s, err := types.ParseJID(q.Sender); err == nil {
			info.Sender = s
		}
	}
	if isGroup && info.Sender.IsEmpty() && !q.FromMe {
		writeJSON(w, 400, map[string]string{"error": "a group message needs its sender"})
		return
	}
	if !isConnected() {
		writeJSON(w, 503, map[string]string{"error": "WhatsApp is not reachable."})
		return
	}
	retryMu.Lock()
	retryPending[q.MessageID] = key
	retryMu.Unlock()
	if err := retrySend(r.Context(), info, key); err != nil {
		retryMu.Lock()
		delete(retryPending, q.MessageID)
		retryMu.Unlock()
		writeJSON(w, 502, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, 202, map[string]bool{"requested": true})
}

const retryPrefix = "media_retry:"

// onMediaRetry: the phone's answer to one of our requests; others are not ours.
func onMediaRetry(evt *events.MediaRetry) {
	retryMu.Lock()
	key, ok := retryPending[evt.MessageID]
	delete(retryPending, evt.MessageID)
	retryMu.Unlock()
	if !ok {
		return
	}
	rec := map[string]any{"at": time.Now().UnixMilli()}
	notif, err := whatsmeow.DecryptMediaRetryNotification(evt, key)
	if err != nil {
		rec["result"] = "ERROR"
		rec["error"] = err.Error()
	} else {
		rec["result"] = strings.ToUpper(notif.GetResult().String())
		rec["direct_path"] = notif.GetDirectPath()
	}
	b, _ := json.Marshal(rec)
	_ = jr.Set(retryPrefix+evt.MessageID, string(b))
}

func retryAnswers() map[string]any {
	out := map[string]any{}
	rows, err := jr.db.Query("SELECT key, value FROM kv WHERE key LIKE ?", retryPrefix+"%")
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var k, v string
		if rows.Scan(&k, &v) == nil {
			var m map[string]any
			if json.Unmarshal([]byte(v), &m) == nil {
				out[strings.TrimPrefix(k, retryPrefix)] = m
			}
		}
	}
	return out
}

var shaRe = regexp.MustCompile(`^[0-9a-f]{64}$`)

func handleMediaFile(w http.ResponseWriter, r *http.Request) {
	sha := strings.TrimPrefix(r.URL.Path, "/admin/media/")
	if !shaRe.MatchString(sha) {
		writeJSON(w, 404, map[string]string{"error": "no such file"})
		return
	}
	p := filepath.Join(stateDir, "media", sha)
	if _, err := os.Stat(p); err != nil {
		writeJSON(w, 404, map[string]string{"error": "no such file"})
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	http.ServeFile(w, r, p)
}
