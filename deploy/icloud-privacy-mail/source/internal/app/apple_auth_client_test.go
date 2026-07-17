package app

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
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

func TestAppleAuthSRPCompleteRetriesTransientHTTPStatus(t *testing.T) {
	var completeAttempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/signin/init":
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"iteration": 1,
				"salt":      "c2FsdA==",
				"protocol":  "s2k",
				"b":         "Ag==",
				"c":         "challenge",
			})
		case "/signin/complete":
			if completeAttempts.Add(1) == 1 {
				http.Error(w, "temporarily unavailable", http.StatusServiceUnavailable)
				return
			}
			w.WriteHeader(http.StatusConflict)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &AppleAuthClient{httpClient: server.Client()}
	session := &appleAuthSession{
		Endpoints: appleAuthEndpoints{
			Home: "https://www.icloud.com",
			Auth: server.URL,
		},
		AppleID:  "test@example.com",
		ClientID: "test-client",
		FrameID:  "test-frame",
	}
	needs2FA, err := client.authSRP(context.Background(), session, "test-password")
	if err != nil {
		t.Fatalf("authSRP returned error: %v", err)
	}
	if !needs2FA {
		t.Fatal("authSRP should report the successful 409 response as requiring 2FA")
	}
	if got := completeAttempts.Load(); got != 2 {
		t.Fatalf("complete attempts = %d, want 2", got)
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

func TestAppleAuthTransientErrorHidesHTMLResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("<html>upstream diagnostic details</html>"))
	}))
	defer server.Close()

	client := &AppleAuthClient{httpClient: server.Client()}
	status, _, err := client.do(
		context.Background(),
		&appleAuthSession{},
		http.MethodPost,
		server.URL+"/appleauth/auth/signin/complete",
		nil,
		nil,
		nil,
		true,
	)
	if err == nil {
		t.Fatal("expected transient HTTP error")
	}
	if status != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", status, http.StatusServiceUnavailable)
	}
	message := err.Error()
	if !strings.Contains(message, "HTTP 503") {
		t.Fatalf("error message %q does not contain HTTP status", message)
	}
	if strings.Contains(message, "<html>") || strings.Contains(message, "diagnostic details") {
		t.Fatalf("error message exposed upstream HTML: %q", message)
	}
}
