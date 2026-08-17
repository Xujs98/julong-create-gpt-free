package health

import (
	"context"
	"encoding/json"
	"net/http"
	"sort"
	"sync"
	"time"
)

type CheckFunc func(context.Context) error

type Handler struct {
	service          string
	version          string
	startedAt        time.Time
	readinessTimeout time.Duration
	checks           map[string]CheckFunc
}

func NewHandler(service, version string, startedAt time.Time, readinessTimeout time.Duration, checks map[string]CheckFunc) http.Handler {
	copyChecks := make(map[string]CheckFunc, len(checks))
	for name, check := range checks {
		if check != nil {
			copyChecks[name] = check
		}
	}
	h := &Handler{
		service:          service,
		version:          version,
		startedAt:        startedAt,
		readinessTimeout: readinessTimeout,
		checks:           copyChecks,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", h.health)
	mux.HandleFunc("GET /readyz", h.ready)
	mux.HandleFunc("GET /", h.info)
	return mux
}

type response struct {
	Status        string            `json:"status"`
	Service       string            `json:"service"`
	Version       string            `json:"version"`
	UptimeSeconds int64             `json:"uptime_seconds,omitempty"`
	Checks        map[string]string `json:"checks,omitempty"`
}

func (h *Handler) health(w http.ResponseWriter, r *http.Request) {
	h.write(w, http.StatusOK, response{
		Status:  "ok",
		Service: h.service,
		Version: h.version,
	})
}

func (h *Handler) info(w http.ResponseWriter, r *http.Request) {
	seconds := int64(time.Since(h.startedAt) / time.Second)
	if seconds < 0 {
		seconds = 0
	}
	h.write(w, http.StatusOK, response{
		Status:        "ok",
		Service:       h.service,
		Version:       h.version,
		UptimeSeconds: seconds,
	})
}

func (h *Handler) ready(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), h.readinessTimeout)
	defer cancel()

	results := make(map[string]string, len(h.checks))
	var wg sync.WaitGroup
	var mu sync.Mutex
	for _, name := range sortedKeys(h.checks) {
		name, check := name, h.checks[name]
		wg.Add(1)
		go func() {
			defer wg.Done()
			status := "ok"
			if err := check(ctx); err != nil {
				status = "down"
			}
			mu.Lock()
			results[name] = status
			mu.Unlock()
		}()
	}
	wg.Wait()

	statusCode := http.StatusOK
	status := "ready"
	for _, value := range results {
		if value != "ok" {
			statusCode = http.StatusServiceUnavailable
			status = "not_ready"
			break
		}
	}
	h.write(w, statusCode, response{
		Status:  status,
		Service: h.service,
		Version: h.version,
		Checks:  results,
	})
}

func sortedKeys(values map[string]CheckFunc) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func (h *Handler) write(w http.ResponseWriter, status int, value response) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
