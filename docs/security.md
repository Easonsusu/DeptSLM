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
| No cross-department access, including `system_admin` | Server membership resolution and exact `DepartmentScope` checks | Phase 2–12 PostgreSQL suites and Phase 13 route inventory | A compromised host can bypass the application |
| No public vector-search or query-vector route | Route surface and adapter boundaries | API route tests and CI static checks | Internal workers still use Qdrant |
| No document, question, answer, prompt, evidence, or vector in PostgreSQL | Content-free schemas and service boundaries | Phase 4–12 persistence sentinels | Legitimate source bytes remain external |
| No evaluation generated-answer persistence | Evaluator result allowlist | Phase 9/12.2 tests | Process memory is not a durable secrecy boundary |
| No dataset content in PostgreSQL or APIs | Phase 10 authority manifests | SFT tests and image isolation checks | External dataset operators remain trusted to protect files |
| No adapter bytes in PostgreSQL/API | Registry metadata projection and private mounts | Phase 12.1D–12.4 tests | External registry storage is not transactionally atomic |
| No automatic promotion, rollback, or base fallback | Separate governance and runtime contracts | Phase 12.3/12.4 tests | Operators can still explicitly choose a governance action |
| Exact adapter target fingerprint | Immutable deployment snapshot and runtime admission | Phase 12.4 tests | A compromised model cache is out of scope |
| Exact E-B retention fence | PostgreSQL reservations and lifecycle checks | Phase 12.1E tests | In-flight filesystem/network requests cannot be retroactively fenced |
| Model runtimes receive no DB/Qdrant/auth credentials | Compose mounts, environment allowlists, and private networks | Image and Compose matrix checks | Docker daemon compromise defeats isolation |
| Runtime artifacts stay outside Git | `DEPTSLM_DATA_DIR`, ignore rules, and policy job | Repository artifact-policy job | Local host or Google Drive compromise remains possible |
| Non-upload bodies are bounded | ASGI transport middleware before FastAPI decoding | Phase 13 middleware tests | Uploads retain their separate configured streaming limit |

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
