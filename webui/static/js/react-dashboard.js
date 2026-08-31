/* global React, ReactDOM */
(() => {
  'use strict';

  // Compatibility-first React migration. The existing DOM remains the visual
  // contract; React owns navigation state and delegates feature work to the
  // already tested panel adapters. Each panel can move to JSX independently
  // without changing IDs, CSS selectors, modal markup, or API endpoints.
  const mountNode = document.getElementById('react-dashboard-root');
  const nav = document.querySelector('.sidebar-nav');
  if (!mountNode || !nav || !window.React || !window.ReactDOM) return;

  const { createElement, useEffect, useRef, useState } = window.React;
  const tabIds = ['register', 'accounts', 'codex', 'outlook', 'config'];

  function readInitialTab() {
    try {
      const saved = localStorage.getItem('gpt_console_active_tab');
      return tabIds.includes(saved) ? saved : 'register';
    } catch (_) {
      return 'register';
    }
  }

  function setPanelVisibility(activeTab) {
    tabIds.forEach((tab) => {
      const panel = document.getElementById(`tab-${tab}`);
      if (panel) panel.classList.toggle('hidden', tab !== activeTab);
    });
    nav.querySelectorAll('button[data-tab]').forEach((button) => {
      button.classList.toggle('active', button.dataset.tab === activeTab);
    });
  }

  function DashboardShell() {
    const [activeTab, setActiveTab] = useState(readInitialTab);
    const activeRef = useRef(activeTab);
    activeRef.current = activeTab;

    useEffect(() => {
      setPanelVisibility(activeTab);
      try {
        localStorage.setItem('gpt_console_active_tab', activeTab);
      } catch (_) {}
    }, [activeTab]);

    useEffect(() => {
      const onClick = (event) => {
        const button = event.target.closest?.('button[data-tab]');
        if (!button || !nav.contains(button)) return;
        const nextTab = button.dataset.tab;
        if (!tabIds.includes(nextTab)) return;
        // Capture-phase ownership prevents the legacy listener from issuing a
        // second request. React invokes the compatibility adapter exactly once.
        event.preventDefault();
        event.stopPropagation();
        setActiveTab(nextTab);
        if (window.__dashboardLegacy?.activateTab) {
          window.__dashboardLegacy.activateTab(nextTab);
        }
      };
      nav.addEventListener('click', onClick, true);
      return () => nav.removeEventListener('click', onClick, true);
    }, []);

    useEffect(() => {
      const observer = new MutationObserver(() => {
        const selected = nav.querySelector('button[data-tab].active')?.dataset.tab;
        if (selected && selected !== activeRef.current) setActiveTab(selected);
      });
      observer.observe(nav, { subtree: true, attributes: true, attributeFilter: ['class'] });
      return () => observer.disconnect();
    }, []);

    return createElement('span', {
      'data-react-dashboard': 'mounted',
      'data-active-tab': activeTab,
      'aria-hidden': 'true',
    });
  }

  const root = window.ReactDOM.createRoot(mountNode);
  root.render(createElement(DashboardShell));
  window.__reactDashboard = { root, tabIds };
})();
