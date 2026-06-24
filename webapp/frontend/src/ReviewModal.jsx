/**
 * ReviewModal.jsx
 * ===============
 * A focused, full-screen-ish overlay for reviewing one invoice when the
 * extraction is unreliable: the original document on one side, a
 * "fill in the blanks" form for missing/low-confidence fields on the
 * other. Saves through the same PATCH /invoices/{id} endpoint the detail
 * panel's inline editing uses, so a save here shows up everywhere else
 * immediately (list, detail panel, ITC summary, activity log).
 *
 * When to show this:
 *   - AUTOMATICALLY right after a low-confidence / NEEDS_MANUAL_REVIEW
 *     invoice is opened (InvoiceList.jsx decides this — see
 *     shouldAutoReview() exported below — this component itself doesn't
 *     decide, it just renders once told to).
 *   - On demand via a "Review document" button on ANY invoice, regardless
 *     of confidence — sometimes a clean extraction still has a field a
 *     human wants to double-check against the source document.
 *
 * Props:
 *   invoiceId   — string
 *   data        — the invoice detail object (same shape DetailPanel uses)
 *   getToken    — async () => string
 *   onClose     — () => void
 *   onSaved     — () => void   (parent should refetch invoice + list after this)
 */

import { useState, useEffect } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// Fields worth surfacing in the fill-in-the-blanks form, in the order a
// human would naturally fill out a GST invoice — identity first, then
// dates, then money. Each entry: [label, apiField, inputType].
const REVIEW_FIELDS = [
  ["Invoice number",   "invoice_number",    "text"],
  ["Invoice date",     "invoice_date",      "date"],
  ["Payment due date", "payment_due_date",  "date"],
  ["Vendor name",      "vendor_name",       "text"],
  ["Vendor GSTIN",     "vendor_gstin",      "text"],
  ["Buyer name",       "buyer_name",        "text"],
  ["Buyer GSTIN",      "buyer_gstin",       "text"],
  ["Place of supply",  "place_of_supply",   "text"],
  ["Taxable amount",   "taxable_amount",    "number"],
  ["CGST amount",      "cgst_amount",       "number"],
  ["SGST amount",      "sgst_amount",       "number"],
  ["IGST amount",      "igst_amount",       "number"],
  ["Total GST amount", "total_gst_amount",  "number"],
  ["Total amount",     "total_amount",      "number"],
  ["Currency",         "currency_code",     "text"],
  ["PO number",        "po_number",         "text"],
];

/**
 * Decides whether an invoice should trigger the review popup automatically
 * the moment it's opened. Lives in InvoiceReviewUtils.js, not here — a
 * .jsx file that default-exports a component AND named-exports a plain
 * function breaks Vite's React Fast Refresh. Import it from there:
 *   import { shouldAutoReview } from "./InvoiceReviewUtils";
 */

/** Which fields actually look blank/missing right now — drives the "fields
 * to fill in" list at the bottom rather than showing all 16 every time. */
function blankFields(data) {
  return REVIEW_FIELDS.filter(([, field]) => {
    const v = data?.[field];
    return v == null || v === "";
  });
}

