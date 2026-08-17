package config

import (
	"testing"
	"time"
)

func TestLoadDefaults(t *testing.T) {
	for _, key := range []string{
		"CONTROL_PLANE_HTTP_ADDR", "DATABASE_URL", "REDIS_URL", "READINESS_TIMEOUT",
		"STARTUP_TIMEOUT", "SHUTDOWN_TIMEOUT", "AUTO_MIGRATE", "DB_MAX_OPEN_CONNS",
		"DB_MAX_IDLE_CONNS", "DB_CONN_MAX_LIFETIME",
	} {
		t.Setenv(key, "")
	}
	c, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if c.HTTPAddress != ":8080" || c.ReadinessTimeout != 2*time.Second || !c.AutoMigrate {
		t.Fatalf("unexpected defaults: %+v", c)
	}
}

func TestLoadRejectsInvalidPool(t *testing.T) {
	t.Setenv("DB_MAX_OPEN_CONNS", "2")
	t.Setenv("DB_MAX_IDLE_CONNS", "3")
	if _, err := Load(); err == nil {
		t.Fatal("Load() accepted idle pool larger than open pool")
	}
}

func TestLoadRejectsInvalidDuration(t *testing.T) {
	t.Setenv("READINESS_TIMEOUT", "not-a-duration")
	if _, err := Load(); err == nil {
		t.Fatal("Load() accepted an invalid duration")
	}
}

func TestLoadRejectsInvalidBoolean(t *testing.T) {
	t.Setenv("AUTO_MIGRATE", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load() accepted an invalid boolean")
	}
}

func TestLoadRejectsInvalidInteger(t *testing.T) {
	t.Setenv("DB_MAX_OPEN_CONNS", "many")
	if _, err := Load(); err == nil {
		t.Fatal("Load() accepted an invalid integer")
	}
}
