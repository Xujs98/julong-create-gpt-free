package health

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestHealthEndpointDoesNotNeedDependencies(t *testing.T) {
	h := NewHandler("control-plane", "test", time.Now(), time.Second, map[string]CheckFunc{
		"database": func(context.Context) error { return errors.New("down") },
	})
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	res := httptest.NewRecorder()
	h.ServeHTTP(res, req)
	if res.Code != http.StatusOK {
		t.Fatalf("health status = %d, want %d", res.Code, http.StatusOK)
	}
}

func TestReadyEndpointReportsAllChecks(t *testing.T) {
	h := NewHandler("control-plane", "test", time.Now(), time.Second, map[string]CheckFunc{
		"database": func(context.Context) error { return nil },
		"redis":    func(context.Context) error { return nil },
	})
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	res := httptest.NewRecorder()
	h.ServeHTTP(res, req)
	if res.Code != http.StatusOK {
		t.Fatalf("ready status = %d, want %d", res.Code, http.StatusOK)
	}
	if got := res.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q", got)
	}
	body := res.Body.String()
	for _, want := range []string{`"status":"ready"`, `"database":"ok"`, `"redis":"ok"`} {
		if !contains(body, want) {
			t.Fatalf("ready body %q does not contain %q", body, want)
		}
	}
}

func TestReadyEndpointReturns503WhenCheckFails(t *testing.T) {
	h := NewHandler("control-plane", "test", time.Now(), 10*time.Millisecond, map[string]CheckFunc{
		"database": func(context.Context) error { return errors.New("down") },
	})
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	res := httptest.NewRecorder()
	h.ServeHTTP(res, req)
	if res.Code != http.StatusServiceUnavailable {
		t.Fatalf("ready status = %d, want %d", res.Code, http.StatusServiceUnavailable)
	}
}

func contains(value, needle string) bool {
	for i := 0; i+len(needle) <= len(value); i++ {
		if value[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
