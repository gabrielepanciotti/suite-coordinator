import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

// `instance` is the selected Life instance id, or null for the "tutte" view.
export function useNotifications(instance = null) {
  const [items, setItems] = useState([]);
  const [meta, setMeta] = useState(null);
  const [instances, setInstances] = useState([]);
  const [defaultInstance, setDefaultInstance] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, m, inst] = await Promise.all([
        api.list(instance),
        api.meta(),
        api.instances(),
      ]);
      setItems(list.notifications);
      setMeta(m);
      setInstances(inst.instances || []);
      setDefaultInstance(inst.default ?? null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [instance]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { items, meta, instances, defaultInstance, loading, error, reload };
}
