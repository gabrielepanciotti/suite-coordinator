import { useCallback, useState } from "react";

const STORAGE_KEY = "coordinator.collapsedNodes";

function load() {
  try {
    return new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function persist(set) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...set]));
  } catch {
    /* ignore quota / private-mode errors */
  }
}

// Stato condiviso dei nodi compressi: persiste in localStorage e consente
// azioni per-nodo (toggle) e globali (collapseAll/expandAll).
export function useCollapsedNodes() {
  const [collapsed, setCollapsed] = useState(load);

  const toggle = useCallback((nodeKey) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.has(nodeKey) ? next.delete(nodeKey) : next.add(nodeKey);
      persist(next);
      return next;
    });
  }, []);

  const collapseAll = useCallback((keys) => {
    const next = new Set(keys);
    persist(next);
    setCollapsed(next);
  }, []);

  const expandAll = useCallback(() => {
    const next = new Set();
    persist(next);
    setCollapsed(next);
  }, []);

  return { collapsed, toggle, collapseAll, expandAll };
}
