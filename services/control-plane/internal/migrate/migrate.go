package migrate

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"path/filepath"
	"sort"
	"strings"
)

// The migration files are embedded so the control-plane image is self-contained.
// The same files can be inspected and reviewed independently of the binary.
//
//go:embed migrations/*.sql
var migrationFS embed.FS

const migrationTable = `
CREATE TABLE IF NOT EXISTS platform_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)`

const migrationLockID int64 = 738219044

type Migration struct {
	Version string
	SQL     string
}

func Files() ([]Migration, error) {
	entries, err := fs.Glob(migrationFS, "migrations/*.sql")
	if err != nil {
		return nil, err
	}
	sort.Strings(entries)
	files := make([]Migration, 0, len(entries))
	for _, name := range entries {
		body, err := migrationFS.ReadFile(name)
		if err != nil {
			return nil, err
		}
		files = append(files, Migration{
			Version: strings.TrimSuffix(filepath.Base(name), filepath.Ext(name)),
			SQL:     string(body),
		})
	}
	return files, nil
}

// Up applies migrations in lexical version order. A PostgreSQL advisory
// transaction lock makes startup safe when several control-plane replicas
// begin at the same time.
func Up(ctx context.Context, db *sql.DB) error {
	if err := ensureMigrationTable(ctx, db); err != nil {
		return err
	}
	files, err := Files()
	if err != nil {
		return fmt.Errorf("load migrations: %w", err)
	}
	for _, migration := range files {
		if err := applyOne(ctx, db, migration); err != nil {
			return err
		}
	}
	return nil
}

func ensureMigrationTable(ctx context.Context, db *sql.DB) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin migration bootstrap: %w", err)
	}
	defer tx.Rollback() // no-op after a successful commit
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, migrationLockID); err != nil {
		return fmt.Errorf("lock migration bootstrap: %w", err)
	}
	if _, err := tx.ExecContext(ctx, migrationTable); err != nil {
		return fmt.Errorf("create migration table: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit migration bootstrap: %w", err)
	}
	return nil
}

func applyOne(ctx context.Context, db *sql.DB, migration Migration) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin migration %s: %w", migration.Version, err)
	}
	defer tx.Rollback() // no-op after a successful commit
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, migrationLockID); err != nil {
		return fmt.Errorf("lock migration %s: %w", migration.Version, err)
	}
	var applied bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM platform_schema_migrations WHERE version = $1)`, migration.Version).Scan(&applied); err != nil {
		return fmt.Errorf("check migration %s: %w", migration.Version, err)
	}
	if applied {
		return tx.Commit()
	}
	if _, err := tx.ExecContext(ctx, migration.SQL); err != nil {
		return fmt.Errorf("apply migration %s: %w", migration.Version, err)
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO platform_schema_migrations(version) VALUES ($1)`, migration.Version); err != nil {
		return fmt.Errorf("record migration %s: %w", migration.Version, err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit migration %s: %w", migration.Version, err)
	}
	return nil
}
