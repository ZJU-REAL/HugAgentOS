import { Component, type ErrorInfo, type ReactNode } from 'react';

import { getLang } from '../../i18n';

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  error: Error | null;
}

/**
 * Last-resort protection for render-time failures.
 *
 * Chat streams can resume after a reload, so keeping a recovery action on
 * screen is safer than allowing one malformed message payload to blank the
 * entire React root.
 */
export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ui] render failed', error, info.componentStack);
  }

  private handleReload = () => {
    window.location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;

    const isEnglish = getLang() === 'en';
    return (
      <main className="jx-appError" role="alert">
        <div className="jx-appError-card">
          <span className="jx-appError-code">UI RECOVERY</span>
          <h1>{isEnglish ? 'The page hit a display error' : '页面显示遇到异常'}</h1>
          <p>
            {isEnglish
              ? 'Your running task is still saved. Reload the page to reconnect and continue.'
              : '正在运行的任务仍会保留。请重新加载页面，系统将重新连接并继续显示。'}
          </p>
          <button type="button" onClick={this.handleReload}>
            {isEnglish ? 'Reload page' : '重新加载'}
          </button>
        </div>
      </main>
    );
  }
}

export default AppErrorBoundary;
