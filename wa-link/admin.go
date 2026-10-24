// The app's doors (/admin/*). Readers may look, ask for older messages and fetch
// media; owners may also delete chats.
//
//	GET    /admin/status        the connection record and counts
//	GET    /admin/qr            the current pairing code as PNG, 404 when none
//	POST   /admin/older         history.go
//	GET|POST|DELETE /admin/ignored  deleted and ignored chats (owner, journal.go)
//	POST   /admin/media/download, GET|POST /admin/media/retry, GET /admin/media/<sha256>
package main

import (
	"net/http"

	"github.com/skip2/go-qrcode"
)

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

func handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, statusDoc())
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
	mux.HandleFunc("/admin/qr", authed(clients, false, get(handleQR)))
	mux.HandleFunc("/admin/older", authed(clients, false, handleOlder))
	mux.HandleFunc("/admin/ignored", authed(clients, true, jr.handleIgnored))
	mux.HandleFunc("/admin/media/download", authed(clients, false, handleDownload))
	mux.HandleFunc("/admin/media/retry", authed(clients, false, handleRetry))
	mux.HandleFunc("/admin/media/", authed(clients, false, get(handleMediaFile)))
}
