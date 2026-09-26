# Remaining CodeQL alerts: 26 September 2026

Live GitHub inventory: **42 open** on default branch `dev`, scanned commit
`c798e4b581f3dd4524d4afac53c2229681a08e1d`. Each current alert was checked
against its source and sink. No query exclusions, permissions, or scan settings
were changed.

## Code fixes

| Alerts | Root cause and correction | Runnable evidence |
|---|---|---|
| 68 | CalDAV probe returned a validator exception; return fixed validation guidance. | `test_caldav_probe_validation_error_is_private` |
| 76, 77 | Teacher endpoint failure inserted an exception into the chat stream and log; return fixed guidance and log its type. | `test_teacher_resolution_error_is_private` |
| 79 | CardDAV import returned and logged validation exception details; use fixed guidance. | `test_carddav_import_validation_error_is_private` |
| 101, 337 | Mail configuration/unsubscribe failures returned arbitrary exceptions; use fixed messages. Sibling list/send/draft/style failure paths corrected too. | `test_send_config_failure_is_private`, `test_unsubscribe_transport_value_error_is_private` |
| 107 | Shared mail authentication helper passed through the raw transport exception; keep Microsoft policy guidance with a fixed fallback. | `test_mail_auth_failure_keeps_provider_guidance_without_raw_error` |
| 112 | PDF failures became document content, reaching memory suggestions and model context; return a fixed failure marker. | `test_pdf_failure_does_not_become_document_content` |
| 124 | Omnigent session transport errors were serialized; preserve the response envelope with a fixed error. | `test_omnigent_session_transport_error_is_private` |
| 130, 131 | Windows background launch and unsupported PTY streams exposed exceptions; retain error codes with fixed text. | `test_background_shell_launch_error_is_private`, `test_unsupported_pty_does_not_return_import_error` |
| 336 | HTML-to-text conversion adopted parsed nodes into the active document, potentially fetching images. Keep nodes inside the inert template and preserve line breaks explicitly. | `tests/test_security_alert_rendering_js.py` |

Named tests above live in `tests/test_remaining_security_alerts.py` unless a
filename is supplied. They inject synthetic errors; no provider/mail/broker
requests or production credentials are needed.

## Individual false-positive evidence

These records support per-alert dismissal, never a rule-wide exclusion.

| Alerts | Verified source-to-sink behavior | Evidence suite |
|---|---|---|
| 9, 10 | Calendar date/time inputs leave helpers only as bounded date/time strings; `_clockFace` checks exact HH:MM before building spans. | `test_security_alert_dom_s6_js.py` |
| 12, 13, 21 | Chat edit/reload rendering passes through the shared Markdown renderer. Raw preserved HTML is scrubbed in an inert template, with handlers, unsafe URLs and script-capable nodes removed before rendering. Parsing itself never attaches the input. | `test_markdown_dom_xss_helpers.py`, `test_markdown_rendering_js.py` |
| 18, 19 | Image previews use cached `URL.createObjectURL(File)` values through DOM properties. Names are `textContent`/`alt`, never interpolated markup. | `test_security_alert_rendering_js.py` |
| 335 | Student input takes the `textContent` branch of `appendMessage`; model replies alone take the shared Markdown branch. CodeQL merges these mutually exclusive calls. | `test_study_practice_coach_js.py`, `test_markdown_dom_xss_helpers.py` |
| 29, 30, 50 | Signature text decoding/stripping is used for heuristics and metadata. `_foldSummary` escapes decoded metadata at its only HTML sink. It is not an HTML sanitizer. | `test_signature_fold_js.py`, `test_signature_fold_self_closing_br_js.py` |
| 64 | Random identifier is an optimistic client map key; it is never sent, persisted, or used for authorization, and is replaced by the server UID. | `test_calendar_temp_uid_js.py` |
| 148 | API keys are Fernet encrypted before JSON storage; readers decrypt. Credential-bearing endpoint URLs are rejected. | `test_embedding_endpoint_config.py` |
| 149 | `effective_user` returns a username string, not an API token/request state; only that string is logged. | `test_chat_helpers.py` |
| 150 | Logs redact the endpoint URL and list header names only; no header values are emitted. | `test_research_endpoint_log_fields.py`, `test_log_safety.py` |
| 151 | Callback log contains only a row id from HMAC-verified state; credential sentinels stay absent. | `test_email_oauth_callback_no_secret_logs.py` |
| 152, 153 | Logs contain confined credential-file paths, not JSON/token payloads. Writes use mode 0600 on POSIX. | `test_mcp_oauth_route_logs.py` |
| 154, 338 | The flagged value is `PASSWORD_MIN_LENGTH = 8`, a public integer constant. No password is printed. | `test_setup_admin_user.py` |
| 203–210 | Assertions about host allowlists, normalization, or rejection messages; no request or credential dispatch. | `test_security_alert_s7_assertion_rows.py` and the eight affected test cases |
| 298 | HMAC-SHA256 with a random key and nonce authenticates OAuth state, verified with `compare_digest`. This is not password hashing. | `test_email_helpers_oauth_state.py` |
| 299 | SHA-256 truncated to eight hex characters is a UI credential label. Endpoint ids select records; the label never authenticates or grants access. | `test_model_routes.py` |

## Local validation

- Disposition evidence suites: **331 passed**.
- Combined security/Study regression checks: **186 passed**.
- Earlier Coach review: **87 passed, 7 environment-dependent skips**.
- `git diff --check`: clean.

GitHub closure must be verified using a fresh scan of the published fix commit.
Per-alert false-positive status is verified after each GitHub write.
