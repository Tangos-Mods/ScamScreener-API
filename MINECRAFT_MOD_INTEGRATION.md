# Minecraft Mod Integration Guide

This document defines the exact request and payload contract a Minecraft mod should follow when uploading training cases to ScamScreener.

It distinguishes between:

- what the server minimally accepts
- what the mod must send if all current UI and admin features should work correctly

This matters because the upload endpoint only validates a small core schema, while the website and admin views render additional fields directly from the stored payload. If those fields are missing, the upload succeeds, but the UI shows `-`, blank values, or empty sections.

Target API base URL:

```text
https://scamscreener.creepans.net
```

## Security rules

- Never ask the player for Minecraft, Microsoft, or Mojang credentials.
- Only send requests over `https://`.
- Do not disable TLS verification.
- Never log raw request payloads, client IDs, or derived handshake hashes without a clear operational need.
- Do not send multipart form data for uploads.
- Do not wrap training cases in an outer JSON object or array.

## Authentication

The mod does not use the web-account login flow anymore. It authenticates only with its local `clientId`.

Normalize the `clientId` exactly like the server:

- trim leading and trailing whitespace
- convert to lowercase

The normalized value is the only mod-side identity for anonymous uploads.

## Upload transport contract

Endpoint:

```text
POST /api/v1/client/uploads/anonymous
```

Send these headers:

```text
Content-Type: application/x-ndjson
X-ScamScreener-Filename: training-cases-v2.jsonl
X-ScamScreener-Client-Id: <normalized clientId>
X-ScamScreener-Payload-Sha256: <sha256(raw request body)>
X-ScamScreener-Handshake-Sha256: <sha256(normalized clientId + ":" + payload sha256)>
User-Agent: ScamScreener/<version>+<mc>
```

Notes:

- The server reads raw request bytes, so the body must be the NDJSON file itself.
- Do not send multipart form data.
- Do not gzip the request unless the server explicitly adds support for it later.
- `X-ScamScreener-Filename` should be a plain file name, not a path.
- The server recalculates both SHA-256 headers. Any mismatch is rejected with `400`.

Request body rules:

- UTF-8 encoded
- one complete JSON object per physical line
- no outer array
- no outer object
- blank lines are ignored
- line breaks inside message text must be escaped as JSON string escapes such as `\n`

This is correct:

```text
{"format":"training_case_v2","schemaVersion":2,"caseId":"case_1"}
{"format":"training_case_v2","schemaVersion":2,"caseId":"case_2"}
```

This is not correct:

```json
[
  {
    "format": "training_case_v2",
    "schemaVersion": 2,
    "caseId": "case_1"
  }
]
```

## Upload responses

Accepted:

```json
{
  "status": "accepted",
  "uploadId": 12,
  "caseCount": 34,
  "acceptedCases": 34,
  "quarantinedCases": 0,
  "insertedCases": 34,
  "updatedCases": 0,
  "sha256": "..."
}
```

Fully quarantined by scrub rules:

```json
{
  "status": "quarantined",
  "uploadId": 13,
  "caseCount": 34,
  "acceptedCases": 0,
  "quarantinedCases": 34,
  "insertedCases": 0,
  "updatedCases": 0,
  "sha256": "..."
}
```

Duplicate for the same client ID:

```json
{
  "status": "duplicate",
  "uploadId": 12,
  "caseCount": 34,
  "acceptedCases": 34,
  "quarantinedCases": 0,
  "sha256": "..."
}
```

Quota exceeded:

```json
{
  "status": "quota-exceeded",
  "detail": "Daily upload count limit reached for this client ID.",
  "caseCount": 34,
  "sha256": "..."
}
```

Relevant status codes:

- `201`: upload accepted
- `202`: upload stored only in quarantine because every case hit a scrub rule
- `200`: duplicate upload for the same client ID
- `400`: invalid UTF-8, invalid JSON, invalid schema, or missing `caseId`
- `413`: upload too large
- `415`: invalid content type
- `429`: upload quota exceeded

## Minimal validation the server enforces

Each non-empty line must be a JSON object containing:

```json
{
  "format": "training_case_v2",
  "schemaVersion": 2,
  "caseId": "case_000001"
}
```

Validation details:

- `format` must be `training_case_v2`
- `schemaVersion` must parse to integer `2`
- `caseId` must be present and non-empty

The server accepts `schemaVersion` as an integer or a numeric string, but the mod should send it as the number `2`.

If you only send the minimum schema, the upload is accepted, but most UI features remain empty.

## What the mod must send for all current features

To make all current case display features work, each case should include:

- `caseData.label`
- `caseData.messages`
- `caseData.caseSignalTagIds`
- `observedPipeline.outcomeAtCapture`
- `observedPipeline.scoreAtCapture`
- `observedPipeline.decidedByStageId`
- `observedPipeline.stageResults`
- `supervision.contextStage.targetLabel`
- `supervision.contextStage.signalMessageIndices`
- `supervision.contextStage.contextMessageIndices`
- `supervision.contextStage.excludedMessageIndices`
- `supervision.contextStage.targetSignalTagIds`

