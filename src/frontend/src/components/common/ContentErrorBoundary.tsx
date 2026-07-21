import { Component, type ErrorInfo, type ReactNode } from 'react';

interface ContentErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode;
  resetKey?: string | number;
}

interface ContentErrorBoundaryState {
  error: Error | null;
}

/** Keep one malformed message or side panel from taking down the entire app. */
export class ContentErrorBoundary extends Component<ContentErrorBoundaryProps, ContentErrorBoundaryState> {
  state: ContentErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ContentErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ui] content render failed', error, info.componentStack);
  }

  componentDidUpdate(previousProps: ContentErrorBoundaryProps) {
    if (this.state.error && previousProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    return this.state.error ? this.props.fallback : this.props.children;
  }
}

export default ContentErrorBoundary;
