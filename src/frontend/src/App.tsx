import { ChatHost } from './customization/ChatHost';
import { AppAuthGate } from './components/AppAuthGate';
import { AppWorkspace } from './components/AppWorkspace';
import { useAppController } from './hooks/useAppController';
export default function App() {
  const state = useAppController();
  return <AppAuthGate><ChatHost state={state}><AppWorkspace state={state} /></ChatHost></AppAuthGate>;
}
