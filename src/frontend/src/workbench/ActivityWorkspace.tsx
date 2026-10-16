import { useMemo, type ReactNode } from 'react';
import { ActivityControls } from './ActivityControls';
import { createActivityExtension, type ActivityContext } from './activityApi';

/** A stable lifecycle owner supplies context. This component never chooses an install or starts a worker. */
export function ActivityWorkspace({context, declared, resourceId, render}: {
  context: ActivityContext; declared: string[]; resourceId?: string;
  render: (extension: ReturnType<typeof createActivityExtension>) => ReactNode;
}) {
  const extension = useMemo(() => createActivityExtension(context), [context]);
  return <>
    <ActivityControls context={context} declared={declared} resourceId={resourceId} />
    {render(extension)}
  </>;
}
