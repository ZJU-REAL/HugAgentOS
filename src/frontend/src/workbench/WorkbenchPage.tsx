import { createElement, Suspense, type ComponentType, type ReactNode } from 'react';

export function WorkbenchPage<Props extends object>({ registry, applicationId, context, fallback }: {
  registry: { component: (id: string) => ComponentType<Props> };
  applicationId: string;
  context: Props;
  fallback: ReactNode;
}) {
  const Page = registry.component(applicationId);
  return <Suspense fallback={fallback}>{createElement(Page, context)}</Suspense>;
}
