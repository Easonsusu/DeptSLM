# Security model

## Scope

DeptSLM is a reviewed local-development and research prototype. Phase 13
consolidates the threat model, adds transport and Compose isolation checks, and
provides a synthetic Docker demonstration. It does not certify a production
deployment, provide a formal proof, or add an identity provider, TLS
termination, rate limiting, secret rotation, backups, high availability,
malware scanning, OCR, or trusted training execution.

## 2026 hardening pass

The current hardening branch keeps the Phase 14.2 contracts unchanged while
reducing dependency and local-configuration exposure:

- Qdrant is pinned to server `1.16.3` by image digest and the Python client is
  constrained to `>=1.16.2,<1.17.0`. This is above the `1.15.6` fix for
  [GHSA-f632-vm87-2m2f](https://github.com/advisories/GHSA-f632-vm87-2m2f).
  `DepartmentQdrant`, its typed `DepartmentScope`, fixed collection contract,
  API key, and loopback-only development ports remain in force.
- Next is locked at the latest resolved `15.5.x` patch at the time of this
  pass (`15.5.23`, with `package.json` requiring at least `15.5.21`). CI and
  container builds use frozen lockfile installation. The workspace lock uses
  exact patched overrides for `sharp` `0.35.3`, `postcss` `8.5.26`, and
  `nanoid` `3.3.18`; `pnpm audit --audit-level high` reports no known
  vulnerabilities for the final tree.
- Compose requires `DEPTSLM_POSTGRES_PASSWORD` and derives every internal
  `DATABASE_URL` from that same untracked value. The PostgreSQL development
  port, when enabled, binds only to `127.0.0.1`.
- `DEPTSLM_WEB_DEV_BEARER_TOKEN` is a server-only Next middleware bridge for
  `local`, `development`, and `test`. It is never a `NEXT_PUBLIC_*` value,
  browser storage value, page value, log value, or replacement for FastAPI
  authentication. Unknown and production-like environments fail closed.
- Production Python images install committed exact lockfiles with hashes where
  practical; the CUDA training image retains its reviewed exact platform lock
  and does not perform a mutable pip self-upgrade.
  Node uses `pnpm-lock.yaml` as the authoritative frozen lock. Dependabot,
  full-history secret scanning, dependency auditing, and high/critical
  filesystem scanning run through separate low-noise security automation.

### Model-runtime residual exposure

Transformers remains pinned at `4.55.0` in the reviewed model runtimes. The
current authoritative advisory
[GHSA-29pf-2h5f-8g72](https://github.com/advisories/GHSA-29pf-2h5f-8g72)
affects versions below its `5.3.0` fix and therefore includes `4.55.0` in
general. The relevant exploit requires a runtime to accept attacker-controlled
model/config inputs. DeptSLM closes that caller-controlled path with fixed
model IDs and immutable revisions, descriptor-verified model directories,
no symlink or hard-link substitution, `local_files_only=True`,
`trust_remote_code=False`, safetensors-only loading, offline environment
variables, and no arbitrary CLI, shell, callback, resume path, or hub token.

The pinned model stack also produces exact `pip-audit` associations for the
Transformers conversion/deserialization rows `PYSEC-2025-213` through
`PYSEC-2025-218` (`CVE-2025-14924`, `CVE-2025-14926` through
`CVE-2025-14930`) and `PYSEC-2026-2289` (`CVE-2026-4372`). The current
PyTorch `2.7.1` stack produces `PYSEC-2025-203`, `PYSEC-2025-204`,
`PYSEC-2025-206`, `PYSEC-2026-139`, `PYSEC-2026-1970`, `PYSEC-2026-2286`,
`GHSA-vgrw-7cvw-pwgx`, `GHSA-rrmf-rvhw-rf47`, and
`GHSA-qfhq-4f3w-5fph`; several are local-only or function/version-specific,
but they remain visible in the uploaded audit reports. No compatible upgrade
was accepted for the reviewed LlamaFactory `0.9.5`, PEFT `0.18.1`, Qwen3,
CUDA, and Phase 14.2 runtime contract during this pass. Python audits remain
reporting evidence for these exact model-runtime locks, while the filesystem
gate hard-fails applicable high/critical findings outside the exact model
runtime directories and mixed `apps/api/requirements-ci.lock` exception used
by that scan (the vector-worker lock is likewise excluded as the exact
model-bearing worker lock). This is a narrow documented residual, not a
blanket ignore.

A malicious model cache, malicious administrator-provided model bytes, or a
compromised host remains a residual exposure and must be resolved before a
future runtime phase accepts a compatible patched model stack.

## Protected assets

Authentication secrets, Qdrant keys, internal runtime tokens, PostgreSQL
metadata and audit rows, uploaded bytes, extracted text, vectors, evaluation
suites/results, Phase 10 datasets, Phase 11 bundles, adapter imports and
registry finals, model caches, and deployment/runtime authority are protected.
Contentful runtime assets live under the external `DEPTSLM_DATA_DIR`; source
control contains only code, configuration templates, documentation, and small
synthetic fixtures.

## Trust boundaries

The browser is an untrusted client of the API. The API is the authorization
boundary for PostgreSQL, Qdrant, the base RAG runtime, and the production
adapter runtime. The adapter evaluator has a separate candidate-runtime
boundary. Workers use only their explicitly mounted external storage roots.
Constrained children receive closed descriptors and bounded, secret-free IPC;
they do not receive live database sockets, credentials, or client paths. The
external training environment and imported adapter files are untrusted. Docker
networks and the host filesystem are additional boundaries, while Google Drive
is external storage rather than an authority or backup system.

## Attacker and failure classes

The model covers unauthenticated callers, malicious same-department users,
cross-department callers, same-department `system_admin` users without a
global bypass, malicious documents and prompt-injection content, oversized or
malformed bodies, untrusted evaluation/SFT/adapter artifacts, symlink/hardlink
and path substitution, stale claims and leases, PostgreSQL/filesystem and
Qdrant races, malformed model output, adapter-target confusion, silent
fallback attempts, credential leakage, unnecessary container lateral access,
and accidental disclosure by local operational commands.

## Invariants

| Invariant | Enforcement boundary | Evidence | Residual limitation |
| --- | --- | --- | --- |
| No cross-department access, including `system_admin` | Server membership resolution and exact `DepartmentScope` checks | Phase 2–12 PostgreSQL suites plus the Phase 13 PostgreSQL-backed current-surface route-family matrix (including A-system-admin to B denial) | A compromised host can bypass the application |
| No public vector-search or query-vector route | Route surface and adapter boundaries | API route tests and CI static checks | Internal workers still use Qdrant |
| No document, question, answer, prompt, evidence, or vector in PostgreSQL | Content-free schemas and service boundaries | Phase 4–12 persistence sentinels plus the Phase 13 real RAG transient-content scan | Legitimate source bytes remain external |
| No evaluation generated-answer persistence | Evaluator result allowlist | Phase 9/12.2 tests | Process memory is not a durable secrecy boundary |
| No dataset content in PostgreSQL or APIs | Phase 10 authority manifests | SFT tests and image isolation checks | External dataset operators remain trusted to protect files |
| No adapter bytes in PostgreSQL/API | Registry metadata projection and private mounts | Phase 12.1D–12.4 tests | External registry storage is not transactionally atomic |
| No automatic promotion, rollback, or base fallback | Separate governance and runtime contracts | Phase 12.3/12.4 tests | Operators can still explicitly choose a governance action |
| Exact adapter target fingerprint | Immutable deployment snapshot and runtime admission | Phase 12.4 tests | A compromised model cache is out of scope |
| Exact E-B retention fence | PostgreSQL reservations and lifecycle checks | Phase 12.1E tests | In-flight filesystem/network requests cannot be retroactively fenced |
| Model runtimes receive no DB/Qdrant/auth credentials | Compose mounts, environment allowlists, and private networks | Image and Compose matrix checks | Docker daemon compromise defeats isolation |
| Runtime artifacts stay outside Git | `DEPTSLM_DATA_DIR`, ignore rules, and policy job | Repository artifact-policy job | Local host or Google Drive compromise remains possible |
| Non-upload bodies are bounded | ASGI transport middleware before FastAPI decoding | Phase 13 real FastAPI/PostgreSQL declared-length and streamed-body integration tests, exact-limit and malformed-body regressions | Uploads retain their separate configured streaming limit |
| Secrets and transient RAG content are not disclosed in logs | Safe typed audit fields and content-free runtime boundaries | Phase 13 API/runtime/worker log-sentinel integration scan plus the synthetic Docker demo log scan | Logs are not a cryptographic secrecy boundary |
| The Phase 4 raw upload exemption remains narrow and streaming | Exact UUID upload-path predicate and the existing incremental upload service | Phase 13 real >65,536-byte upload regression and upload-own-limit 413 regression | Upload storage and PostgreSQL commit remain non-atomic |

These controls are reviewed engineering boundaries, not formal verification.

## Residual and out-of-scope risk

Phase 13 does not solve Docker daemon or local-host compromise, Google account
or Drive compromise, atomic cloud synchronization, production DDoS, production
OIDC/OAuth/JWKS/SSO, TLS, secret rotation, coordinated PostgreSQL/Qdrant
backups, clustering, disaster recovery, signed or attested adapter provenance,
post-preparation model supply-chain compromise, malware scanning, or trusted
external training execution. PostgreSQL and external files are not atomically
committed. Runtime failure never triggers automatic base fallback; rollback to
base remains explicit. No automatic model download occurs.

## Phase 14 training-execution boundary

Roadmap v2 Phase 14.0 and Phase 14.1 completed the contract and control plane;
Phase 14.2 adds the first private real-training runtime. The threat model adds
malicious human-authored examples, malicious but schema-valid Phase 11 bundles,
semantic configuration drift, arbitrary CLI or shell injection, environment
inheritance, model-cache substitution, training dependency drift,
path/symlink/hard-link substitution, output substitution, log exfiltration,
external telemetry, accidental model downloads, network exfiltration, disk or
RAM exhaustion, GPU OOM, process explosion, hung/orphan children, cancellation
races, stale leases, host crashes, publication crashes, a compromised training
runtime, and a compromised Docker host.

The Phase 14.2 worker receives only PostgreSQL, exact external training-run
surfaces, and the private runtime IPC token/socket. The private runtime
receives no PostgreSQL, Qdrant, API-auth, membership, RAG, evaluation, adapter,
or cloud credentials. It uses `network_mode: none`, has no public port or
Docker socket, and uses a fixed executable/argv boundary, sanitized offline
environment, process-group supervision, deadlines, byte/process/disk bounds,
and complete tree reaping.
The runtime consumes only a verified server-created snapshot of one approved
Phase 11 bundle and cannot turn filesystem presence or a zero exit code into
authority. Candidate adapter bytes are private and non-authoritative until a
future Phase 14.3 handoff.

Phase 14 provides no hardware or cryptographic attestation. A compromised
host or runtime can invalidate the supervised-execution provenance claim; this
residual risk remains explicitly outside the contract. Real execution,
training dependency pinning, numeric resource bounds, and hardware fingerprints
are reviewed in Phase 14.2. Normal CI downloads no model weights and performs
no real training; qualifying NVIDIA/LoRA/QLoRA validation is an explicit
opt-in gate. Phase 14.3 and Phase 15 have not started.
