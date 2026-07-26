import { useState } from 'react'

import WorkflowList from './WorkflowList'
import WorkflowDetail from './WorkflowDetail'

/** Composes the workflow list/builder with the selected version's detail
 * (graph, publish/disable, kill-switch status, schedule/triggers,
 * simulation) -- structural model borrowed from `ScheduleWorkspace.tsx`'s
 * own list-plus-selected-detail shape. */
export default function WorkflowsPanel() {
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null)

  return (
    <div className="schedule-workspace">
      <WorkflowList onSelect={setSelectedVersionId} />
      {selectedVersionId ? <WorkflowDetail versionId={selectedVersionId} /> : null}
    </div>
  )
}
