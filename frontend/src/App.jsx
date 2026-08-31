import { useEffect, useState } from 'react';

const tabs = [
  ['register', '注册'],
  ['accounts', '账号'],
  ['codex', 'Codex 授权'],
  ['outlook', '邮箱池'],
  ['config', '配置'],
];

export default function App() {
  const [activeTab, setActiveTab] = useState(() => {
    const saved = window.localStorage.getItem('gpt_console_active_tab');
    return tabs.some(([id]) => id === saved) ? saved : 'register';
  });

  useEffect(() => {
    window.localStorage.setItem('gpt_console_active_tab', activeTab);
    window.dispatchEvent(new CustomEvent('dashboard-tab-change', { detail: activeTab }));
  }, [activeTab]);

  return (
    <nav className="sidebar-nav" aria-label="主导航">
      {tabs.map(([id, label]) => (
        <button
          key={id}
          type="button"
          data-tab={id}
          className={`sidebar-item${activeTab === id ? ' active' : ''}`}
          onClick={() => setActiveTab(id)}
        >
          <span className="sidebar-item-label">{label}</span>
        </button>
      ))}
    </nav>
  );
}
