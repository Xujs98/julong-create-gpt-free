package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config contains only control-plane settings. Registration-specific secrets
// remain owned by the Python worker and are deliberately not read here.
type Config struct {
	HTTPAddress       string
	DatabaseURL       string
	RedisURL          string
	ReadinessTimeout  time.Duration
	StartupTimeout    time.Duration
	ShutdownTimeout   time.Duration
	AutoMigrate       bool
	DBMaxOpenConns    int
	DBMaxIdleConns    int
	DBConnMaxLifetime time.Duration
}

func Load() (Config, error) {
	readinessTimeout, err := durationValue("READINESS_TIMEOUT", 2*time.Second)
	if err != nil {
		return Config{}, err
	}
	startupTimeout, err := durationValue("STARTUP_TIMEOUT", 45*time.Second)
	if err != nil {
		return Config{}, err
	}
	shutdownTimeout, err := durationValue("SHUTDOWN_TIMEOUT", 10*time.Second)
	if err != nil {
		return Config{}, err
	}
	autoMigrate, err := boolValue("AUTO_MIGRATE", true)
	if err != nil {
		return Config{}, err
	}
	maxOpenConns, err := intValue("DB_MAX_OPEN_CONNS", 25)
	if err != nil {
		return Config{}, err
	}
	maxIdleConns, err := intValue("DB_MAX_IDLE_CONNS", 10)
	if err != nil {
		return Config{}, err
	}
	connMaxLifetime, err := durationValue("DB_CONN_MAX_LIFETIME", 30*time.Minute)
	if err != nil {
		return Config{}, err
	}
	c := Config{
		HTTPAddress:       envOr("CONTROL_PLANE_HTTP_ADDR", ":8080"),
		DatabaseURL:       envOr("DATABASE_URL", "postgres://registration:registration-local-only@127.0.0.1:5432/registration?sslmode=disable"),
		RedisURL:          envOr("REDIS_URL", "redis://127.0.0.1:6379/0"),
		ReadinessTimeout:  readinessTimeout,
		StartupTimeout:    startupTimeout,
		ShutdownTimeout:   shutdownTimeout,
		AutoMigrate:       autoMigrate,
		DBMaxOpenConns:    maxOpenConns,
		DBMaxIdleConns:    maxIdleConns,
		DBConnMaxLifetime: connMaxLifetime,
	}
	if strings.TrimSpace(c.HTTPAddress) == "" {
		return Config{}, errors.New("CONTROL_PLANE_HTTP_ADDR must not be empty")
	}
	if strings.TrimSpace(c.DatabaseURL) == "" {
		return Config{}, errors.New("DATABASE_URL must not be empty")
	}
	if strings.TrimSpace(c.RedisURL) == "" {
		return Config{}, errors.New("REDIS_URL must not be empty")
	}
	if c.ReadinessTimeout <= 0 || c.StartupTimeout <= 0 || c.ShutdownTimeout <= 0 || c.DBConnMaxLifetime <= 0 {
		return Config{}, errors.New("timeouts must be greater than zero")
	}
	if c.DBMaxOpenConns < 1 || c.DBMaxIdleConns < 0 || c.DBMaxIdleConns > c.DBMaxOpenConns {
		return Config{}, errors.New("database pool limits are invalid")
	}
	return c, nil
}

func envOr(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func durationValue(key string, fallback time.Duration) (time.Duration, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback, nil
	}
	parsed, err := time.ParseDuration(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be a duration: %w", key, err)
	}
	return parsed, nil
}

func boolValue(key string, fallback bool) (bool, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be a boolean: %w", key, err)
	}
	return parsed, nil
}

func intValue(key string, fallback int) (int, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", key, err)
	}
	return parsed, nil
}

// String is useful for startup diagnostics without exposing connection URLs.
func (c Config) String() string {
	return fmt.Sprintf("http=%s auto_migrate=%t db_pool=%d/%d", c.HTTPAddress, c.AutoMigrate, c.DBMaxOpenConns, c.DBMaxIdleConns)
}
