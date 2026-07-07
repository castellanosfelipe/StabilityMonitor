"""Dashboard backup/import affordances."""
from __future__ import annotations

from pathlib import Path


def test_dashboard_exposes_connection_backup_import_controls():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/js/app.js").read_text(encoding="utf-8")

    assert 'id="btn-backup"' in html
    assert 'id="modal-backup"' in html
    assert 'href="/api/backup"' in html
    assert 'id="backup-file"' in html
    assert 'id="backup-result"' in html
    assert 'api("/api/restore"' in js
    assert "restoreBackupFile" in js
