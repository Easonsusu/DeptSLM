import { FeedbackReviewQueue } from "../../../components/FeedbackReviewQueue";

export default async function DepartmentFeedbackPage({
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
        <span className="phaseBadge">Phase 8</span>
      </nav>
      <FeedbackReviewQueue departmentId={departmentId} />
    </main>
  );
}