export default function ReviewModal({ invoiceId, data, getToken, onClose, onSaved }) {
  const [draft, setDraft]   = useState({});
  const [lineItemDrafts, setLineItemDrafts] = useState({}); // { [line_item_id]: { field: value } }
  const [fileUrl, setFileUrl] = useState(null);
  const [driveFileId, setDriveFileId] = useState(null);
  const [storageError, setStorageError] = useState(null);
  const [fileUrlErr, setFileUrlErr] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr]       = useState(null);
  const [saved, setSaved]   = useState(false);
  const [reconciliationIssues, setReconciliationIssues] = useState([]);

  useEffect(() => {
    setDraft({});
    setLineItemDrafts({});
    setSaved(false);
    setErr(null);
    setFileUrl(null);
    setDriveFileId(null);
    setStorageError(null);
    setFileUrlErr(false);
    setReconciliationIssues([]);
    if (!invoiceId) return;
    getToken().then(tok =>
      fetch(`${API_BASE}/invoices/${invoiceId}/file-url`, { headers: { Authorization: `Bearer ${tok}` } })
    ).then(r => r.json()).then(d => {
      // Backend's fallback chain (see main.py get_file_url): a real signed
      // Supabase URL when Storage is reachable, otherwise drive_file_id so
      // we can embed Google Drive's own preview instead of giving up. This
      // used to only check d.url and silently discard drive_file_id even
      // when it was present — meaning the fallback never actually worked.
      if (d.url) {
        setFileUrl(d.url);
      } else if (d.drive_file_id) {
        setDriveFileId(d.drive_file_id);
        if (d.storage_error) setStorageError(d.storage_error);
      } else {
        if (d.storage_error) setStorageError(d.storage_error);
        setFileUrlErr(true);
      }
    }).catch(() => setFileUrlErr(true));
  }, [invoiceId, getToken]);

  if (!invoiceId || !data) return null;

  const missing = blankFields(data);
  // Show every field for editing, but visually flag the missing ones —
  // a human correcting one wrong field often wants to glance at neighbors.
  const fieldsToShow = REVIEW_FIELDS;

  function fieldValue(field) {
    return draft[field] !== undefined ? draft[field] : (data[field] ?? "");
  }

  function lineItemValue(lineItemId, field, original) {
    const d = lineItemDrafts[lineItemId];
    return d && d[field] !== undefined ? d[field] : (original ?? "");
  }

  function setLineItemField(lineItemId, field, value) {
    setLineItemDrafts(prev => ({
      ...prev,
      [lineItemId]: { ...prev[lineItemId], [field]: value },
    }));
  }

  async function handleSaveAll() {
    setSaving(true);
    setErr(null);
    try {
      const tok = await getToken();
      const fields = {};
      for (const [, field] of fieldsToShow) {
        if (draft[field] !== undefined && draft[field] !== String(data[field] ?? "")) {
          fields[field] = draft[field] === "" ? null : draft[field];
        }
      }

      // Build line_items patches only for rows that actually have a draft —
      // same partial-update contract main.py's PATCH handler already
      // expects (COALESCE on the backend means omitted fields keep their
      // current value, so we only need to send what actually changed).
      const lineItemPatches = Object.entries(lineItemDrafts)
        .filter(([, fieldsDraft]) => Object.keys(fieldsDraft).length > 0)
        .map(([lineItemId, fieldsDraft]) => {
          const patch = { line_item_id: lineItemId };
          for (const [field, value] of Object.entries(fieldsDraft)) {
            if (value === "") continue; // omit, don't send empty string for a numeric field
            patch[field] = ["quantity", "rate", "amount", "line_tax_rate_percent", "business_use_percent"].includes(field)
              ? Number(value)
              : value;
          }
          return patch;
        });

      if (Object.keys(fields).length === 0 && lineItemPatches.length === 0) {
        setSaved(true);
        return;
      }
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fields, line_items: lineItemPatches }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const result = await res.json();
      // Backend now reconciles line items against header totals on every
      // save (see main.py _reconcile_invoice_amounts) and reports specific
      // mismatches here rather than only setting a status flag invisibly —
      // surface them immediately instead of making the user reopen the
      // invoice to discover something doesn't add up.
      setReconciliationIssues(result.reconciliation_issues || []);
      setSaved(true);
      onSaved?.();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  const s = styles;

  return (
    <div style={s.overlay} role="dialog" aria-modal="true" aria-label="Review document">
      <div style={s.modal}>
        <div style={s.modalHeader}>
          <div>
            <div style={s.modalTitle}>Review: {data.file_name || "Invoice"}</div>
            <div style={s.modalSubtitle}>
              {missing.length > 0
                ? `${missing.length} field${missing.length !== 1 ? "s" : ""} look blank below — fill in what you can see on the document.`
                : "All fields have a value — double-check anything that looks wrong against the document."}
            </div>
          </div>
          <button onClick={onClose} style={s.closeBtn} aria-label="Close review">✕</button>
        </div>

        <div style={s.modalBody}>
          {/* ── Document viewer ── */}
          <div style={s.viewerPane}>
            {fileUrl ? (
              <iframe src={fileUrl} title="Invoice document" style={s.iframe} />
            ) : driveFileId ? (
              // Drive's "/preview" URL embeds inline with no sign-in prompt.
              // "/view" (used in an earlier version of this fix) is Drive's
              // full interactive viewer — it's auth-gated and can refuse to
              // render inside an iframe at all, which is exactly the
              // "asks to sign in and pops a new browser window" symptom.
              // "/preview" is the embeddable, no-auth-prompt variant and
              // never opens a new tab on its own.
              <iframe
                src={`https://drive.google.com/file/d/${driveFileId}/preview`}
                title="Invoice document (from Google Drive)"
                style={s.iframe}
                allow="autoplay"
              />
            ) : fileUrlErr ? (
              <div style={s.viewerFallback}>
                Document preview isn't available (file storage not configured, or the file is missing).
                You can still fill in fields from a copy of the invoice you have on hand.
                {storageError && (
                  <div style={s.viewerErrorDetail}>Storage error: {storageError}</div>
                )}
              </div>
            ) : (
              <div style={s.viewerFallback}>Loading document…</div>
            )}
          </div>

          {/* ── Fill-in-the-blanks form ── */}
          <div style={s.formPane}>
            <div style={s.formScroll}>
              {reconciliationIssues.length > 0 && (
                <div style={s.reconcileBanner}>
                  <div style={s.reconcileTitle}>⚠ Amounts don't add up</div>
                  {reconciliationIssues.map((issue, i) => (
                    <div key={i} style={s.reconcileLine}>{issue}</div>
                  ))}
                  <div style={s.reconcileHint}>
                    Saved anyway — this invoice is flagged for review until the numbers reconcile.
                  </div>
                </div>
              )}
              {fieldsToShow.map(([label, field, inputType]) => {
                const isBlank = data[field] == null || data[field] === "";
                return (
                  <div key={field} style={s.formRow}>
                    <label style={{ ...s.formLabel, ...(isBlank ? s.formLabelBlank : {}) }}>
                      {isBlank && <span style={s.blankDot} title="Currently blank" />}
                      {label}
                    </label>
                    <input
                      type={inputType}
                      value={fieldValue(field)}
                      onChange={e => setDraft(d => ({ ...d, [field]: e.target.value }))}
                      placeholder={isBlank ? "Not extracted — type what's on the document" : ""}
                      style={{ ...s.formInput, ...(isBlank ? s.formInputBlank : {}) }}
                    />
                  </div>
                );
              })}

              {/* ── Line items — previously missing entirely from this
                  modal, including the per-line tax % the person flagged
                  as not showing up anywhere here. ── */}
              <div style={s.lineItemsHeading}>
                Line items {data.line_items?.length > 0 ? `(${data.line_items.length})` : ""}
              </div>
              {(!data.line_items || data.line_items.length === 0) && (
                <div style={s.noLineItems}>No line items recorded for this invoice.</div>
              )}
              {data.line_items?.map(item => {
                const lid = item.line_item_id;
                return (
                  <div key={lid} style={s.lineItemCard}>
                    <input
                      type="text"
                      value={lineItemValue(lid, "description", item.description)}
                      onChange={e => setLineItemField(lid, "description", e.target.value)}
                      placeholder="Description"
                      style={{ ...s.formInput, marginBottom: 6 }}
                    />
                    <div style={s.lineItemGrid}>
                      <div>
                        <label style={s.lineItemLabel}>HSN/SAC</label>
                        <input
                          type="text"
                          value={lineItemValue(lid, "hsn_code", item.hsn_code)}
                          onChange={e => setLineItemField(lid, "hsn_code", e.target.value)}
                          style={s.formInputSmall}
                        />
                      </div>
                      <div>
                        <label style={s.lineItemLabel}>Qty</label>
                        <input
                          type="number"
                          value={lineItemValue(lid, "quantity", item.quantity)}
                          onChange={e => setLineItemField(lid, "quantity", e.target.value)}
                          style={s.formInputSmall}
                        />
                      </div>
                      <div>
                        <label style={s.lineItemLabel}>Rate</label>
                        <input
                          type="number"
                          value={lineItemValue(lid, "rate", item.rate)}
                          onChange={e => setLineItemField(lid, "rate", e.target.value)}
                          style={s.formInputSmall}
                        />
                      </div>
                      <div>
                        <label style={s.lineItemLabel}>Amount</label>
                        <input
                          type="number"
                          value={lineItemValue(lid, "amount", item.amount)}
                          onChange={e => setLineItemField(lid, "amount", e.target.value)}
                          style={s.formInputSmall}
                        />
                      </div>
                      <div>
                        <label style={s.lineItemLabel}>Tax %</label>
                        <input
                          type="number"
                          value={lineItemValue(lid, "line_tax_rate_percent", item.line_tax_rate_percent)}
                          onChange={e => setLineItemField(lid, "line_tax_rate_percent", e.target.value)}
                          placeholder="—"
                          style={s.formInputSmall}
                        />
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {err && <div style={s.errBanner}>Save failed: {err}</div>}
            {saved && !err && <div style={s.savedBanner}>✓ Saved — values updated everywhere this invoice appears.</div>}

            <div style={s.formFooter}>
              <button onClick={onClose} style={s.secondaryBtn}>
                {saved ? "Done" : "Close without saving"}
              </button>
              <button onClick={handleSaveAll} disabled={saving} style={s.primaryBtn}>
                {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const styles = {
  overlay: {
    position: "fixed", inset: 0, background: "rgba(17, 19, 24, 0.55)",
    display: "flex", alignItems: "center", justifyContent: "center",
    zIndex: 1000, padding: 20, fontFamily: "Inter, system-ui, -apple-system, sans-serif",
  },
  modal: {
    background: "#fff", borderRadius: 14, width: "100%", maxWidth: 1100, height: "85vh",
    display: "flex", flexDirection: "column", overflow: "hidden",
    boxShadow: "0 24px 70px rgba(0,0,0,0.35)",
  },
  modalHeader: {
    display: "flex", alignItems: "flex-start", justifyContent: "space-between",
    padding: "16px 20px", borderBottom: "1px solid #E5E7EB", flexShrink: 0, gap: 12,
  },
  modalTitle: { fontSize: 15, fontWeight: 700, color: "#111318" },
  modalSubtitle: { fontSize: 12, color: "#6B7280", marginTop: 3, lineHeight: 1.5, maxWidth: 640 },
  closeBtn: {
    background: "none", border: "1px solid #E5E7EB", borderRadius: 6, cursor: "pointer",
    fontSize: 14, color: "#6B7280", padding: "4px 10px", flexShrink: 0,
  },
  modalBody: { display: "flex", flex: 1, minHeight: 0 },
  viewerPane: {
    flex: "1.2", background: "#1F2937", display: "flex",
    alignItems: "center", justifyContent: "center", minWidth: 0,
  },
  iframe: { width: "100%", height: "100%", border: "none", background: "#fff" },
  viewerFallback: { color: "#D1D5DB", fontSize: 13, textAlign: "center", padding: 32, lineHeight: 1.6 },
  viewerErrorDetail: {
    marginTop: 12, fontSize: 11, color: "#9CA3AF", fontFamily: "monospace",
    wordBreak: "break-word",
  },
  reconcileBanner: {
    background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 8,
    padding: "10px 12px", marginBottom: 14,
  },
  reconcileTitle: { fontSize: 12, fontWeight: 700, color: "#92400E", marginBottom: 4 },
  reconcileLine: { fontSize: 12, color: "#92400E", lineHeight: 1.5 },
  reconcileHint: { fontSize: 11, color: "#B45309", marginTop: 6, lineHeight: 1.4 },
  lineItemsHeading: {
    fontSize: 11, fontWeight: 700, color: "#6B7280", textTransform: "uppercase",
    letterSpacing: "0.04em", marginTop: 18, marginBottom: 8, paddingTop: 14,
    borderTop: "1px solid #E5E7EB",
  },
  noLineItems: { fontSize: 12, color: "#9CA3AF", fontStyle: "italic" },
  lineItemCard: {
    border: "1px solid #E5E7EB", borderRadius: 8, padding: 10, marginBottom: 8,
    background: "#FAFAFB",
  },
  lineItemGrid: {
    display: "grid", gridTemplateColumns: "1fr 0.7fr 0.8fr 0.9fr 0.8fr", gap: 6,
  },
  lineItemLabel: {
    display: "block", fontSize: 10, fontWeight: 600, color: "#9CA3AF",
    textTransform: "uppercase", letterSpacing: "0.03em", marginBottom: 2,
  },
  formInputSmall: {
    width: "100%", border: "1px solid #D1D5DB", borderRadius: 5, padding: "5px 7px",
    fontSize: 12, fontFamily: "inherit", outline: "none", color: "#111318", boxSizing: "border-box",
  },
  formPane: {
    flex: 1, display: "flex", flexDirection: "column", borderLeft: "1px solid #E5E7EB", minWidth: 320,
  },
  formScroll: { flex: 1, overflowY: "auto", padding: "16px 20px" },
  formRow: { marginBottom: 12 },
  formLabel: {
    display: "flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 600,
    color: "#6B7280", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4,
  },
  formLabelBlank: { color: "#92400E" },
  blankDot: { width: 6, height: 6, borderRadius: "50%", background: "#F59E0B", flexShrink: 0 },
  formInput: {
    width: "100%", border: "1px solid #D1D5DB", borderRadius: 6, padding: "7px 10px",
    fontSize: 13, fontFamily: "inherit", outline: "none", color: "#111318", boxSizing: "border-box",
  },
  formInputBlank: { borderColor: "#FDE68A", background: "#FFFBEB" },
  errBanner: {
    margin: "0 20px 12px", padding: "8px 12px", background: "#FEF2F2", border: "1px solid #FECACA",
    borderRadius: 6, color: "#B91C1C", fontSize: 12,
  },
  savedBanner: {
    margin: "0 20px 12px", padding: "8px 12px", background: "#F0FDF4", border: "1px solid #BBF7D0",
    borderRadius: 6, color: "#15803D", fontSize: 12,
  },
  formFooter: {
    display: "flex", justifyContent: "flex-end", gap: 8, padding: "14px 20px",
    borderTop: "1px solid #E5E7EB", flexShrink: 0,
  },
  primaryBtn: {
    background: "#312E81", color: "#fff", border: "none", borderRadius: 6, padding: "8px 18px",
    fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
  },
  secondaryBtn: {
    background: "#fff", color: "#374151", border: "1px solid #D1D5DB", borderRadius: 6,
    padding: "8px 16px", fontSize: 13, cursor: "pointer", fontFamily: "inherit",
  },
};