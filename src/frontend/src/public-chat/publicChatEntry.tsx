import { createBrowserRouter, RouterProvider } from 'react-router';
import { bindRouter } from '../routing/navigation';
import PublicChatApp from './PublicChatApp';

export function createPublicChatEntry() {
  const router = createBrowserRouter([
    { path: '/chat/:kind/:resourceId', element: <PublicChatApp /> },
    { path: '/chat/:kind/:resourceId/c/:chatId', element: <PublicChatApp /> },
  ]);
  bindRouter(router);
  return () => <RouterProvider router={router} />;
}
