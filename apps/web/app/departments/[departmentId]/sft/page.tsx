import { SftDatasetPanel } from "../../../components/SftDatasetPanel";

export default async function DepartmentSftPage({
  params,
}: {
  params: Promise<{ departmentId: string }>;
}) {
  const { departmentId } = await params;
  return (
    <main>
      <nav className="nav" aria-label="Primary navigation">
        <a className="brand" href="/" aria-label="DeptSLM home"><span className="brandMark" aria-hidden="true">D</span>DeptSLM</a>
        <a className="phaseBadge" href={`/departments/${departmentId}`}>Department</a>
      </nav>
      <SftDatasetPanel departmentId={departmentId} />
    </main>
  );
}
