/**
 * InvoiceList.jsx
 * ===============
 * Phase 3 frontend — list view for uploaded invoices.
 *
 * Drop this into webapp/frontend/src/InvoiceList.jsx
 * Then wire it into App.jsx (see bottom of this file for the integration snippet).
 *
 * Props:
 *   getToken  — async () => string, from Clerk's useAuth()
 *   orgId     — string, from useOrganization()
 *
 * Talks to:
 *   GET /invoices?status=&search=&sort_by=&sort_dir=&page=&page_size=
 *   GET /invoices/:id  (on row click → detail panel)
 */

import { useState, useEffect, useCallback, useRef } from "react";
import ReviewModal from "./ReviewModal";
import { shouldAutoReview } from "./InvoiceReviewUtils";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";
const PAGE_SIZE = 50;

// ── Status badge metadata ────────────────────────────────────────────────────
const STATUS_META = {
  PASSED:              { label: "Passed",       bg: "#DCFCE7", color: "#15803D" },
  WARNING:             { label: "Warning",      bg: "#FEF9C3", color: "#854D0E" },
  FAILED:              { label: "Failed",       bg: "#FEE2E2", color: "#B91C1C" },
  NEEDS_MANUAL_REVIEW: { label: "Needs review", bg: "#E0E7FF", color: "#3730A3" },
};

function statusMeta(s) {
  return STATUS_META[s] || { label: s ?? "Unknown", bg: "#F3F4F6", color: "#374151" };
}

// ── Formatters ───────────────────────────────────────────────────────────────
function fmtAmount(n) {
  if (n == null || n === "" || n === "—") return "—";
  const num = typeof n === "string" ? parseFloat(n) : Number(n);
  if (isNaN(num)) return "—";
  // en-IN locale: uses Indian numbering (1,00,000 style), 2 decimal places
  return "₹" + num.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDate(d) {
  if (!d) return "—";
  return new Date(d).toLocaleDateString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
  });
}

function fmtDatetime(d) {
  if (!d) return "—";
  return new Date(d).toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

// ── Sub-components ───────────────────────────────────────────────────────────

function StatusBadge({ status }) {
  const m = statusMeta(status);
  return (
    <span style={{
      display: "inline-block",
      padding: "2px 8px",
      borderRadius: 4,
      fontSize: 11,
      fontWeight: 600,
      letterSpacing: "0.03em",
      textTransform: "uppercase",
      background: m.bg,
      color: m.color,
      whiteSpace: "nowrap",
    }}>
      {m.label}
    </span>
  );
}

function SortIcon({ col, active, dir }) {
  if (!active) return <span style={{ opacity: 0.25, marginLeft: 4 }}>↕</span>;
  return <span style={{ marginLeft: 4 }}>{dir === "asc" ? "↑" : "↓"}</span>;
}

function Spinner() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: 48, color: "#6B7280" }}>
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" style={{ animation: "spin 0.8s linear infinite" }}>
        <circle cx="12" cy="12" r="10" stroke="#E5E7EB" strokeWidth="3" />
        <path d="M12 2a10 10 0 0 1 10 10" stroke="#312E81" strokeWidth="3" strokeLinecap="round" />
      </svg>
      <span style={{ marginLeft: 10, fontSize: 13 }}>Loading invoices…</span>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

// ── Detail panel ─────────────────────────────────────────────────────────────

// HSN classification badge: shows if this HSN is expected/unusual/unclassified
// for this org's business profile.
//
// profile.expected_codes / profile.ambiguous_codes are arrays of OBJECTS
// ({code, code_type, description, ...}) — not arrays of plain code strings —
// so matching needs .some(c => c.code === code), not .includes(code).
// ── ITC eligibility — Section 17(5) CGST Act blocked credit patterns ────────
// Checks HSN/SAC codes against known blocked-credit categories. This is a
// triage aid, not a legal determination — the business owner should confirm
// before claiming or reversing ITC on any line marked blocked.
const _ITC_BLOCKED_RULES = [
  [/^87(?!07|08|14)/, "Motor vehicles (Ch. 87) — blocked under Sec. 17(5)(a) unless for resale, transport, or demo fleet"],
  [/^9963/,           "Restaurant/catering/food services — blocked for employee consumption under Sec. 17(5)(b)"],
  [/^9954/,           "Works contract / civil construction for immovable property — blocked under Sec. 17(5)(c)"],
  [/^9956/,           "Club, fitness, or recreational services — blocked under Sec. 17(5)(d)"],
  [/^99712|^99713/,   "Life/health insurance for employees — blocked under Sec. 17(5)(b)"],
  [/^996511/,         "Air/rail travel benefits for employees — blocked under Sec. 17(5)(d)"],
];

function getItcEligibility(hsnCode, profile) {
  if (!hsnCode) return { status: "none" };
  const c = String(hsnCode).trim();
  for (const [pattern, reason] of _ITC_BLOCKED_RULES) {
    if (pattern.test(c)) return { status: "blocked", reason };
  }
  const isExpected  = profile?.expected_hsn_codes?.some(p => p.code === c);
  const isAmbiguous = profile?.ambiguous_hsn_codes?.some(p => p.code === c);
  if (isExpected)  return { status: "eligible",  reason: "In your HSN profile — expected business purchase" };
  if (isAmbiguous) return { status: "review",    reason: "Ambiguous — verify actual use before claiming" };
  return { status: "unknown", reason: "Not in your HSN profile — add in Settings → Business profile" };
}

function ItcEligibilityBadge({ code, profile }) {
  const el = getItcEligibility(code, profile);
  const cfg = {
    eligible: { bg: "#DCFCE7", color: "#15803D", text: "✓ ITC OK"       },
    blocked:  { bg: "#FEE2E2", color: "#B91C1C", text: "✗ ITC blocked"  },
    review:   { bg: "#FEF9C3", color: "#854D0E", text: "⚠ Verify use"   },
    unknown:  { bg: "#F3F4F6", color: "#6B7280", text: "? Unclassified" },
    none:     null,
  }[el.status];
  if (!cfg) return null;
  return (
    <span title={el.reason} style={{ fontSize: 10, borderRadius: 3, padding: "1px 5px",
      background: cfg.bg, color: cfg.color, cursor: "help", whiteSpace: "nowrap" }}>
      {cfg.text}
    </span>
  );
}

