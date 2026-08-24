# Security model

## Scope

DeptSLM is a reviewed local-development and research prototype. Phase 13
consolidates the threat model, adds transport and Compose isolation checks, and
provides a synthetic Docker demonstration. It does not certify a production
deployment, provide a formal proof, or add an identity provider, TLS
termination, rate limiting, secret rotation, backups, high availability,
malware scanning, OCR, or trusted training execution.

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

The Phase 11 bundle revision and the Phase 14 executor revision are separate
security authorities. `TrainingJob.code_revision` is checked against the
immutable Phase 11 manifest; the executor uses only the exact lowercase SHA
from `DEPTSLM_TRAINING_EXECUTION_CODE_REVISION`. Both are frozen in the
control-plane authority and attempt fingerprints, so a changed bundle or a
changed executor is rejected independently.

Phase 14 provides no hardware or cryptographic attestation. A compromised
host or runtime can invalidate the supervised-execution provenance claim; this
residual risk remains explicitly outside the contract. Real execution,
training dependency pinning, numeric resource bounds, and hardware fingerprints
are reviewed in Phase 14.2. Normal CI downloads no model weights and performs
no real training; qualifying NVIDIA/LoRA/QLoRA validation is an explicit
opt-in gate. Phase 14.3 and Phase 15 have not started.
