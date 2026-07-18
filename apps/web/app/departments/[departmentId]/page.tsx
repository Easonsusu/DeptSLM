import { RagAnswerPanel } from "../../components/RagAnswerPanel";

export default async function DepartmentAnswerPage({
  params,
}: {
  params: Promise<{ departmentId: string }>;
}) {
  const { departmentId } = await params;
  return (
    <main>
      <nav className="nav" aria-label="Primary navigation">
        <a className="brand" href="/" aria-label="DeptSLM home">
          <span className="brandMark" aria-hidden="true">D</span>
          DeptSLM
        </a>
        <span className="phaseBadge">Phase 7</span>
      </nav>
      <RagAnswerPanel departmentId={departmentId} />
    </main>
  );
}
