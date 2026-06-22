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
 * the moment it's opened. Exported so InvoiceList.jsx's row-click handler
 * can call this without duplicating the threshold logic.
 */
export function shouldAutoReview(invoice) {
  if (!invoice) return false;
  if (invoice.status === "NEEDS_MANUAL_REVIEW" || invoice.status === "FAILED") return true;
  if (typeof invoice.confidence === "number" && invoice.confidence < 70) return true;
  return false;
}

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
  const [fileUrl, setFileUrl] = useState(null);
  const [fileUrlErr, setFileUrlErr] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr]       = useState(null);
  const [saved, setSaved]   = useState(false);

  useEffect(() => {
    setDraft({});
    setSaved(false);
    setErr(null);
    setFileUrl(null);
    setFileUrlErr(false);
    if (!invoiceId) return;
    getToken().then(tok =>
      fetch(`${API_BASE}/invoices/${invoiceId}/file-url`, { headers: { Authorization: `Bearer ${tok}` } })
    ).then(r => r.json()).then(d => {
      if (d.url) setFileUrl(d.url);
      else setFileUrlErr(true);
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
      if (Object.keys(fields).length === 0) {
        setSaved(true);
        return;
      }
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fields, line_items: [] }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
            ) : fileUrlErr ? (
              <div style={s.viewerFallback}>
                Document preview isn't available (file storage not configured, or the file is missing).
                You can still fill in fields from a copy of the invoice you have on hand.
              </div>
            ) : (
              <div style={s.viewerFallback}>Loading document…</div>
            )}
          </div>

          {/* ── Fill-in-the-blanks form ── */}
          <div style={s.formPane}>
            <div style={s.formScroll}>
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