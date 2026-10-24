// The app's doors (/admin/*). Readers may look, ask for older messages and fetch
// media; owners may also change the marker and hold the bridge lease.
//
//	GET    /admin/status        the connection record and counts
//	GET    /admin/qr            the current pairing code as PNG, 404 when none
//	GET    /admin/info          the write chat, the marker, the part size, the lease
//	PUT    /admin/bridge        {"marker"} (owner)
//	POST   /admin/bridge/lease  claim or renew (owner), 409 when another holds it
//	DELETE /admin/bridge/lease  release (owner)
//	POST   /admin/older         history.go
//	GET    /admin/records       every record of one chat, NDJSON (journal.go)
//	POST   /admin/media/download, GET|POST /admin/media/retry, GET /admin/media/<sha256>
package main

import (
	"encoding/json"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/skip2/go-qrcode"
)

const (
	defaultMarker = "Celine: "
	markerKey     = "marker"
	partChars     = 3000 // the longest text the app's bridge sends as one message
)

func marker() string { return jr.Get(markerKey, defaultMarker) }

// connection: the one state record the app shows (connected, unreachable, needs_login).
func connection(s Status) map[string]any {
	state, errText := "connected", ""
	switch s.State {
	case "connected":
	case "pairing", "logged_out":
		state = "needs_login"
		errText = s.Error
	case "starting":
		state, errText = "unreachable", "Not connected to WhatsApp yet."
	default:
		state, errText = "unreachable", s.Error
		if errText == "" {
			errText = "WhatsApp is not reachable."
		}
	}
	rec := map[string]any{"state": state, "since": s.Since, "error": nil,
		"last_sync": nil, "next_sync": nil}
	if errText != "" && state != "connected" {
		rec["error"] = errText
	}
	return rec
}

func statusDoc() map[string]any {
	s := snapshot()
	doc := map[string]any{"connection": connection(s), "account": nil, "qr_at": nil,
		"journal":        map[string]any{"head": jr.Head()},
		"history_chunks": s.HistoryDone, "live_messages": s.Messages}
	if own, ok := ownJID(); ok {
		acc := map[string]any{"id": own.User + "@s.whatsapp.net", "name": nil}
		if waClient != nil && waClient.Store.PushName != "" {
			acc["name"] = waClient.Store.PushName
		}
		doc["account"] = acc
	}
	if s.QR != "" {
		doc["qr_at"] = s.QRAt
	}
	return doc
}

func infoDoc() map[string]any {
	holder, exp := lease.State()
	info := map[string]any{"platform": "whatsapp", "write_chat": nil,
		"write_chat_name": "Message yourself", "marker": marker(), "part_chars": partChars,
		"bridge": map[string]any{"holder": nil, "expires_at": nil}}
	if own, ok := ownJID(); ok {
		info["write_chat"] = own.User + "@s.whatsapp.net"
	}
	if holder != "" {
		info["bridge"] = map[string]any{"holder": holder, "expires_at": exp}
	}
	return info
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, statusDoc())
}

func handleInfo(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, infoDoc())
}

func handleQR(w http.ResponseWriter, r *http.Request) {
	code := snapshot().QR
	if code == "" {
		writeJSON(w, 404, map[string]string{"error": "There is no code to scan right now."})
		return
	}
	png, err := qrcode.Encode(code, qrcode.Medium, 600)
	if err != nil {
		writeJSON(w, 500, map[string]string{"error": err.Error()})
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(png)
}

func handleBridge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeJSON(w, 405, map[string]string{"error": "PUT only"})
		return
	}
	var body struct {
		Marker *string `json:"marker"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Marker == nil {
		writeJSON(w, 400, map[string]string{"error": "need marker"})
		return
	}
	m := *body.Marker
	if n := utf8.RuneCountInString(m); n < 1 || n > 40 || strings.TrimSpace(m) == "" {
		writeJSON(w, 400, map[string]string{"error": "The marker is 1 to 40 characters."})
		return
	}
	if err := jr.Set(markerKey, m); err != nil {
		writeJSON(w, 500, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, 200, infoDoc())
}

func handleLease(w http.ResponseWriter, r *http.Request) {
	c, _ := caller(r.Context())
	switch r.Method {
	case http.MethodPost:
		holder, exp, err := lease.Claim(c.Name)
		if err != nil {
			writeJSON(w, 409, map[string]any{"error": err.Error(), "holder": holder,
				"expires_at": exp.UnixMilli()})
			return
		}
		writeJSON(w, 200, map[string]any{"holder": holder, "expires_at": exp.UnixMilli()})
	case http.MethodDelete:
		writeJSON(w, 200, map[string]any{"released": lease.Release(c.Name)})
	default:
		writeJSON(w, 405, map[string]string{"error": "POST or DELETE only"})
	}
}

func registerAdmin(mux *http.ServeMux, clients map[string]Client) {
	get := func(h http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet {
				writeJSON(w, 405, map[string]string{"error": "GET only"})
				return
			}
			h(w, r)
		}
	}
	mux.HandleFunc("/admin/status", authed(clients, false, get(handleStatus)))
	mux.HandleFunc("/admin/info", authed(clients, false, get(handleInfo)))
	mux.HandleFunc("/admin/qr", authed(clients, false, get(handleQR)))
	mux.HandleFunc("/admin/bridge", authed(clients, true, handleBridge))
	mux.HandleFunc("/admin/bridge/lease", authed(clients, true, handleLease))
	mux.HandleFunc("/admin/older", authed(clients, false, handleOlder))
	mux.HandleFunc("/admin/records", authed(clients, false, get(jr.handleRecords)))
	mux.HandleFunc("/admin/media/download", authed(clients, false, handleDownload))
	mux.HandleFunc("/admin/media/retry", authed(clients, false, handleRetry))
	mux.HandleFunc("/admin/media/", authed(clients, false, get(handleMediaFile)))
}
