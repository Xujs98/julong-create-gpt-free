package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"

	"github.com/Xujs98/julong-create-gpt-free/services/control-plane/internal/config"
	"github.com/Xujs98/julong-create-gpt-free/services/control-plane/internal/health"
	"github.com/Xujs98/julong-create-gpt-free/services/control-plane/internal/migrate"
	"github.com/Xujs98/julong-create-gpt-free/services/control-plane/internal/redisprobe"
)

var (
	version   = "dev"
	commit    = "none"
	buildDate = "unknown"
)

const serviceName = "registration-control-plane"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(runHealthcheck(os.Args[2:]))
	}
	if err := run(); err != nil {
		slog.Error("control plane stopped", "error", err)
		os.Exit(1)
	}
}

func run() error {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load config: %w", err)
	}
	logger.Info("starting control plane", "version", version, "commit", commit, "build_date", buildDate, "config", cfg.String())

	db, err := sql.Open("pgx", cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("open postgres: %w", err)
	}
	defer db.Close()
	db.SetMaxOpenConns(cfg.DBMaxOpenConns)
	db.SetMaxIdleConns(cfg.DBMaxIdleConns)
	db.SetConnMaxLifetime(cfg.DBConnMaxLifetime)

	startupCtx, cancelStartup := context.WithTimeout(context.Background(), cfg.StartupTimeout)
	defer cancelStartup()
	if err := waitForPostgres(startupCtx, db); err != nil {
		return err
	}
	if cfg.AutoMigrate {
		if err := migrate.Up(startupCtx, db); err != nil {
			return err
		}
		logger.Info("postgres migrations applied")
	}
	redis, err := redisprobe.New(cfg.RedisURL)
	if err != nil {
		return fmt.Errorf("parse redis URL: %w", err)
	}

	startedAt := time.Now()
	handler := health.NewHandler(serviceName, version, startedAt, cfg.ReadinessTimeout, map[string]health.CheckFunc{
		"postgres": func(ctx context.Context) error {
			return db.PingContext(ctx)
		},
		"schema": func(ctx context.Context) error {
			return checkSchema(ctx, db)
		},
		"redis": redis.Ping,
	})
	server := &http.Server{
		Addr:              cfg.HTTPAddress,
		Handler:           requestLogger(handler),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		logger.Info("http server listening", "addr", cfg.HTTPAddress)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()

	stopCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	select {
	case err := <-serverErr:
		return fmt.Errorf("http server: %w", err)
	case <-stopCtx.Done():
		shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancelShutdown()
		if err := server.Shutdown(shutdownCtx); err != nil {
			return fmt.Errorf("shutdown http server: %w", err)
		}
		logger.Info("http server stopped")
		return nil
	}
}

func waitForPostgres(ctx context.Context, db *sql.DB) error {
	interval := 100 * time.Millisecond
	for {
		if err := db.PingContext(ctx); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("postgres did not become ready: %w", ctx.Err())
		case <-time.After(interval):
			if interval < 2*time.Second {
				interval *= 2
			}
		}
	}
}

func checkSchema(ctx context.Context, db *sql.DB) error {
	const query = `
SELECT
    to_regclass('public.accounts') IS NOT NULL AND
    to_regclass('public.registration_batches') IS NOT NULL AND
    to_regclass('public.registration_jobs') IS NOT NULL AND
    to_regclass('public.job_events') IS NOT NULL AND
    to_regclass('public.workers') IS NOT NULL`
	var ready bool
	if err := db.QueryRowContext(ctx, query).Scan(&ready); err != nil {
		return err
	}
	if !ready {
		return errors.New("required platform tables are missing")
	}
	return nil
}

func requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		next.ServeHTTP(w, r)
		slog.Debug("http request", "method", r.Method, "path", r.URL.Path, "duration_ms", time.Since(started).Milliseconds())
	})
}

func runHealthcheck(args []string) int {
	endpoint := "http://127.0.0.1:8080/healthz"
	if len(args) > 0 && args[0] != "" {
		endpoint = args[0]
	}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(endpoint)
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return 1
	}
	return 0
}
