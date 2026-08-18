const capabilities = [
  {
    eyebrow: "Grounded answers",
    title: "Search departmental knowledge",
    body: "The reviewed API and worker boundaries keep department metadata, source files, and runtime data isolated.",
  },
  {
    eyebrow: "Department control",
    title: "Keep contexts isolated",
    body: "Every department-owned operation is authorized and filtered on the server.",
  },
  {
    eyebrow: "Purpose-built models",
    title: "Customize a compact SLM",
    body: "The prototype documents reviewed Qwen3, Qdrant, evaluation, dataset, and adapter boundaries without claiming production readiness.",
  },
];

const stack = ["FastAPI", "PostgreSQL", "Qdrant", "Next.js", "External runtime storage"];

export default function Home() {
  return (
    <main>
      <nav className="nav" aria-label="Primary navigation">
        <a className="brand" href="#top" aria-label="DeptSLM home">
          <span className="brandMark" aria-hidden="true">
            D
          </span>
          DeptSLM
        </a>
        <span className="phaseBadge">Local prototype</span>
      </nav>

      <section className="hero" id="top">
        <div className="heroCopy">
          <p className="kicker">Department knowledge, made useful</p>
          <h1>
            Build an AI assistant that understands <span>your department.</span>
          </h1>
          <p className="lede">
            DeptSLM is a reviewed local prototype for department-scoped source,
            retrieval, evaluation, and adapter-governance boundaries.
          </p>
          <div className="heroActions">
            <a className="primaryButton" href="#platform">
              Explore the platform
            </a>
            <p>No production deployment or readiness claim is made.</p>
          </div>
        </div>

        <aside className="systemCard" aria-label="Reviewed DeptSLM boundaries">
          <p className="cardLabel">Reviewed boundaries</p>
          <ol>
            <li>
              <span>01</span>
              <div>
                <strong>Keep runtime data external</strong>
                <p>Source files and generated artifacts stay outside the checkout.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Enforce department scope</strong>
                <p>API and worker operations use server-validated department authority.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Fail closed</strong>
                <p>Retrieval, runtime, and adapter errors never silently broaden access.</p>
              </div>
            </li>
          </ol>
        </aside>
      </section>

      <section className="capabilities" id="platform" aria-labelledby="platform-title">
        <div className="sectionHeading">
          <p className="kicker">One platform, clear boundaries</p>
          <h2 id="platform-title">Designed for responsible departmental customization.</h2>
        </div>
        <div className="cardGrid">
          {capabilities.map((capability, index) => (
            <article className="capabilityCard" key={capability.title}>
              <span className="cardNumber">0{index + 1}</span>
              <p className="cardEyebrow">{capability.eyebrow}</p>
              <h3>{capability.title}</h3>
              <p>{capability.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="stack" aria-labelledby="stack-title">
        <div>
          <p className="kicker">Open foundation</p>
          <h2 id="stack-title">A practical stack for university teams.</h2>
        </div>
        <ul aria-label="Reviewed technology stack">
          {stack.map((technology) => (
            <li key={technology}>{technology}</li>
          ))}
        </ul>
      </section>

      <footer>
        <a className="brand" href="#top">
          <span className="brandMark" aria-hidden="true">
            D
          </span>
          DeptSLM
        </a>
        <p>Source code in GitHub. Runtime data outside the repository.</p>
      </footer>
    </main>
  );
}