function HsnBadge({ code, profile }) {
  if (!code || !profile) return null;
  // GET /org/hsn-profile returns expected_hsn_codes / ambiguous_hsn_codes
  const isExpected = profile.expected_hsn_codes?.some(c => c.code === code);
  const isAmbiguous = profile.ambiguous_hsn_codes?.some(c => c.code === code);
  if (isExpected) return (
    <span style={{ fontSize: 10, background: "#DCFCE7", color: "#15803D", borderRadius: 3, padding: "1px 5px", marginLeft: 4 }}>
      ✓ expected
    </span>
  );
  if (isAmbiguous) return (
    <span style={{ fontSize: 10, background: "#FEF9C3", color: "#854D0E", borderRadius: 3, padding: "1px 5px", marginLeft: 4 }}>
      ? verify use
    </span>
  );
  return (
    <span style={{ fontSize: 10, background: "#F3F4F6", color: "#6B7280", borderRadius: 3, padding: "1px 5px", marginLeft: 4 }}>
      unknown
    </span>
  );
}

function DetailPanel({ invoiceId, getToken, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  // "collapsed" = 44px strip | "normal" = 400px | "expanded" = 680px
  const [panelSize, setPanelSize] = useState("normal");
  // business_use_pct editing
  const [bupDraft, setBupDraft] = useState({});
  const [bupSaving, setBupSaving] = useState({});
  // HSN org profile (fetched once per panel open)
  const [hsnProfile, setHsnProfile] = useState(null);
  // Classification prompt state
  const [classifyItem, setClassifyItem] = useState(null);
  const [classifyChoice, setClassifyChoice] = useState("");
  const [classifySaving, setClassifySaving] = useState(false);
  // Header field inline editing
  const [editingField, setEditingField] = useState(null); // api field name string | null
  const [editDraft, setEditDraft] = useState("");
  const [savingField, setSavingField] = useState(null);
  const [fieldEditErr, setFieldEditErr] = useState(null);
  // Line item row inline editing
  const [editingLid, setEditingLid] = useState(null);
  const [lineItemDraft, setLineItemDraft] = useState({});
  const [savingLid, setSavingLid] = useState(null);
  // Document review modal — auto-opens for low-confidence/needs-review
  // invoices, also reachable any time via the "Review document" button.
  const [reviewOpen, setReviewOpen] = useState(false);
  const [autoReviewedIds, setAutoReviewedIds] = useState(() => new Set());

  useEffect(() => {
    if (!invoiceId) return;
    setLoading(true);
    setData(null);
    setErr(null);
    setBupDraft({});
    setClassifyItem(null);
    getToken().then(tok =>
      fetch(`${API_BASE}/invoices/${invoiceId}`, {
        headers: { Authorization: `Bearer ${tok}` },
      })
    ).then(r => r.json()).then(d => {
      setData(d);
      setLoading(false);
      // Auto-open the review modal the FIRST time this invoice is opened
      // in this session if it looks low-confidence / needs manual review —
      // re-opening the same invoice again later won't keep re-popping it,
      // but switching to a different qualifying invoice will.
      if (shouldAutoReview(d) && !autoReviewedIds.has(invoiceId)) {
        setReviewOpen(true);
        setAutoReviewedIds(prev => new Set(prev).add(invoiceId));
      }
    }).catch(e => {
      setErr(String(e));
      setLoading(false);
    });

    // Load org HSN profile in parallel — used to badge line item HSN codes
    getToken().then(tok =>
      fetch(`${API_BASE}/org/hsn-profile`, { headers: { Authorization: `Bearer ${tok}` } })
    ).then(r => r.ok ? r.json() : null).then(d => setHsnProfile(d)).catch(() => {});
  }, [invoiceId, getToken]);

  async function saveBup(lineItemId, pct) {
    const val = parseFloat(pct);
    if (isNaN(val) || val < 0 || val > 100) return;
    setBupSaving(s => ({ ...s, [lineItemId]: true }));
    try {
      const tok = await getToken();
      await fetch(`${API_BASE}/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        // NOTE: backend's _LineItemPatch field is `business_use_percent`,
        // not `business_use_pct` — sending the wrong key meant FastAPI
        // silently dropped it (unknown fields are ignored, not errored),
        // so the save always "succeeded" while quietly doing nothing and
        // the value reset to 100 on next refresh. This was the root cause
        // of "editing business use % doesn't actually change anything."
        body: JSON.stringify({ fields: {}, line_items: [{ line_item_id: lineItemId, business_use_percent: val }] }),
      });
      // Update local data so claimable ITC recalculates immediately
      setData(d => ({
        ...d,
        line_items: d.line_items.map(li =>
          li.line_item_id === lineItemId ? { ...li, business_use_percent: val } : li
        ),
      }));
      setBupDraft(s => { const n = { ...s }; delete n[lineItemId]; return n; });
    } finally {
      setBupSaving(s => { const n = { ...s }; delete n[lineItemId]; return n; });
    }
  }

  async function saveClassification() {
    if (!classifyItem || !classifyChoice) return;
    setClassifySaving(true);
    try {
      const tok = await getToken();
      await fetch(`${API_BASE}/org/hsn-classifications`, {
        method: "POST",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          hsn_code: classifyItem.hsn_code,
          vendor_gstin: data.vendor_gstin,
          use_type: classifyChoice,
        }),
      });
      const tok2 = await getToken();
      const p = await fetch(`${API_BASE}/org/hsn-profile`, { headers: { Authorization: `Bearer ${tok2}` } });
      if (p.ok) setHsnProfile(await p.json());
      setClassifyItem(null);
      setClassifyChoice("");
    } finally {
      setClassifySaving(false);
    }
  }

  // Refresh invoice data after any edit
  async function refreshData() {
    const tok = await getToken();
    const r = await fetch(`${API_BASE}/invoices/${invoiceId}`, {
      headers: { Authorization: `Bearer ${tok}` },
    });
    if (r.ok) setData(await r.json());
  }

  // Save a single header-field correction
  async function saveFieldEdit() {
    if (!editingField) return;
    setSavingField(editingField);
    setFieldEditErr(null);
    try {
      const tok = await getToken();
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fields: { [editingField]: editDraft || null }, line_items: [] }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await refreshData();
      setEditingField(null);
      setEditDraft("");
    } catch (e) {
      setFieldEditErr(String(e));
    } finally {
      setSavingField(null);
    }
  }

  // Save a line item edit (update or new row)
  async function saveLineItem(lid) {
    setSavingLid(lid ?? "new");
    try {
      const tok = await getToken();
      // Coerce numeric fields and drop empty-string entries so the backend
      // (Optional[float]) doesn't reject "" or receive a string where a
      // partial-update COALESCE expects either NULL (omit it) or a number.
      const NUMERIC_FIELDS = new Set(["quantity", "rate", "amount", "line_tax_rate_percent", "business_use_percent"]);
      const cleaned = {};
      for (const [k, v] of Object.entries(lineItemDraft)) {
        if (v === "" || v == null) continue; // omit so backend keeps existing value
        cleaned[k] = NUMERIC_FIELDS.has(k) ? parseFloat(v) : v;
      }
      const patch = lid
        ? [{ line_item_id: lid, ...cleaned }]
        : [{ ...cleaned }];
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fields: {}, line_items: patch }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await refreshData();
      setEditingLid(null);
      setLineItemDraft({});
    } catch (e) {
      setFieldEditErr(String(e));
    } finally {
      setSavingLid(null);
    }
  }

  // Delete a line item
  async function deleteLineItem(lid) {
    if (!window.confirm("Remove this line item?")) return;
    setSavingLid(lid);
    try {
      const tok = await getToken();
      await fetch(`${API_BASE}/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fields: {}, line_items: [{ line_item_id: lid, delete: true }] }),
      });
      await refreshData();
    } finally {
      setSavingLid(null);
    }
  }

  const s = styles;
  const isCollapsed = panelSize === "collapsed";
  const isExpanded  = panelSize === "expanded";
  const panelWidth  = isCollapsed ? 44 : isExpanded ? 680 : 400;

  return (
    <div style={{
      ...s.panel,
      width: panelWidth,
      maxWidth: isExpanded ? "70vw" : isCollapsed ? 44 : "45vw",
      transition: "width 0.2s ease, max-width 0.2s ease",
    }}>
      {/* ── Collapsed strip ── */}
      {isCollapsed ? (
        <div style={s.collapsedStrip}>
          <button
            onClick={() => setPanelSize("normal")}
            style={s.headerIconBtn}
            title="Expand panel"
            aria-label="Expand panel"
          >⟩</button>
          <span style={s.collapsedLabel}>
            {data?.file_name ?? "Invoice"}
          </span>
        </div>
      ) : (
      <>
      {/* ── Normal / expanded header ── */}
      <div style={s.panelHeader}>
        <span style={{ fontWeight: 600, fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>
          {loading ? "Loading…" : data?.file_name ?? "Invoice detail"}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 4, flexShrink: 0, marginLeft: 8 }}>
          {/* Open the document + fill-in-the-blanks review modal on demand */}
          {data && (
            <button
              onClick={() => setReviewOpen(true)}
              style={{ ...s.headerIconBtn, fontSize: 11, padding: "3px 9px", color: "#312E81", borderColor: "#312E81" }}
              title="Open document viewer with a fill-in-the-blanks form"
            >🔍 Review document</button>
          )}
          {/* Collapse to strip */}
          <button
            onClick={() => setPanelSize("collapsed")}
            style={s.headerIconBtn}
            title="Collapse panel"
            aria-label="Collapse panel"
          >⟨</button>
          {/* Toggle normal ↔ expanded */}
          <button
            onClick={() => setPanelSize(isExpanded ? "normal" : "expanded")}
            style={s.headerIconBtn}
            title={isExpanded ? "Restore panel width" : "Expand panel wide"}
            aria-label={isExpanded ? "Restore panel width" : "Expand panel wide"}
          >{isExpanded ? "⊟" : "⊞"}</button>
          <button onClick={onClose} style={s.headerIconBtn} aria-label="Close detail panel">✕</button>
        </div>
      </div>

      {reviewOpen && (
        <ReviewModal
          invoiceId={invoiceId}
          data={data}
          getToken={getToken}
          onClose={() => setReviewOpen(false)}
          onSaved={refreshData}
        />
      )}

      {loading && <Spinner />}
      {err && <div style={{ padding: 24, color: "#B91C1C", fontSize: 13 }}>{err}</div>}


      {data && !loading && (
        <div style={s.panelBody}>

          {/* Status row */}
          <div style={s.panelRow}>
            <StatusBadge status={data.status} />
            {data.is_user_verified && (
              <span style={{ ...s.tag, background: "#F0FDF4", color: "#15803D" }}>✓ Human-verified</span>
            )}
            <span style={{ ...s.tag, background: "#F3F4F6", color: "#374151" }}>
              {data.extraction_method?.replace("_", " ")}
            </span>
            <span style={{ ...s.tag, background: "#F3F4F6", color: "#374151" }}>
              Confidence {data.confidence?.toFixed(0)}%
            </span>
          </div>

          {/* Issues */}
          {data.issues && (
            <div style={s.issues}>
              <span style={{ fontWeight: 600, color: "#B91C1C", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.05em" }}>Issues</span>
              <div style={{ marginTop: 4, fontSize: 12, color: "#374151", lineHeight: 1.5 }}>{data.issues}</div>
            </div>
          )}

          {/* ── Header field grid — click ✏ to edit any field ── */}
          {fieldEditErr && (
            <div style={{ fontSize: 11, color: "#B91C1C", background: "#FEF2F2", border: "1px solid #FECACA", borderRadius: 5, padding: "5px 10px", marginBottom: 8 }}>
              Save failed: {fieldEditErr}
            </div>
          )}
          <div style={s.fieldGrid}>
            {[
              // [label, displayValue, apiField, rawValue, inputType]
              // apiField=null → read-only
              ["Invoice #",       data.invoice_number,          "invoice_number",    data.invoice_number,       "text"],
              ["Invoice date",    fmtDate(data.invoice_date),   "invoice_date",      data.invoice_date,         "date"],
              ["Payment due",     fmtDate(data.payment_due_date),"payment_due_date", data.payment_due_date,     "date"],
              ["Place of supply", data.place_of_supply,         "place_of_supply",   data.place_of_supply,      "text"],
              ["Vendor",          data.vendor_name,             "vendor_name",       data.vendor_name,          "text"],
              ["Vendor GSTIN",    data.vendor_gstin,            "vendor_gstin",      data.vendor_gstin,         "text"],
              ["Buyer",           data.buyer_name,              "buyer_name",        data.buyer_name,           "text"],
              ["Buyer GSTIN",     data.buyer_gstin,             "buyer_gstin",       data.buyer_gstin,          "text"],
              ["Taxable amount",  fmtAmount(data.taxable_amount), "taxable_amount",  data.taxable_amount,       "number"],
              ["CGST",            fmtAmount(data.cgst_amount),  "cgst_amount",       data.cgst_amount,          "number"],
              ["SGST",            fmtAmount(data.sgst_amount),  "sgst_amount",       data.sgst_amount,          "number"],
              ["IGST",            fmtAmount(data.igst_amount),  "igst_amount",       data.igst_amount,          "number"],
              ["Total GST",       fmtAmount(data.total_gst_amount), "total_gst_amount", data.total_gst_amount,  "number"],
              ["Total amount",    fmtAmount(data.total_amount), "total_amount",      data.total_amount,         "number"],
              ["Currency",        data.currency_code,           "currency_code",     data.currency_code,        "text"],
              ["Tax label",       data.tax_label_raw,           null,                null,                      null],
              ["Tax rate",        data.tax_rate_percent != null ? `${data.tax_rate_percent}%` : null, null, null, null],
              ["PO number",       data.po_number,               "po_number",         data.po_number,            "text"],
            ].map(([label, display, apiField, raw, inputType]) => {
              const isEmpty    = display == null || display === "" || display === "—";
              const isEditing  = editingField === apiField && apiField != null;
              const isEditable = apiField != null;
              return (
                <div key={label} style={s.fieldRow}>
                  <span style={s.fieldLabel}>{label} :-</span>
                  <span style={{ display: "flex", alignItems: "center", gap: 5, minWidth: 0, flexWrap: "wrap" }}>
                    {isEditing ? (
                      <>
                        <input
                          autoFocus
                          type={inputType}
                          value={editDraft}
                          onChange={e => setEditDraft(e.target.value)}
                          onKeyDown={e => {
                            if (e.key === "Enter") saveFieldEdit();
                            if (e.key === "Escape") { setEditingField(null); setEditDraft(""); setFieldEditErr(null); }
                          }}
                          style={s.inlineInput}
                        />
                        <button onClick={saveFieldEdit} disabled={!!savingField} style={s.saveEditBtn}>
                          {savingField === apiField ? "…" : "✓"}
                        </button>
                        <button
                          onClick={() => { setEditingField(null); setEditDraft(""); setFieldEditErr(null); }}
                          style={s.cancelEditBtn}
                        >✗</button>
                      </>
                    ) : (
                      <>
                        <span style={isEmpty ? s.fieldValueEmpty : s.fieldValue}>
                          {isEmpty ? "N/A" : display}
                        </span>
                        {isEditable && (
                          <button
                            onClick={() => { setEditingField(apiField); setEditDraft(raw != null ? String(raw) : ""); setFieldEditErr(null); }}
                            style={s.editPencilBtn}
                            title={`Edit ${label}`}
                          >✏</button>
                        )}
                      </>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
          {/* Line items with ITC editing */}
          {data.line_items?.length > 0 && (
            <div style={{ marginTop: 20 }}>
              <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "#6B7280", marginBottom: 4 }}>
                Line items ({data.line_items.length})
              </div>
              <div style={{ fontSize: 11, color: "#9CA3AF", marginBottom: 8 }}>
                Set business use % per line to calculate claimable ITC (Rule 42/43 apportionment).
              </div>
              {/* When tax is only on the overall bill, show a note */}
              {data.line_items.every(i => i.tax_amount == null) && data.total_gst_amount != null && (
                <div style={{ fontSize: 11, color: "#854D0E", background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 6, padding: "6px 10px", marginBottom: 8 }}>
                  Tax is applied on the overall bill, not per line item.
                  Bill-level {data.tax_label_raw || "tax"}{data.tax_rate_percent ? ` @ ${data.tax_rate_percent}%` : ""}: {fmtAmount(data.total_gst_amount)}
                </div>
              )}
              <div style={{ overflowX: "auto" }}>
                <table style={{ ...s.table, width: "100%" }}>
                  <thead>
                    <tr>
                      {["Description / HSN", "Net", "Tax rate", "Tax", "Gross", "Biz use %", "Claimable ITC", "ITC status", ""].map(h => (
                        <th key={h} style={s.th}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.line_items.map((item, i) => {
                      const lid = item.line_item_id;
                      const bup = bupDraft[lid] !== undefined
                        ? bupDraft[lid]
                        : (item.business_use_percent ?? 100);
                      const bupNum = parseFloat(bup);
                      const isDirty = bupDraft[lid] !== undefined &&
                        parseFloat(bupDraft[lid]) !== (item.business_use_percent ?? 100);
                      const needsClassify = item.hsn_code &&
                        hsnProfile?.ambiguous_hsn_codes?.some(c => c.code === item.hsn_code) &&
                        !hsnProfile?.classified_hsn_codes?.some(c => (c.code ?? c) === item.hsn_code);

                      const netTotal = data.line_items.reduce((s, li) => s + (li.amount ?? 0), 0);
                      const taxBase = item.tax_amount != null
                        ? item.tax_amount
                        : (data.total_gst_amount != null && netTotal > 0 && item.amount != null)
                          ? data.total_gst_amount * (item.amount / netTotal)
                          : null;
                      const claimable = taxBase != null && !isNaN(bupNum)
                        ? taxBase * (bupNum / 100)
                        : null;

                      const isRowEditing = editingLid === lid;

                      return (
                        <>
                          <tr key={i} style={{ background: i % 2 === 0 ? "#fff" : "#F9FAFB" }}>
                            <td style={s.td}>
                              {isRowEditing ? (
                                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                                  <input placeholder="Description" value={lineItemDraft.description ?? item.description ?? ""} onChange={e => setLineItemDraft(d => ({...d, description: e.target.value}))} style={s.inlineInput} />
                                  <input placeholder="HSN code" value={lineItemDraft.hsn_code ?? item.hsn_code ?? ""} onChange={e => setLineItemDraft(d => ({...d, hsn_code: e.target.value}))} style={{...s.inlineInput, fontFamily: "monospace"}} />
                                </div>
                              ) : (
                                <>
                                  <div style={{ fontSize: 12 }}>{item.description ?? "—"}</div>
                                  {item.hsn_code && (
                                    <div style={{ display: "flex", alignItems: "center", marginTop: 2, flexWrap: "wrap", gap: 4 }}>
                                      <span style={{ fontFamily: "monospace", fontSize: 10, color: "#6B7280" }}>HSN {item.hsn_code}</span>
                                      <HsnBadge code={item.hsn_code} profile={hsnProfile} />
                                      {needsClassify && (
                                        <button
                                          onClick={() => setClassifyItem({ line_item_id: lid, hsn_code: item.hsn_code, description: item.description })}
                                          style={{ fontSize: 10, background: "#FEF9C3", border: "1px solid #FDE68A", borderRadius: 3, padding: "1px 6px", cursor: "pointer", color: "#92400E" }}
                                        >Classify use →</button>
                                      )}
                                    </div>
                                  )}
                                </>
                              )}
                            </td>
                            <td style={{ ...s.td, textAlign: "right", whiteSpace: "nowrap" }}>
                              {isRowEditing
                                ? <input type="number" value={lineItemDraft.amount ?? item.amount ?? ""} onChange={e => setLineItemDraft(d => ({...d, amount: e.target.value}))} style={{...s.inlineInput, width: 70, textAlign: "right"}} />
                                : fmtAmount(item.amount)}
                            </td>
                            <td style={{ ...s.td, textAlign: "center", whiteSpace: "nowrap", color: "#6B7280", fontSize: 11 }}>
                              {isRowEditing
                                ? <input type="number" placeholder="%" value={lineItemDraft.line_tax_rate_percent ?? item.line_tax_rate_percent ?? ""} onChange={e => setLineItemDraft(d => ({...d, line_tax_rate_percent: e.target.value}))} style={{...s.inlineInput, width: 52, textAlign: "right"}} />
                                : item.tax_rate != null
                                  ? `${item.tax_rate}%`
                                  : data.tax_rate_percent != null
                                    ? <span title="Bill-level rate">{data.tax_rate_percent}%*</span>
                                    : "—"}
                            </td>
                            <td style={{ ...s.td, textAlign: "right", whiteSpace: "nowrap", color: "#374151" }}>
                              {item.tax_amount != null
                                ? fmtAmount(item.tax_amount)
                                : taxBase != null
                                  ? <span style={{ color: "#9CA3AF" }} title="Apportioned from bill total">{fmtAmount(taxBase)}*</span>
                                  : "—"}
                            </td>
                            <td style={{ ...s.td, textAlign: "right", whiteSpace: "nowrap", fontWeight: 500 }}>
                              {item.gross_amount != null
                                ? fmtAmount(item.gross_amount)
                                : item.amount != null && taxBase != null
                                  ? <span style={{ color: "#9CA3AF" }} title="Net + apportioned tax">{fmtAmount(item.amount + taxBase)}*</span>
                                  : fmtAmount(item.amount)}
                            </td>
                            <td style={{ ...s.td, textAlign: "center" }}>
                              <div style={{ display: "flex", alignItems: "center", gap: 4, justifyContent: "center" }}>
                                <input
                                  type="number" min="0" max="100" step="5"
                                  value={bup}
                                  onChange={e => setBupDraft(prev => ({ ...prev, [lid]: e.target.value }))}
                                  style={{ width: 48, border: "1px solid #D1D5DB", borderRadius: 4, padding: "2px 4px", fontSize: 12, textAlign: "right", fontFamily: "inherit" }}
                                />
                                <span style={{ fontSize: 11, color: "#6B7280" }}>%</span>
                                {isDirty && (
                                  <button
                                    onClick={() => saveBup(lid, bupDraft[lid])}
                                    disabled={bupSaving[lid]}
                                    style={{ fontSize: 10, background: "#312E81", color: "#fff", border: "none", borderRadius: 3, padding: "2px 6px", cursor: "pointer" }}
                                  >{bupSaving[lid] ? "…" : "Save"}</button>
                                )}
                              </div>
                            </td>
                            <td style={{ ...s.td, textAlign: "right", fontWeight: 500, color: claimable != null ? "#15803D" : "#9CA3AF" }}>
                              {claimable != null ? fmtAmount(claimable) : "—"}
                            </td>
                            {/* ITC eligibility badge */}
                            <td style={{ ...s.td, whiteSpace: "nowrap" }}>
                              <ItcEligibilityBadge code={item.hsn_code} profile={hsnProfile} />
                            </td>
                            {/* Actions: edit / delete */}
                            <td style={{ ...s.td, whiteSpace: "nowrap" }}>
                              {isRowEditing ? (
                                <div style={{ display: "flex", gap: 4 }}>
                                  <button onClick={() => saveLineItem(lid)} disabled={savingLid === lid} style={{ ...s.saveEditBtn, fontSize: 11 }}>
                                    {savingLid === lid ? "…" : "✓ Save"}
                                  </button>
                                  <button onClick={() => { setEditingLid(null); setLineItemDraft({}); }} style={{ ...s.cancelEditBtn, fontSize: 11 }}>✗</button>
                                </div>
                              ) : (
                                <div style={{ display: "flex", gap: 4 }}>
                                  <button onClick={() => { setEditingLid(lid); setLineItemDraft({}); }} style={s.editPencilBtn} title="Edit line item">✏</button>
                                  <button onClick={() => deleteLineItem(lid)} disabled={savingLid === lid} style={{ ...s.editPencilBtn, color: "#B91C1C" }} title="Delete line item">🗑</button>
                                </div>
                              )}
                            </td>
                          </tr>
                        </>
                      );
                    })}
                  </tbody>
                  <tfoot>
                    <tr style={{ borderTop: "2px solid #E5E7EB" }}>
                      <td style={{ ...s.td, fontWeight: 600, fontSize: 12 }} colSpan={6}>
                        Total claimable ITC
                      </td>
                      <td style={{ ...s.td, textAlign: "right", fontWeight: 700, color: "#15803D" }}>
                        {(() => {
                          const netTotal = data.line_items.reduce((s, li) => s + (li.amount ?? 0), 0);
                          return fmtAmount(
                            data.line_items.reduce((sum, item) => {
                              const lid = item.line_item_id;
                              const bup = bupDraft[lid] !== undefined
                                ? parseFloat(bupDraft[lid])
                                : (item.business_use_percent ?? 100);
                              const taxBase = item.tax_amount != null
                                ? item.tax_amount
                                : (data.total_gst_amount != null && netTotal > 0 && item.amount != null)
                                  ? data.total_gst_amount * (item.amount / netTotal)
                                  : null;
                              return sum + (taxBase != null ? taxBase * (bup / 100) : 0);
                            }, 0)
                          );
                        })()}
                      </td>
                    </tr>
                    <tr>
                      <td colSpan={7} style={{ ...s.td, fontSize: 10, color: "#9CA3AF", paddingTop: 4 }}>
                        * Apportioned from bill-level tax — per-line tax not printed on this invoice.
                        Claimable ITC = tax amount × business use %.
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>

              {/* Classification prompt — shown when user clicks "Classify use" on an ambiguous HSN */}
              {classifyItem && (
                <div style={{ marginTop: 12, padding: "12px 14px", background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 8 }}>
                  <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 6 }}>
                    How does your business use HSN {classifyItem.hsn_code}?
                  </div>
                  <div style={{ fontSize: 11, color: "#6B7280", marginBottom: 10 }}>
                    "{classifyItem.description}" — your answer applies to all future invoices with this HSN from this vendor.
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 10 }}>
                    {[
                      ["raw_material", "Raw material / stock-in-trade — used in products I sell or make"],
                      ["capital_asset", "Capital asset — equipment or tool used in the business"],
                      ["personal", "Personal use — not for business purposes"],
                    ].map(([val, label]) => (
                      <label key={val} style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12, cursor: "pointer" }}>
                        <input type="radio" name="classify" value={val}
                          checked={classifyChoice === val}
                          onChange={() => setClassifyChoice(val)}
                          style={{ marginTop: 2 }}
                        />
                        {label}
                      </label>
                    ))}
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      onClick={saveClassification}
                      disabled={!classifyChoice || classifySaving}
                      style={{ background: "#312E81", color: "#fff", border: "none", borderRadius: 6, padding: "6px 16px", fontSize: 12, cursor: "pointer" }}
                    >
                      {classifySaving ? "Saving…" : "Save classification"}
                    </button>
                    <button
                      onClick={() => { setClassifyItem(null); setClassifyChoice(""); }}
                      style={{ background: "#fff", border: "1px solid #D1D5DB", borderRadius: 6, padding: "6px 14px", fontSize: 12, cursor: "pointer", color: "#374151" }}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {data.line_items?.length === 0 && (
            <div style={{ fontSize: 12, color: "#9CA3AF", marginTop: 16 }}>
              No line items extracted — re-process with live API keys to populate.
            </div>
          )}

          <div style={{ marginTop: 20, fontSize: 11, color: "#9CA3AF" }}>
            Processed {fmtDatetime(data.processed_at)} · Source: {data.source_type}
            {data.storage_path && ` · ${data.storage_path.split("/").pop()}`}
          </div>
        </div>
      )}
      </>
      )}
    </div>
  );
}

// ── Main list component ──────────────────────────────────────────────────────

export default function InvoiceList({ getToken }) {
  // Filters & sort
  const [status, setStatus]     = useState("");
  const [search, setSearch]     = useState("");
  const [searchInput, setSearchInput] = useState(""); // debounced separately
  const [sortBy, setSortBy]     = useState("processed_at");
  const [sortDir, setSortDir]   = useState("desc");
  const [page, setPage]         = useState(1);

  // Data
  const [data, setData]         = useState(null);
  const [loading, setLoading]   = useState(false);
  const [err, setErr]           = useState(null);

  // Detail panel
  const [selectedId, setSelectedId] = useState(null);

  const searchTimer = useRef(null);

  // ── Fetch ──────────────────────────────────────────────────────────────────
  const fetchInvoices = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const tok = await getToken();
      const params = new URLSearchParams({
        sort_by: sortBy,
        sort_dir: sortDir,
        page,
        page_size: PAGE_SIZE,
      });
      if (status) params.set("status", status);
      if (search) params.set("search", search);

      const res = await fetch(`${API_BASE}/invoices?${params}`, {
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [getToken, status, search, sortBy, sortDir, page]);

  useEffect(() => { fetchInvoices(); }, [fetchInvoices]);

  // Debounce search input → search state
  function handleSearchInput(val) {
    setSearchInput(val);
    clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      setSearch(val);
      setPage(1);
    }, 350);
  }

  // Sort column click: same column → flip dir; new column → desc
  function handleSort(col) {
    if (col === sortBy) {
      setSortDir(d => d === "desc" ? "asc" : "desc");
    } else {
      setSortBy(col);
      setSortDir("desc");
    }
    setPage(1);
  }

  const s = styles;
  const totalPages = data?.total_pages ?? 1;

  // ── Render ─────────────────────────────────────────────────────────────────
  // NOTE: no branded top bar here — App.jsx's TopNav already renders the
  // "Invoice Intelligence" header once for the whole app shell. This
  // component used to render a second, identical indigo bar directly above
  // the filter strip; that's removed. Refresh now lives in the filter strip.
  return (
    <div style={s.root}>
      {/* ── Filter strip ── */}
      <div style={s.filterStrip}>
        {/* Search */}
        <div style={s.searchWrap}>
          <span style={s.searchIcon}>⌕</span>
          <input
            type="text"
            placeholder="Search vendor…"
            value={searchInput}
            onChange={e => handleSearchInput(e.target.value)}
            style={s.searchInput}
          />
          {searchInput && (
            <button
              onClick={() => { setSearchInput(""); setSearch(""); setPage(1); }}
              style={s.clearBtn}
              aria-label="Clear search"
            >✕</button>
          )}
        </div>

        {/* Status filter */}
        <select
          value={status}
          onChange={e => { setStatus(e.target.value); setPage(1); }}
          style={s.select}
        >
          <option value="">All statuses</option>
          <option value="PASSED">Passed</option>
          <option value="WARNING">Warning</option>
          <option value="FAILED">Failed</option>
          <option value="NEEDS_MANUAL_REVIEW">Needs review</option>
        </select>

        {/* Count + refresh */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginLeft: "auto" }}>
          <div style={s.countLabel}>
            {data != null && !loading && (
              <>{data.total_count.toLocaleString("en-IN")} invoice{data.total_count !== 1 ? "s" : ""}</>
            )}
          </div>
          <button
            onClick={fetchInvoices}
            style={s.refreshBtnLight}
            disabled={loading}
            title="Refresh"
          >
            ↻ Refresh
          </button>
        </div>
      </div>

      {/* ── Content area ── */}
      <div style={{ display: "flex", flex: 1, minHeight: 0, overflow: "hidden" }}>

        {/* ── Table area ── */}
        <div style={{ flex: 1, overflow: "auto", minWidth: 0 }}>
          {err && (
            <div style={s.errBanner}>
              Failed to load invoices: {err}
              <button onClick={fetchInvoices} style={s.retryBtn}>Retry</button>
            </div>
          )}

          {loading && !data && <Spinner />}

          {!loading && data?.invoices?.length === 0 && (
            <div style={s.empty}>
              <div style={{ fontSize: 32, marginBottom: 12 }}>📄</div>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>No invoices found</div>
              <div style={{ fontSize: 13, color: "#6B7280" }}>
                {status || search
                  ? "Try adjusting your filters."
                  : "Upload a PDF using the button below to get started."}
              </div>
            </div>
          )}

          {data?.invoices?.length > 0 && (
            <table style={s.table}>
              <thead>
                <tr>
                  {[
                    { key: "file_name",    label: "File",            sortable: true  },
                    { key: "vendor_name",  label: "Vendor",          sortable: true  },
                    { key: "invoice_date", label: "Invoice date",    sortable: true  },
                    { key: "total_amount", label: "Total",           sortable: true  },
                    { key: "status",       label: "Status",          sortable: true  },
                    { key: "confidence",   label: "Confidence",      sortable: true  },
                    { key: "processed_at", label: "Processed",       sortable: true  },
                  ].map(col => (
                    <th
                      key={col.key}
                      style={{
                        ...s.th,
                        cursor: col.sortable ? "pointer" : "default",
                        userSelect: "none",
                        whiteSpace: "nowrap",
                      }}
                      onClick={col.sortable ? () => handleSort(col.key) : undefined}
                    >
                      {col.label}
                      {col.sortable && (
                        <SortIcon col={col.key} active={sortBy === col.key} dir={sortDir} />
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.invoices.map((inv, i) => (
                  <tr
                    key={inv.invoice_id}
                    onClick={() => setSelectedId(inv.invoice_id === selectedId ? null : inv.invoice_id)}
                    style={{
                      ...s.row,
                      background: inv.invoice_id === selectedId
                        ? "#EEF2FF"
                        : i % 2 === 0 ? "#FFFFFF" : "#F9FAFB",
                      borderLeft: inv.invoice_id === selectedId
                        ? "3px solid #312E81"
                        : "3px solid transparent",
                    }}
                  >
                    <td style={{ ...s.td, maxWidth: 160 }}>
                      <span style={{ fontSize: 11, color: "#6B7280" }}>
                        {inv.file_name}
                        {inv.page > 1 && <span style={{ color: "#9CA3AF" }}> p{inv.page}</span>}
                      </span>
                    </td>
                    <td style={s.td}>
                      <span style={{ fontWeight: inv.vendor_name ? 500 : 400, color: inv.vendor_name ? "#111318" : "#9CA3AF" }}>
                        {inv.vendor_name ?? "—"}
                      </span>
                      {inv.vendor_gstin && (
                        <div style={{ fontSize: 10, color: "#6B7280", fontFamily: "monospace", marginTop: 1 }}>
                          {inv.vendor_gstin}
                        </div>
                      )}
                    </td>
                    <td style={{ ...s.td, whiteSpace: "nowrap" }}>{fmtDate(inv.invoice_date)}</td>
                    <td style={{ ...s.td, textAlign: "right", fontVariantNumeric: "tabular-nums", fontWeight: inv.total_amount ? 500 : 400, color: inv.total_amount ? "#111318" : "#9CA3AF" }}>
                      {fmtAmount(inv.total_amount)}
                    </td>
                    <td style={s.td}><StatusBadge status={inv.status} /></td>
                    <td style={{ ...s.td, textAlign: "right", color: "#6B7280", fontSize: 12 }}>
                      {inv.confidence != null ? `${inv.confidence.toFixed(0)}%` : "—"}
                    </td>
                    <td style={{ ...s.td, whiteSpace: "nowrap", fontSize: 11, color: "#9CA3AF" }}>
                      {fmtDatetime(inv.processed_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* ── Pagination ── */}
          {data?.total_count > PAGE_SIZE && (
            <div style={s.pagination}>
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page <= 1 || loading}
                style={{ ...s.pageBtn, opacity: page <= 1 ? 0.4 : 1 }}
              >
                ← Prev
              </button>
              <span style={{ fontSize: 13, color: "#6B7280" }}>
                Page {page} of {totalPages}
              </span>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages || loading}
                style={{ ...s.pageBtn, opacity: page >= totalPages ? 0.4 : 1 }}
              >
                Next →
              </button>
            </div>
          )}
        </div>

        {/* ── Detail panel ── */}
        {selectedId && (
          <DetailPanel
            invoiceId={selectedId}
            getToken={getToken}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
    </div>
  );
}

// ── Style tokens ─────────────────────────────────────────────────────────────
// All styles in one object so they're easy to retheme later.
const styles = {
  root: {
    display: "flex",
    flexDirection: "column",
    height: "100vh",
    background: "#F7F8FA",
    fontFamily: "Inter, system-ui, -apple-system, sans-serif",
    fontSize: 13,
    color: "#111318",
    overflow: "hidden",
  },

  // Filter strip
  filterStrip: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "10px 20px",
    background: "#fff",
    borderBottom: "1px solid #E5E7EB",
    flexShrink: 0,
  },
  searchWrap: {
    position: "relative",
    display: "flex",
    alignItems: "center",
  },
  searchIcon: {
    position: "absolute",
    left: 9,
    fontSize: 15,
    color: "#9CA3AF",
    pointerEvents: "none",
  },
  searchInput: {
    border: "1px solid #D1D5DB",
    borderRadius: 6,
    padding: "6px 28px 6px 30px",
    fontSize: 13,
    fontFamily: "inherit",
    outline: "none",
    width: 220,
    background: "#F9FAFB",
    color: "#111318",
  },
  clearBtn: {
    position: "absolute",
    right: 6,
    background: "none",
    border: "none",
    cursor: "pointer",
    color: "#9CA3AF",
    fontSize: 11,
    padding: 2,
    lineHeight: 1,
  },
  select: {
    border: "1px solid #D1D5DB",
    borderRadius: 6,
    padding: "6px 10px",
    fontSize: 13,
    fontFamily: "inherit",
    background: "#F9FAFB",
    color: "#111318",
    cursor: "pointer",
    outline: "none",
  },
  countLabel: {
    fontSize: 12,
    color: "#6B7280",
    whiteSpace: "nowrap",
  },
  // Light-on-white refresh button — used in the filter strip now that the
  // indigo top bar (which this was originally styled for) is gone.
  refreshBtnLight: {
    background: "#fff",
    border: "1px solid #D1D5DB",
    borderRadius: 6,
    color: "#374151",
    fontSize: 12,
    padding: "5px 12px",
    cursor: "pointer",
    fontFamily: "inherit",
    whiteSpace: "nowrap",
  },

  // Table
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: 13,
  },
  th: {
    padding: "9px 14px",
    textAlign: "left",
    fontSize: 11,
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.05em",
    color: "#6B7280",
    background: "#F3F4F6",
    borderBottom: "1px solid #E5E7EB",
    position: "sticky",
    top: 0,
    zIndex: 1,
  },
  row: {
    cursor: "pointer",
    transition: "background 0.1s",
  },
  td: {
    padding: "9px 14px",
    borderBottom: "1px solid #F3F4F6",
    verticalAlign: "top",
    lineHeight: 1.4,
  },

  // Error / empty
  errBanner: {
    margin: 20,
    padding: "12px 16px",
    background: "#FEF2F2",
    border: "1px solid #FECACA",
    borderRadius: 6,
    color: "#B91C1C",
    fontSize: 13,
    display: "flex",
    alignItems: "center",
    gap: 12,
  },
  retryBtn: {
    background: "#B91C1C",
    color: "#fff",
    border: "none",
    borderRadius: 5,
    padding: "4px 12px",
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  empty: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    padding: 64,
    color: "#374151",
    textAlign: "center",
  },

  // Pagination
  pagination: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 16,
    padding: "14px 20px",
    borderTop: "1px solid #E5E7EB",
    background: "#fff",
  },
  pageBtn: {
    background: "#fff",
    border: "1px solid #D1D5DB",
    borderRadius: 6,
    padding: "5px 14px",
    fontSize: 13,
    cursor: "pointer",
    fontFamily: "inherit",
    color: "#374151",
  },

  // Detail panel — width controlled inline per panelSize state
  panel: {
    borderLeft: "1px solid #E5E7EB",
    background: "#fff",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    flexShrink: 0,
  },
  // Collapsed vertical strip
  collapsedStrip: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    padding: "10px 0",
    gap: 12,
    height: "100%",
    overflow: "hidden",
  },
  collapsedLabel: {
    fontSize: 11,
    color: "#9CA3AF",
    writingMode: "vertical-rl",
    textOrientation: "mixed",
    transform: "rotate(180deg)",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    maxHeight: 200,
    letterSpacing: "0.03em",
  },
  panelHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "10px 12px",
    borderBottom: "1px solid #E5E7EB",
    background: "#F9FAFB",
    flexShrink: 0,
    gap: 8,
  },
  headerIconBtn: {
    background: "none",
    border: "1px solid #E5E7EB",
    cursor: "pointer",
    fontSize: 13,
    color: "#6B7280",
    padding: "3px 8px",
    borderRadius: 5,
    lineHeight: 1,
    fontFamily: "inherit",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  panelBody: {
    padding: "16px",
    overflowY: "auto",
    flex: 1,
  },
  panelRow: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    flexWrap: "wrap",
    marginBottom: 12,
  },
  tag: {
    fontSize: 11,
    padding: "2px 7px",
    borderRadius: 4,
    fontWeight: 500,
  },
  issues: {
    background: "#FFF7ED",
    border: "1px solid #FED7AA",
    borderRadius: 6,
    padding: "10px 12px",
    marginBottom: 14,
  },
  // Two-column grid with FIXED 118px label column.
  // This is what stopped values from being squeezed — "VENDOR GSTIN" no longer
  // decides how much space "27AABCT1234F1Z5" gets.
  fieldGrid: {
    display: "grid",
    gridTemplateColumns: "118px 1fr",
    gap: "5px 12px",
    alignItems: "baseline",
    marginBottom: 4,
  },
  fieldRow: {
    display: "contents",  // participates in parent grid without adding a wrapper box
  },
  fieldLabel: {
    fontSize: 11,
    color: "#6B7280",
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  },
  fieldValue: {
    fontSize: 13,
    color: "#111318",
    overflowWrap: "break-word",
    wordBreak: "break-word",
    minWidth: 0,
  },
  fieldValueEmpty: {
    fontSize: 13,
    color: "#9CA3AF",
    fontStyle: "italic",
  },
  // Inline edit controls
  inlineInput: {
    border: "1px solid #6366F1",
    borderRadius: 4,
    padding: "2px 6px",
    fontSize: 12,
    fontFamily: "inherit",
    outline: "none",
    background: "#EEF2FF",
    color: "#111318",
    minWidth: 0,
    flex: 1,
  },
  saveEditBtn: {
    background: "#312E81",
    color: "#fff",
    border: "none",
    borderRadius: 4,
    padding: "2px 7px",
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "inherit",
    flexShrink: 0,
  },
  cancelEditBtn: {
    background: "#fff",
    color: "#6B7280",
    border: "1px solid #D1D5DB",
    borderRadius: 4,
    padding: "2px 7px",
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "inherit",
    flexShrink: 0,
  },
  editPencilBtn: {
    background: "none",
    border: "none",
    cursor: "pointer",
    fontSize: 11,
    color: "#9CA3AF",
    padding: "0 2px",
    lineHeight: 1,
    flexShrink: 0,
    opacity: 0.7,
  },
};

/*
──────────────────────────────────────────────────────────────────────────────
INTEGRATION: update webapp/frontend/src/App.jsx

Replace the ApiTester function and the <SignedIn> block with this:

import InvoiceList from './InvoiceList'

// Inside the signed-in section, replace ApiTester with:
function AppShell() {
  const { getToken } = useAuth()
  const { organization } = useOrganization()

  if (!organization) {
    return (
      <div style={{ fontFamily: 'monospace', padding: 24 }}>
        <p>Create or select an organization to continue:</p>
        <CreateOrganization />
      </div>
    )
  }

  return <InvoiceList getToken={getToken} />
}

// Then in the JSX:
<SignedIn>
  <AppShell />
</SignedIn>
──────────────────────────────────────────────────────────────────────────────
*/