import { AppAuthGate } from './components/AppAuthGate';
import { AppWorkspace } from './components/AppWorkspace';
import { useAppController } from './hooks/useAppController';
export default function App() {
  const state = useAppController();
  return <AppAuthGate><AppWorkspace state={state} /></AppAuthGate>;
}
