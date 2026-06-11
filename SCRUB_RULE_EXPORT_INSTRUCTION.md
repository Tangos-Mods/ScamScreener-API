# Anweisung: Content-Scrub-Regeln als `exclude.bundle.json` exportieren

## Zweck

Die im Training Hub gepflegten Content-Scrub-Regeln sollen nicht nur serverseitig beim Upload angewendet werden, sondern auch als exportierbares Bundle vorliegen, damit die Minecraft-Mod dieselben Phrasen bereits lokal entfernen oder aus der Detection ausschließen kann.

Der manuelle Mod-Workflow soll dadurch auf Folgendes reduziert werden:

1. Im Admin-Panel Bundle bauen.
2. `exclude.bundle.json` herunterladen.
3. Den fest in der Mod-Pipeline verdrahteten Exclude-Stand damit aktualisieren.

## Aktueller Stand

- Die Regeln liegen serverseitig in `content_scrub_rules`.
- Sie werden beim Upload serverseitig angewendet.
- Die Training-Pipeline baut aktuell nur ein NDJSON-Trainingsbundle.
- Es gibt aktuell **keinen** Export der Scrub-Regeln als separates Bundle.

## Ziel

Beim Erzeugen eines Trainings-Runs soll zusätzlich zum bestehenden `training-bundle-*.jsonl` immer ein zweites Artefakt erzeugt werden:

- `exclude.bundle-<timestamp>.json`

Dieses Artefakt ist die exportierbare Quelle für die Mod.

Wichtig:

- Keine statische Datei im Repo als Source of Truth einführen.
- Keine manuell gepflegte Parallelkonfiguration im Code anlegen.
- Source of Truth bleiben ausschließlich die Admin-Regeln aus `content_scrub_rules`.

## Exportformat

Das Exportformat soll bewusst einfach, deterministisch und mod-freundlich sein.

Empfohlenes JSON:

```json
{
  "format": "scamscreener_exclude_bundle",
  "version": 1,
  "generatedAt": "2026-06-06T12:34:56Z",
  "ruleCount": 2,
  "rules": [
    {
      "id": 1,
      "mode": "contains",
      "regex": false,
      "pattern": "discord.gg/"
    },
    {
      "id": 2,
      "mode": "contains",
      "regex": true,
      "pattern": "discord\\.gg/[A-Za-z0-9]+"
    }
  ]
}
```

## Verbindliche Exportregeln

- Nur `is_enabled = 1` exportieren.
- Immer stabil nach `id ASC` exportieren.
- `pattern` exakt unverändert exportieren.
- `regex` als bool exportieren.
- `mode` exakt als einer von `exact`, `starts_with`, `contains`, `ends_with` exportieren.
- Auch bei `0` Regeln ein gültiges leeres Bundle erzeugen.
- Das Exportbundle darf keine anderen Daten aus Uploads oder Cases enthalten.

## Erwartete Semantik in der Mod

Die Mod soll dieselbe Regelbedeutung verwenden wie der Server:

- `exact`: Wert komplett entfernen, wenn er exakt passt
- `starts_with`: passenden Präfix entfernen
- `contains`: alle Treffer entfernen
- `ends_with`: passendes Suffix entfernen

Bei `regex = true` gilt dieselbe Semantik auf Regex-Basis.

Wichtig:

- Die Mod darf das Bundle lokal intern in ihre fest verdrahtete Pipeline übernehmen.
- Das Hub-System muss **nicht** wissen, wie die Mod intern speichert.
- Das Hub-System muss nur einen sauberen, stabilen Export liefern.

## Betroffene Stellen im Backend

### 1. Regelquelle

Verwenden:

- `app/training_hub/core/content_scrubbing.py`

Benötigt wird dort eine neue reine Exportfunktion, z. B.:

- `_export_content_scrub_rules_bundle(database_path: Path | str) -> dict[str, Any]`

Diese Funktion soll:

