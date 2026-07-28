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
        <span>
          <a className="phaseBadge" href={`/departments/${departmentId}/feedback`}>Phase 8 review</a>{" "}
          <a className="phaseBadge" href={`/departments/${departmentId}/sft`}>Phase 10 datasets</a>
        </span>
      </nav>
      <RagAnswerPanel departmentId={departmentId} />
    </main>
  );
}
