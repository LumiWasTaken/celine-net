// Who may use wa-link, and the bridge lease.
//
// Every door needs `Authorization: Bearer <key>`. The keys come from the clients file
// (-clients-file, the app's keys/connector-clients: "name:key:role", comma or line
// separated; role owner or reader). An owner may send into the own chat
// (with the bridge lease), change the marker and hold the lease; a reader may read the
// feed, call the read tools, fetch media and ask the phone for older messages.
//
// The bridge lease: one app instance at a time answers in the bridge chat. The holder
// renews it every 20 s; it lapses 60 s after the last renewal.
package main

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"
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

// ---- the lease -------------------------------------------------------------------------

var clock = time.Now // tests move time

const leaseTTL = 60 * time.Second

type Lease struct {
	mu      sync.Mutex
	holder  string
	expires time.Time
}

var errLeaseHeld = errors.New("Another instance holds the bridge.")

func (l *Lease) Claim(name string) (string, time.Time, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := clock()
	if l.holder != "" && l.holder != name && now.Before(l.expires) {
		return l.holder, l.expires, errLeaseHeld
	}
	l.holder, l.expires = name, now.Add(leaseTTL)
	return l.holder, l.expires, nil
}

func (l *Lease) Release(name string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.holder != name {
		return false
	}
	l.holder, l.expires = "", time.Time{}
	return true
}

// Holds: the named client holds a lease that has not lapsed.
func (l *Lease) Holds(name string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.holder == name && clock().Before(l.expires)
}

func (l *Lease) State() (string, int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.holder == "" || !clock().Before(l.expires) {
		return "", 0
	}
	return l.holder, l.expires.UnixMilli()
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