- aktive Regeln laden
- Export-JSON zusammensetzen
- nur exportgeeignete Felder enthalten

### 2. Pipeline-Artefakt erzeugen

Erweitern:

- `app/training_hub/core/pipeline.py`

Beim Lauf von `_run_training_pipeline(...)` soll zusätzlich zum NDJSON-Bundle auch das Exclude-Bundle geschrieben werden.

Empfohlene Dateinamen:

- `training-bundle-<timestamp>.jsonl`
- `exclude.bundle-<timestamp>.json`

Beide Dateien sollen im bestehenden `settings.bundles_dir` liegen.

## Persistenz des zweiten Artefakts

Der Run muss das Exclude-Bundle referenzierbar machen.

Empfohlene Umsetzung:

1. `training_runs` um `exclude_bundle_path` erweitern
2. Migration für SQLite und MariaDB ergänzen
3. Pfad beim Pipeline-Lauf mitschreiben
4. Retention-, Backup- und Delete-Pfade um dieses Artefakt ergänzen

Betroffene Stellen:

- `app/training_hub/core/storage_schema_sqlite.py`
- `app/training_hub/core/storage_schema_mariadb.py`
- `app/training_hub/core/storage_migrations.py`
- `app/training_hub/core/recovery_backup.py`
- `app/training_hub/core/recovery_retention.py`
- `app/training_hub/core/account_ops.py`

## Download im Admin-Panel

Zusätzlich zum bestehenden Bundle-Download soll es einen zweiten Download geben.

Empfohlen:

- neue Route: `/admin/runs/{run_id}/exclude-bundle`

Erweitern:

- `app/training_hub/routes/admin_downloads.py`
- passende UI in der Runs-Seite

Anforderungen:

- nur Admin
- Pfadprüfung analog zum bestehenden Bundle-Download
- Audit-Log-Eintrag
- `application/json` als Medientyp

## UI-Anforderung

Auf der Admin-Runs-Seite soll pro Run sichtbar sein:

- Download Training Bundle
- Download Exclude Bundle

Wenn kein Exclude-Bundle vorhanden ist, soll die UI das sauber anzeigen und keinen kaputten Link rendern.

## Keine Vermischung mit Self-Service-Export

Dieses Bundle ist **kein** Bestandteil des Self-Service-Account-Exports.

Nicht anfassen für dieses Feature:

- Benutzer-Datenexport per E-Mail
- Privacy-Export für einzelne Accounts

Das Exclude-Bundle ist ein operatives Admin-/Pipeline-Artefakt.

## Testanforderungen

Es müssen mindestens folgende Tests ergänzt werden:

1. Pipeline-Run erzeugt zusätzlich `exclude.bundle-*.json`.
2. Export enthält aktive Regeln vollständig und in stabiler Reihenfolge.
3. Export mit `0` Regeln erzeugt gültiges leeres Bundle.
4. Admin kann das Exclude-Bundle herunterladen.
5. Download schlägt sauber fehl, wenn Datei fehlt oder Pfad unsicher ist.
6. Retention löscht verwaiste Exclude-Bundles mit.
7. Backup/Restore berücksichtigt `exclude_bundle_path`.

Betroffene Testdatei primär:

- `tests/test_training_hub.py`

## Nicht-Ziele

- Keine automatische Mod-Synchronisation
- Kein öffentlicher API-Endpunkt für Clients
- Keine zusätzliche statische Datei im Repo als Regelquelle
- Keine Trennung in eine zweite Admin-Regelverwaltung

## Definition of Done

Das Feature ist fertig, wenn:

- ein Trainings-Run immer auch ein `exclude.bundle.json`-Äquivalent erzeugt
- der Export ausschließlich aus `content_scrub_rules` gespeist wird
- Admin das Artefakt pro Run herunterladen kann
- Backups, Retention und Aufräumpfade das zweite Artefakt korrekt behandeln
- Tests die neue Exportkette zuverlässig abdecken
