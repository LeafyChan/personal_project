/**
 * ActivityLog.jsx
 * ===============
 * Shows a chronological feed of every change made in this org — invoice
 * field edits, line item edits/adds/deletes, and settings changes (Drive
 * folder, business description, HSN profile) — whether made by a human
 * user or by the AI pipeline (e.g. applying a generated HSN profile).
 *
 * Backed by GET /activity-log (filters: entity_type, actor_type, page).
 * Every row already exists server-side via log_activity() calls added
 * alongside each mutating endpoint — this page is purely a read view.
 *
 * Props:
 *   getToken — async () => string, from Clerk's useAuth()
 */

import { useState, useEffect, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";
const PAGE_SIZE = 50;

const ENTITY_META = {
  invoice:            { label: "Invoice",       icon: "📄" },
  line_item:          { label: "Line item",     icon: "🧾" },
  drive_folder:       { label: "Drive folder",  icon: "📁" },
  business_description:{ label: "Business profile", icon: "🏢" },
  hsn_profile:        { label: "HSN profile",   icon: "🏷" },
  hsn_profile_code:   { label: "HSN code",      icon: "🏷" },
};

const ACTION_LABEL = {
  invoice_field_edit: "edited",
  line_item_edit:     "edited",
  line_item_add:      "added",
  line_item_delete:   "deleted",
  settings_update:    "updated",
  hsn_profile_apply:  "applied",
  hsn_code_add:       "added",
  hsn_code_remove:    "removed",
};

function fmtDatetime(d) {
  if (!d) return "—";
  return new Date(d).toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function ActorBadge({ actorType }) {
  const isAi = actorType === "ai";
  return (
    <span style={{
      fontSize: 10, fontWeight: 600, padding: "2px 7px", borderRadius: 4,
      textTransform: "uppercase", letterSpacing: "0.04em", whiteSpace: "nowrap",
      background: isAi ? "#EEF2FF" : "#F0FDF4",
      color: isAi ? "#3730A3" : "#15803D",
    }}>
      {isAi ? "🤖 AI" : "👤 You"}
    </span>
  );
}

function EntryRow({ entry }) {
  const meta = ENTITY_META[entry.entity_type] || { label: entry.entity_type, icon: "•" };
  const actionLabel = ACTION_LABEL[entry.action] || entry.action;
  return (
    <div style={s.row}>
      <div style={s.rowIcon}>{meta.icon}</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={s.rowTop}>
          <span style={s.rowEntity}>{meta.label}</span>
          <span style={s.rowAction}>{actionLabel}</span>
          <ActorBadge actorType={entry.actor_type} />
          <span style={s.rowTime}>{fmtDatetime(entry.created_at)}</span>
        </div>
        <div style={s.rowSummary}>
          {entry.summary || `${entry.field_name || ""} changed`}
        </div>
        {entry.field_name && (entry.old_value != null || entry.new_value != null) && (
          <div style={s.rowDiff}>
            <span style={s.rowField}>{entry.field_name}</span>
            <span style={s.diffOld}>{entry.old_value ?? "—"}</span>
            <span style={s.diffArrow}>→</span>
            <span style={s.diffNew}>{entry.new_value ?? "—"}</span>
          </div>
        )}
      </div>
    </div>
  );
}

export default function ActivityLog({ getToken }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr]         = useState(null);
  const [page, setPage]       = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [entityFilter, setEntityFilter] = useState("");
  const [actorFilter, setActorFilter]   = useState("");

  const fetchLog = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const tok = await getToken();
      const params = new URLSearchParams({ page, page_size: PAGE_SIZE });
      if (entityFilter) params.set("entity_type", entityFilter);
      if (actorFilter) params.set("actor_type", actorFilter);
      const res = await fetch(`${API_BASE}/activity-log?${params}`, {
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      setEntries(d.entries);
      setTotalPages(d.total_pages);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [getToken, page, entityFilter, actorFilter]);

  useEffect(() => { fetchLog(); }, [fetchLog]);

  // Group entries by calendar day for a scannable timeline
  const grouped = entries.reduce((acc, e) => {
    const day = new Date(e.created_at).toLocaleDateString("en-IN", {
      day: "2-digit", month: "short", year: "numeric",
    });
    (acc[day] = acc[day] || []).push(e);
    return acc;
  }, {});

  const s = styles;

  return (
    <div style={s.root}>
      <div style={s.header}>
        <div>
          <h2 style={s.heading}>Activity log</h2>
          <p style={s.subheading}>
            Every change made in this workspace — by you or automatically by the AI pipeline.
          </p>
        </div>
        <div style={s.filters}>
          <select value={entityFilter} onChange={e => { setEntityFilter(e.target.value); setPage(1); }} style={s.select}>
            <option value="">All areas</option>
            <option value="invoice">Invoices</option>
            <option value="line_item">Line items</option>
            <option value="drive_folder">Drive folder</option>
            <option value="business_description">Business profile</option>
            <option value="hsn_profile">HSN profile (applied)</option>
            <option value="hsn_profile_code">HSN codes</option>
          </select>
          <select value={actorFilter} onChange={e => { setActorFilter(e.target.value); setPage(1); }} style={s.select}>
            <option value="">User &amp; AI</option>
            <option value="user">User only</option>
            <option value="ai">AI only</option>
          </select>
          <button onClick={fetchLog} disabled={loading} style={s.refreshBtn} title="Refresh">↻ Refresh</button>
        </div>
      </div>

      {err && (
        <div style={s.errBanner}>
          Failed to load activity log: {err}
          <button onClick={fetchLog} style={s.retryBtn}>Retry</button>
        </div>
      )}

      {loading && entries.length === 0 && <div style={s.loadingBox}>Loading…</div>}

      {!loading && entries.length === 0 && !err && (
        <div style={s.emptyBox}>
          Nothing logged yet. Edits to invoices, line items, and settings will show up here.
        </div>
      )}

      {Object.entries(grouped).map(([day, dayEntries]) => (
        <div key={day} style={{ marginBottom: 24 }}>
          <div style={s.dayHeading}>{day}</div>
          <div style={s.dayCard}>
            {dayEntries.map(entry => <EntryRow key={entry.log_id} entry={entry} />)}
          </div>
        </div>
      ))}

      {totalPages > 1 && (
        <div style={s.pagination}>
          <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1} style={s.pageBtn}>← Newer</button>
          <span style={{ fontSize: 12, color: "#6B7280" }}>Page {page} of {totalPages}</span>
          <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page >= totalPages} style={s.pageBtn}>Older →</button>
        </div>
      )}
    </div>
  );
}

