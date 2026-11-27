package main

// About texts: what each person wrote under their name, read slowly.
//
// A change WhatsApp tells of (events.UserAbout) is journaled at once. Everyone else is
// asked for one at a time: on the first round one person every aboutFirst, on every
// later round one every aboutRefresh, so each About comes round again and a change is
// journaled as a person record (the archive keeps the old text in the person's history).
// The place in the round is kept in kv (about_cursor, about_rounds), so a restart goes on
// where it was.

import (
	"context"
	"strconv"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/types"
)

var (
	aboutFirst   = 2 * time.Minute
	aboutRefresh = 15 * time.Minute
)

// lookupAbout: one person's About text from WhatsApp; ok false when it did not answer.
var lookupAbout = func(ctx context.Context, j types.JID) (string, bool) {
	if waClient == nil || !isConnected() {
		return "", false
	}
	infos, err := waClient.GetUserInfo(ctx, []types.JID{j})
	if err != nil {
		return "", false
	}
	u, ok := infos[j]
	return u.Status, ok
}

// nextAbout: the person after the cursor, and whether the round started over.
func (j *Journal) nextAbout(cursor string) (key string, wrapped bool) {
	q := `SELECT DISTINCT message_key FROM journal WHERE kind = 'person'
		AND message_key LIKE '%@s.whatsapp.net' AND message_key > ? ORDER BY message_key LIMIT 1`
	if j.db.QueryRow(q, cursor).Scan(&key) == nil {
		return key, false
	}
	if cursor == "" || j.db.QueryRow(q, "").Scan(&key) != nil {
		return "", false
	}
	return key, true
}

// aboutStep: read one person's About and journal it. Returns how long to wait.
func aboutStep(ctx context.Context) time.Duration {
	rounds, _ := strconv.Atoi(jr.Get("about_rounds", "0"))
	wait := aboutFirst
	if rounds > 0 {
		wait = aboutRefresh
	}
	key, wrapped := jr.nextAbout(jr.Get("about_cursor", ""))
	if key == "" {
		return wait
	}
	jid, err := types.ParseJID(key)
	if err != nil {
		_ = jr.Set("about_cursor", key)
		return wait
	}
	text, ok := lookupAbout(ctx, jid)
	if !ok {
		return wait // not reachable now: the same person next time
	}
	// an empty About is a fact too: the archive ends the one they had
	emit(Rec{"kind": "person", "person": Rec{"key": key, "bio": strings.TrimSpace(text), "profile": true}})
	_ = jr.Set("about_cursor", key)
	if wrapped {
		_ = jr.Set("about_rounds", strconv.Itoa(rounds+1))
	}
	return wait
}

func aboutLoop(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(aboutStep(ctx)):
		}
	}
}