Optional but safe to include:

- `supervision.fixedStageCalibrations`

The current server stores and exports submitted cases as-is, unless an admin-configured content-scrubbing rule matches somewhere in a case during ingestion. Matching cases are discarded from normal ingestion and written to server-side quarantine so admins can export the false positives as a separate bundle for mod tuning. Several pages derive their visible values from these exact fields. If a field is missing, the upload still succeeds; the page simply cannot render that value.

## Field-by-field contract

### `caseData`

Recommended shape:

```json
"caseData": {
  "label": "risk",
  "messages": [
    {
      "index": 0,
      "role": "message",
      "text": "Hello there"
    }
  ],
  "caseSignalTagIds": [
    "middleman-claim",
    "trust-language"
  ]
}
```

Used for:

- case label in list/detail views
- conversation rendering
- case-level signal tags

Requirements:

- `label` should be a non-empty string if you want a visible label
- `messages` should be an array
- `caseSignalTagIds` should be an array of non-empty strings

Supported message field aliases:

- index: `index` or `messageIndex`
- speaker: `role`, `sender`, `author`, `username`, or `source`
- text: `text`, `content`, `message`, `raw`, or `body`

Recommended message object:

```json
{
  "index": 3,
  "role": "message",
  "text": "I can middleman this trade for you."
}
```

If `messages` is missing or empty, the conversation section is empty.

### `observedPipeline`

Recommended shape:

```json
"observedPipeline": {
  "outcomeAtCapture": "review",
  "scoreAtCapture": 0.93,
  "decidedByStageId": "stage.rule",
  "stageResults": [
    {
      "stageId": "stage.rule",
      "outcome": "pass",
      "score": 0.93,
      "reason": "Matched middleman phrasing"
    }
  ]
}
```

Used for:

- stored case outcome
- top-level score display
- decided-by-stage display
- stage results table

Requirements:

- `outcomeAtCapture` should be present if you want a visible outcome
- `scoreAtCapture` should be present if you want the top score to render
- `decidedByStageId` should be present if you want the selected stage shown
- `stageResults` should be an array if you want stage rows displayed

Supported stage result field aliases:

- stage id: `stageId` or `id`
- outcome: `outcome` or `decision`
- score: `score` or `scoreAtStage`
- reason: `reason` or `note`

Recommended stage result object:

```json
{
  "stageId": "stage.context",
  "outcome": "pass",
  "score": 0.52,
  "reason": "Context stage reinforced risk signal"
}
```

If `scoreAtCapture` is missing, the detail page shows `-` for the top score.

If a stage result does not contain `score` or `scoreAtStage`, that row shows `-` in the score column.

If a stage result does not contain `reason` or `note`, that row shows `-` in the reason column.

### `supervision.contextStage`

Recommended shape:

```json
"supervision": {
  "contextStage": {
    "targetLabel": "risk",
    "signalMessageIndices": [1, 4],
    "contextMessageIndices": [0, 2, 3],
    "excludedMessageIndices": [],
    "targetSignalTagIds": [
      "middleman-claim",
      "trust-language"
    ]
  },
  "fixedStageCalibrations": []
}
```

Used for:

- context target label
- signal/context/excluded message lists
- target signal tag export and future compatibility

Requirements:

- `targetLabel` should be a string
- `signalMessageIndices` should be an array of integers
- `contextMessageIndices` should be an array of integers
- `excludedMessageIndices` should be an array of integers
- `targetSignalTagIds` should be an array of strings if your pipeline has them

`fixedStageCalibrations` is currently not required for rendering, but it is safe to include and will remain part of the stored payload.

## Canonical full example

The following object includes all fields needed for the current display features. In the actual `.jsonl` file, serialize it onto a single physical line.