const styles = {
  root: {
    flex: 1,
    overflow: "auto",
    background: "#F7F8FA",
    padding: "28px 24px",
    fontFamily: "Inter, system-ui, -apple-system, sans-serif",
    fontSize: 13,
    color: "#111318",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    flexWrap: "wrap",
    gap: 16,
    marginBottom: 22,
  },
  heading: { margin: "0 0 4px", fontSize: 18, fontWeight: 700, color: "#111318", letterSpacing: "-0.01em" },
  subheading: { margin: 0, fontSize: 13, color: "#6B7280" },
  filters: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" },
  select: {
    border: "1px solid #D1D5DB", borderRadius: 6, padding: "6px 10px",
    fontSize: 12, fontFamily: "inherit", background: "#fff", color: "#111318", cursor: "pointer",
  },
  refreshBtn: {
    background: "#fff", border: "1px solid #312E81", borderRadius: 6, color: "#312E81",
    fontSize: 12, fontWeight: 500, padding: "6px 12px", cursor: "pointer",
    fontFamily: "inherit", whiteSpace: "nowrap",
  },
  errBanner: {
    background: "#FEF2F2", border: "1px solid #FECACA", borderRadius: 8, color: "#B91C1C",
    padding: "10px 14px", marginBottom: 16, fontSize: 13, display: "flex", alignItems: "center", gap: 12,
  },
  retryBtn: {
    background: "none", border: "1px solid #B91C1C", borderRadius: 6, color: "#B91C1C",
    padding: "3px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit",
  },
  loadingBox: { color: "#9CA3AF", fontSize: 13, padding: "40px 0", textAlign: "center" },
  emptyBox: {
    color: "#9CA3AF", fontSize: 13, padding: "48px 24px", textAlign: "center",
    background: "#fff", border: "1px solid #E5E7EB", borderRadius: 10,
  },
  dayHeading: {
    fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em",
    color: "#6B7280", marginBottom: 8,
  },
  dayCard: {
    background: "#fff", border: "1px solid #E5E7EB", borderRadius: 10, overflow: "hidden",
  },
  row: {
    display: "flex", gap: 12, padding: "12px 16px", borderBottom: "1px solid #F3F4F6",
  },
  rowIcon: { fontSize: 16, lineHeight: "20px", flexShrink: 0 },
  rowTop: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 3 },
  rowEntity: { fontSize: 12, fontWeight: 600, color: "#111318" },
  rowAction: { fontSize: 12, color: "#6B7280" },
  rowTime: { fontSize: 11, color: "#9CA3AF", marginLeft: "auto" },
  rowSummary: { fontSize: 12, color: "#374151", lineHeight: 1.5 },
  rowDiff: {
    display: "flex", alignItems: "center", gap: 6, marginTop: 4, fontSize: 11,
    fontFamily: "monospace", flexWrap: "wrap",
  },
  rowField: {
    color: "#6B7280", textTransform: "uppercase", letterSpacing: "0.03em", fontSize: 10,
    fontFamily: "Inter, system-ui, sans-serif", fontWeight: 600,
  },
  diffOld: { color: "#B91C1C", background: "#FEF2F2", padding: "1px 6px", borderRadius: 3, textDecoration: "line-through" },
  diffArrow: { color: "#9CA3AF" },
  diffNew: { color: "#15803D", background: "#F0FDF4", padding: "1px 6px", borderRadius: 3 },
  pagination: {
    display: "flex", alignItems: "center", justifyContent: "center", gap: 16,
    padding: "14px 0", marginTop: 8,
  },
  pageBtn: {
    background: "#fff", border: "1px solid #D1D5DB", borderRadius: 6, padding: "5px 14px",
    fontSize: 13, cursor: "pointer", fontFamily: "inherit", color: "#374151",
  },
};