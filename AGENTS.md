# AGENTS.md

## Purpose

This repository must be treated as production software by default. Any code, configuration, infrastructure, automation, documentation, or generated artifact created for this project must be production ready unless a task explicitly states otherwise.

## Core Rule

Do not produce prototype-grade, demo-grade, placeholder, or "good enough for now" implementations.
Do not optimize for speed of delivery at the expense of correctness, security, maintainability, or operability.
Assume the code may be deployed, exposed to real users, connected to real data, and maintained long term.

## Mandatory Engineering Standard

When generating or modifying anything in this repository, always apply the following standards:

### 1. Production readiness

* Deliver complete implementations, not stubs disguised as finished work.
* Avoid mock logic in production paths.
* Ensure the result is coherent, executable, and integrated into the existing architecture.
* Include all necessary wiring, imports, configuration, and error handling.
* Prefer explicitness over cleverness.

### 2. Security by default

* Follow secure-by-default design in every layer.
* Validate all inputs strictly.
* Sanitize and encode untrusted data where required.
* Prevent common vulnerabilities including, but not limited to:

  * injection attacks
  * command injection
  * SQL/NoSQL injection
  * path traversal
  * SSRF
  * XSS
  * CSRF where applicable
  * insecure deserialization
  * broken authentication and authorization
  * secret leakage
  * insecure defaults
  * race-condition-based privilege issues
* Use least privilege for permissions, tokens, and runtime capabilities.
* Never hardcode secrets, API keys, credentials, or tokens.
* Use environment variables or approved secret-management mechanisms.
* Fail safely, not openly.

### 3. Authentication and authorization

* Enforce authentication where access is not explicitly public.
* Enforce authorization server-side.
* Never trust client-side role checks alone.
* Deny by default when access rules are unclear.
* Scope tokens and sessions minimally.

### 4. Data protection

* Minimize collection and retention of sensitive data.
* Protect sensitive data in transit and at rest where applicable.
* Avoid logging personal data, secrets, access tokens, raw credentials, or internal security details.
* Redact sensitive values in logs, traces, and error messages.
* Use safe defaults for privacy and retention.

### 5. Dependency hygiene

* Prefer mature, well-maintained, widely trusted libraries.
* Avoid unnecessary dependencies.
* Do not introduce abandoned or suspicious packages.
* Pin versions appropriately.
* Keep supply-chain risk in mind.
* When replacing native functionality with a dependency, justify the tradeoff in code comments or documentation if not obvious.

### 6. Reliability and resilience

* Handle expected failure modes explicitly.
* Use timeouts, retries, backoff, and circuit-breaking patterns where appropriate.
* Do not swallow exceptions silently.
* Produce actionable error messages internally, but do not leak sensitive internals externally.
* Design for recoverability.
* Avoid single points of failure where reasonable.

### 7. Observability

* Add meaningful logging for operational visibility.
* Logs must support debugging and auditing without exposing secrets.
* Include metrics, health checks, or tracing hooks where appropriate.
* Make failures diagnosable.

### 8. Maintainability

* Write clean, readable, idiomatic code.
* Use clear naming.
* Keep functions and modules focused.
* Avoid duplication.
* Document non-obvious decisions.
* Match the repository’s conventions unless those conventions are unsafe.

### 9. Testing

* Provide or update meaningful tests for the change.
* Cover success paths, failure paths, and security-relevant behavior.
* Do not claim code is done if it lacks reasonable test coverage for critical logic.
* Prefer deterministic tests.

### 10. Performance and scalability

* Avoid obviously inefficient approaches.
* Consider expected scale, load, and data volume.
* Prevent unnecessary memory growth, blocking behavior, and N+1 patterns.
* Use pagination, batching, streaming, caching, and concurrency controls where appropriate.

### 11. Configuration and deployment

* Keep configuration environment-specific and externalized.
* Ensure sane, secure defaults.
* Do not assume local development settings are safe for production.
* Support repeatable deployment and startup.
* Include migration steps or rollout notes when relevant.

### 12. Documentation quality

* Update documentation when behavior, configuration, APIs, or operational procedures change.
* Keep README, runbooks, and inline docs aligned with reality.
* Do not document features that do not exist.

## Output Expectations for Codex

When implementing a task, Codex must:

* think beyond just making the code work
* identify production risks and address them in the implementation
* close obvious security gaps even if the prompt does not mention them explicitly
* avoid unsafe shortcuts
* avoid TODOs for critical production or security concerns
* state clearly when a request cannot be completed safely as asked
* prefer a smaller correct solution over a larger fragile one

## Forbidden Shortcuts

Do not:

* hardcode credentials or secrets
* disable security checks to make something work
* bypass certificate validation without explicit documented justification
* use wildcard CORS in sensitive contexts without justification
* trust user input by default
* expose internal stack traces to end users
* leave admin/debug endpoints exposed in production paths
* ship placeholder auth, placeholder crypto, or fake validation
* mark insecure code as production ready

## If Requirements Conflict

If a user request conflicts with production readiness, security, privacy, compliance, or safe operation:

1. do not implement the unsafe version silently
2. choose the safest viable implementation
3. explain the constraint briefly in comments or accompanying notes if needed

## Definition of Done

A task is only done when the result is:

* production ready
* secure by default
* maintainable
* tested appropriately
* operationally sane
* consistent with current best practices

If those conditions are not met, the task is not complete.