```json
{
  "format": "training_case_v2",
  "schemaVersion": 2,
  "caseId": "case_20260330_000001",
  "caseData": {
    "label": "risk",
    "messages": [
      {
        "index": 0,
        "role": "message",
        "text": "yoyoyo"
      },
      {
        "index": 1,
        "role": "message",
        "text": "i am a legit middleman"
      }
    ],
    "caseSignalTagIds": [
      "middleman-claim",
      "trust-language"
    ]
  },
  "observedPipeline": {
    "outcomeAtCapture": "review",
    "scoreAtCapture": 0.93,
    "decidedByStageId": "stage.rule",
    "stageResults": [
      {
        "stageId": "stage.mute",
        "outcome": "pass",
        "score": 0.01,
        "reason": "No mute evasion indicators"
      },
      {
        "stageId": "stage.player_list",
        "outcome": "pass",
        "score": 0.07,
        "reason": "Player list looked normal"
      },
      {
        "stageId": "stage.rule",
        "outcome": "pass",
        "score": 0.93,
        "reason": "Matched middleman phrasing"
      },
      {
        "stageId": "stage.similarity",
        "outcome": "pass",
        "score": 0.48,
        "reason": "Moderate similarity to known scam examples"
      },
      {
        "stageId": "stage.behavior",
        "outcome": "pass",
        "score": 0.65,
        "reason": "Behavioral pattern suspicious"
      },
      {
        "stageId": "stage.trend",
        "outcome": "pass",
        "score": 0.22,
        "reason": "Low historical trend confidence"
      },
      {
        "stageId": "stage.funnel",
        "outcome": "pass",
        "score": 0.18,
        "reason": "Weak funnel signal"
      },
      {
        "stageId": "stage.context",
        "outcome": "pass",
        "score": 0.52,
        "reason": "Context stage reinforced risk signal"
      }
    ]
  },
  "supervision": {
    "contextStage": {
      "targetLabel": "risk",
      "signalMessageIndices": [
        1
      ],
      "contextMessageIndices": [
        0
      ],
      "excludedMessageIndices": [],
      "targetSignalTagIds": [
        "middleman-claim",
        "trust-language"
      ]
    },
    "fixedStageCalibrations": []
  }
}
```

## Field-to-feature checklist

Use this when a value is missing in the website or admin UI.

- `caseData.label`: visible label in case tables and detail view
- `caseData.messages`: conversation section
- `caseData.caseSignalTagIds`: case signal tags
- `observedPipeline.outcomeAtCapture`: stored outcome and outcome display
- `observedPipeline.scoreAtCapture`: top-level score display
- `observedPipeline.decidedByStageId`: decided-by-stage display
- `observedPipeline.stageResults[].stageId` or `id`: stage ID column
- `observedPipeline.stageResults[].outcome` or `decision`: stage outcome column
- `observedPipeline.stageResults[].score` or `scoreAtStage`: stage score column
- `observedPipeline.stageResults[].reason` or `note`: stage reason column
- `supervision.contextStage.targetLabel`: context target label
- `supervision.contextStage.signalMessageIndices`: signal message indices
- `supervision.contextStage.contextMessageIndices`: context message indices
- `supervision.contextStage.excludedMessageIndices`: excluded message indices
- `supervision.contextStage.targetSignalTagIds`: retained in payload and export, recommended for completeness

## Duplicate and update semantics

These rules affect how the mod should serialize and resend files.

- Duplicate detection is based on the SHA-256 hash of the accepted upload bytes that remain after any scrub-rule quarantine filtering.
- For the same ScamScreener account, uploading byte-identical NDJSON again returns `status=duplicate`.
- Changing any byte changes the hash. This includes whitespace, field order, number formatting, and line order.
- If a different client ID uploads the exact same bytes, the upload is still accepted for that client ID. It is only linked internally as a duplicate of the first upload.
- Case updates are keyed by `caseId`.
- Re-uploading a known `caseId` replaces the stored case summary and payload with the newest version for that `caseId`.
- Do not include the same `caseId` multiple times in one file. Later lines can overwrite earlier lines for that case during ingestion.

If you want stable duplicate behavior, serialize JSON deterministically and keep line ordering stable.

## Recommended client behavior

Recommended lifecycle inside the mod:

1. Generate or load the local ScamScreener `clientId`.
2. Normalize it with `trim().lowercase()`.
3. Make it explicit that players must not enter Minecraft credentials anywhere in the mod.
4. Build the NDJSON payload deterministically.
5. Hash the raw NDJSON bytes with SHA-256.
6. Hash `normalizedClientId + ":" + payloadSha256` with SHA-256.
7. Upload the raw bytes to `/api/v1/client/uploads/anonymous`.
8. On `400`, keep the rejected payload and header values for developer inspection.
9. On shutdown, clear any transient upload buffers that should not remain on disk.

Recommended error handling:

- `400` on upload: keep the rejected payload for developer inspection
- `413` on upload: split or reduce the file before retrying
- `415` on upload: fix the request content type
- `429` on upload: retry later, do not spam retries

## Short summary

If you only want the upload to pass validation, send:

- `format`
- `schemaVersion`
- `caseId`

If you want all current ScamScreener case display features to work, also send:

- `caseData.label`
- `caseData.messages`
- `caseData.caseSignalTagIds`
- `observedPipeline.outcomeAtCapture`
- `observedPipeline.scoreAtCapture`
- `observedPipeline.decidedByStageId`
- `observedPipeline.stageResults[].stageId` or `id`
- `observedPipeline.stageResults[].outcome` or `decision`
- `observedPipeline.stageResults[].score` or `scoreAtStage`
- `observedPipeline.stageResults[].reason` or `note`
- `supervision.contextStage.*`

If scores are showing as `-`, the mod is not sending the score fields under `observedPipeline`.
