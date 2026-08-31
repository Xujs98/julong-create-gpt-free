# React frontend

The production page currently uses a compatibility shell: React owns navigation,
active-tab state, and lifecycle bootstrapping while each existing feature panel
remains a DOM-compatible island. The shell is intentionally mounted into the
existing sidebar so CSS, element IDs, modal behavior, and API contracts remain
unchanged during the migration.

The Vite app in this directory is the source layout for the next migration
steps. Build output is configured for `webui/static/react`; the Flask page keeps
the checked-in runtime bridge so the dashboard also works on hosts without a
Node.js installation.
