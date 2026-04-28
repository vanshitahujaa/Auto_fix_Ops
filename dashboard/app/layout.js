import './globals.css';
import AppShell from '../components/AppShell';

export const metadata = {
  title: 'AutoFixOps — Control Plane',
  description: 'Human-in-the-loop control plane for autonomous Kubernetes remediation',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
