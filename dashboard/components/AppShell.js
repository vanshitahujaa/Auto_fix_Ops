'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const NAV = [
  { href: '/incidents', icon: '⚡', label: 'Incidents' },
  { href: '/metrics', icon: '📊', label: 'Metrics' },
  { href: '/chaos', icon: '💥', label: 'Chaos' },
  { href: '/onboard', icon: '⚙️', label: 'Settings' },
];

function TopBar() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    const load = () => api.getStatus().then(setStatus).catch(() => {});
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  if (!status) {
    return (
      <div className="top-bar">
        <span className="loading-spinner" />
      </div>
    );
  }

  const shadowClass = status.shadow_mode === 'true' ? 'shadow' : 'live';
  const cbClass = status.circuit_breaker.toLowerCase().replace('_', '-');
  const ghClass = status.github_connected ? 'connected' : 'disconnected';

  return (
    <div className="top-bar">
      <div className="status-pill" style={{ marginRight: 'auto' }}>
        <span className={`dot ${status.shadow_mode === 'true' ? 'yellow' : 'green'}`} />
        {status.target_namespace}
      </div>

      <span className={`status-pill ${shadowClass}`}>
        {status.shadow_mode === 'true' ? '🛡 SHADOW' : '🟢 LIVE'}
      </span>

      <span className={`status-pill ${cbClass}`}>
        CB: {status.circuit_breaker}
      </span>

      <span className={`status-pill ${ghClass}`}>
        {status.github_connected ? '✓ GitHub' : '✗ GitHub'}
      </span>
    </div>
  );
}

function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <h1>AutoFixOps</h1>
        <span>Control Plane</span>
      </div>
      <nav className="sidebar-nav">
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={pathname?.startsWith(item.href) ? 'active' : ''}
          >
            <span className="nav-icon">{item.icon}</span>
            {item.label}
          </Link>
        ))}
      </nav>
    </aside>
  );
}

export default function AppShell({ children }) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Prevent hydration mismatch by only rendering client-dependent UI after mount
  if (!mounted) {
    return (
      <div className="app-layout">
        <aside className="sidebar">
          <div className="sidebar-logo">
            <h1>AutoFixOps</h1>
            <span>Control Plane</span>
          </div>
          <nav className="sidebar-nav">
            {NAV.map((item) => (
              <a key={item.href} href={item.href}>
                <span className="nav-icon">{item.icon}</span>
                {item.label}
              </a>
            ))}
          </nav>
        </aside>
        <main className="main-content">
          <div className="top-bar">
            <span className="loading-spinner" />
          </div>
          {children}
        </main>
      </div>
    );
  }

  return (
    <div className="app-layout">
      <Sidebar />
      <main className="main-content">
        <TopBar />
        {children}
      </main>
    </div>
  );
}
