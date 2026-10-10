import { createBrowserRouter,Navigate } from 'react-router';
import { AutomationChatRoute,ChatRoute,Shell } from './RouteSync';
import { AUTOMATION_CHAT_PATTERN,CHAT_PATTERN,ROUTED_PANELS } from './navigation';

// 面板路由只负责让地址存在（面板由 usePanel 从地址算），所以元素是空的。
const routes = [
  {
    path: '/',
    element: <Shell />,
    children: [
      { index: true, element: <ChatRoute /> },
      { path: CHAT_PATTERN.slice(1), element: <ChatRoute /> },
      { path: AUTOMATION_CHAT_PATTERN.slice(1), element: <AutomationChatRoute /> },
      // 每个面板都再开一层：`/<面板>/<二级页>`（能力中心类别、我的空间模块、
      // 设置子菜单、定时任务的某个任务…）
      ...ROUTED_PANELS.flatMap(([, slug]) => [
        { path: slug, element: null },
        ...(slug === 'projects' ? [] : [
          { path: `${slug}/:sub`, element: null },
          { path: `${slug}/:sub/:sub2`, element: null },
        ]),
      ]),
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
];

/** 建 router 会立刻 initialize（装 popstate 监听、匹配一次当前地址），所以只在真正要挂
 *  RouterProvider 的主聊天入口调用；/admin、/config 等入口共用这个模块但不该付这份开销。 */
export function createAppRouter() {
  return createBrowserRouter(routes);
}
