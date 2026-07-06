import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Root } from './Root';
import { BackendGate } from './screens/BackendGate';
import './styles/tokens.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BackendGate>
      <Root />
    </BackendGate>
  </StrictMode>
);
