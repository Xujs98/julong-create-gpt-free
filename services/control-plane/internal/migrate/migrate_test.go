package migrate

import (
	"strings"
	"testing"
)

func TestFilesAreOrderedAndContainCoreTables(t *testing.T) {
	files, err := Files()
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 1 {
		t.Fatalf("migration count = %d, want 1", len(files))
	}
	if files[0].Version != "000001_platform" {
		t.Fatalf("migration version = %q", files[0].Version)
	}
	for _, table := range []string{"accounts", "registration_batches", "registration_jobs", "job_events", "workers"} {
		if !strings.Contains(files[0].SQL, "CREATE TABLE IF NOT EXISTS "+table) {
			t.Errorf("migration does not create %s", table)
		}
	}
}
