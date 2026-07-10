const capabilities = [
  {
    eyebrow: "Grounded answers",
    title: "Search departmental knowledge",
    body: "Planned RAG workflows will connect approved documents to clear, source-aware answers.",
  },
  {
    eyebrow: "Department control",
    title: "Keep contexts isolated",
    body: "Each department will manage its own documents, indexes, evaluations, and model adapter.",
  },
  {
    eyebrow: "Purpose-built models",
    title: "Customize a compact SLM",
    body: "Qwen3, Qwen3-Embedding, and LoRA are the target stack for efficient specialization.",
  },
];

const stack = ["Qwen3", "Qwen3-Embedding", "LlamaIndex", "Qdrant", "LLaMA-Factory"];

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
        <span className="phaseBadge">Phase 0</span>
      </nav>

      <section className="hero" id="top">
        <div className="heroCopy">
          <p className="kicker">Department knowledge, made useful</p>
          <h1>
            Build an AI assistant that understands <span>your department.</span>
          </h1>
          <p className="lede">
            DeptSLM is being designed as an open platform for creating grounded,
            department-specific assistants with isolated institutional knowledge.
          </p>
          <div className="heroActions">
            <a className="primaryButton" href="#platform">
              Explore the platform
            </a>
            <p>Project initialization is in progress.</p>
          </div>
        </div>

        <aside className="systemCard" aria-label="Planned DeptSLM workflow">
          <p className="cardLabel">Planned workflow</p>
          <ol>
            <li>
              <span>01</span>
              <div>
                <strong>Bring trusted material</strong>
                <p>Department-approved documents stay in external runtime storage.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Build department context</strong>
                <p>Index knowledge and prepare carefully scoped training data.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Answer with evidence</strong>
                <p>Retrieve relevant sources before the assistant responds.</p>
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
        <ul aria-label="Target technology stack">
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
