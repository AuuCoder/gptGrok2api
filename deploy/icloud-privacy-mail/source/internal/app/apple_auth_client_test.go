package app

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestAppleAuthRetriesTransientHTTPStatus(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) == 1 {
			http.Error(w, "temporarily unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]bool{"ok": true})
	}))
	defer server.Close()

	client := &AppleAuthClient{httpClient: server.Client()}
	session := &appleAuthSession{}
	var result struct {
		OK bool `json:"ok"`
	}
	status, _, err := client.doWithTransientRetry(
		context.Background(),
		session,
		http.MethodGet,
		server.URL+"/signin/init",
		nil,
		nil,
		&result,
		false,
	)
	if err != nil {
		t.Fatalf("doWithTransientRetry returned error: %v", err)
	}
	if status != http.StatusOK {
		t.Fatalf("status = %d, want %d", status, http.StatusOK)
	}
	if !result.OK {
		t.Fatal("successful retry response was not decoded")
	}
	if got := attempts.Load(); got != 2 {
		t.Fatalf("attempts = %d, want 2", got)
	}
}

func TestAppleAuthDoesNotRetryUnauthorized(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
	}))
	defer server.Close()

	client := &AppleAuthClient{httpClient: server.Client()}
	status, _, err := client.doWithTransientRetry(
		context.Background(),
		&appleAuthSession{},
		http.MethodGet,
		server.URL+"/signin/init",
		nil,
		nil,
		nil,
		false,
	)
	if err == nil {
		t.Fatal("expected unauthorized error")
	}
	if status != http.StatusUnauthorized {
		t.Fatalf("status = %d, want %d", status, http.StatusUnauthorized)
	}
	if got := attempts.Load(); got != 1 {
		t.Fatalf("attempts = %d, want 1", got)
	}
}

func TestAppleTransientErrorClassification(t *testing.T) {
	transientCodes := []string{"apple_protocol_http_transient", "apple_account_http_transient"}
	for _, code := range transientCodes {
		if !isAppleTransientNetworkError(errCode(code, "temporary", true)) {
			t.Fatalf("code %q should be transient", code)
		}
	}
	if isAppleTransientNetworkError(errCode("apple_protocol_http_error", "unauthorized", true)) {
		t.Fatal("non-transient protocol error should not be retried")
	}
}
