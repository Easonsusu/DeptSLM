import { TrainingJobPanel } from "../../../components/TrainingJobPanel";

export default async function TrainingJobsPage({
  params,
}: {
  params: Promise<{ departmentId: string }>;
}) {
  const { departmentId } = await params;
  return <main><TrainingJobPanel departmentId={departmentId} /></main>;
}
