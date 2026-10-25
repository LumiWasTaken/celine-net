// Who may use wa-link.
//
// Every door needs `Authorization: Bearer <key>`. The keys come from the clients file
// (-clients-file, the app's keys/connector-clients: "name:key:role", comma or line
// separated; role owner or reader). An owner may also delete and ignore a chat
// (/admin/ignored); a reader may read the feed, call the tools, fetch media and ask the
// phone for older messages.
package main

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"syscall"
)

type Client struct {
	Name string
	Role string // owner | reader
}

func (c Client) Owner() bool { return c.Role == "owner" }

type clientKey struct{}

func parseClients(spec string) (map[string]Client, error) {
	out := map[string]Client{}
	for _, part := range strings.FieldsFunc(spec, func(r rune) bool { return r == ',' || r == '\n' }) {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		f := strings.Split(part, ":")
		if len(f) != 3 || f[0] == "" || len(f[1]) < 16 || (f[2] != "owner" && f[2] != "reader") {
			return nil, fmt.Errorf("client %q is not name:key:owner|reader "+
				"with a key of at least 16 characters", f[0])
		}
		out[f[1]] = Client{Name: f[0], Role: f[2]}
	}
	if len(out) == 0 {
		return nil, errors.New("the clients file names no client")
	}
	return out, nil
}

// authed: the door behind a bearer key. owner: the door is for owners only.
func authed(clients map[string]Client, owner bool, h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		c, ok := lookup(clients, r)
		if !ok {
			writeJSON(w, 401, map[string]string{"error": "This key is not known to wa-link."})
			return
		}
		if owner && !c.Owner() {
			writeJSON(w, 403, map[string]string{"error": "This app instance may only read."})
			return
		}
		h(w, r.WithContext(context.WithValue(r.Context(), clientKey{}, c)))
	}
}

func lookup(clients map[string]Client, r *http.Request) (Client, bool) {
	h := r.Header.Get("Authorization")
	if !strings.HasPrefix(h, "Bearer ") {
		return Client{}, false
	}
	key := strings.TrimSpace(h[len("Bearer "):])
	for k, c := range clients {
		if subtle.ConstantTimeCompare([]byte(k), []byte(key)) == 1 {
			return c, true
		}
	}
	return Client{}, false
}

func caller(ctx context.Context) (Client, bool) {
	c, ok := ctx.Value(clientKey{}).(Client)
	return c, ok
}

// ---- one process per state directory -------------------------------------------------------

func lockState(dir string) (*os.File, error) {
	f, err := os.OpenFile(dir+"/lock", os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, fmt.Errorf("another wa-link already uses %s", dir)
	}
	return f, nil
}
