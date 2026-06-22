/**
 * ItcSummary.jsx
 * ==============
 * Shows total claimable ITC across all invoices, broken down by vendor and
 * by HSN-profile status (expected / ambiguous / manual / unknown), plus an
 * explicit list of line items flagged ambiguous so the total isn't just a
 * number with no way to see what's driving any one slice of it.
 *
 * Backed by GET /itc-summary (see PROJECT_LOG.md Phase 6.5). The endpoint
 * computes claimable amount as line_tax x (business_use_percent / 100):
 *   - line_tax uses the line's own printed rate (line_tax_rate_percent)
 *     when one exists, otherwise apportions the invoice's bill-level
 *     total_gst_amount by this line's share of taxable_amount.
 *   - business_use_percent defaults to 100 (fully business use) and is
 *     edited per line on the invoice detail view for any item with a
 *     personal/mixed-use portion - this view surfaces the resulting total,
 *     it doesn't decide that percentage itself.
 *
 * Props:
 *   getToken — async () => string, from Clerk's useAuth()
 */

import { useState, useEffect, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function formatINR(n) {
  return `₹${Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const STATUS_LABEL = {
  expected: "Expected (in HSN profile)",
  ambiguous: "Ambiguous (needs review)",
  manual: "Manually classified",
  unknown: "Unknown (not in HSN profile)",
};
const STATUS_COLOR = {
  expected: "#15803D",
  ambiguous: "#854D0E",
  manual: "#1D4ED8",
  unknown: "#6B7280",
};

export default function ItcSummary({ getToken }) {
  const [data, setData]       = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr]         = useState(null);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo]     = useState("");

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const tok = await getToken();
      const params = new URLSearchParams();
      if (dateFrom) params.set("date_from", dateFrom);
      if (dateTo) params.set("date_to", dateTo);
      const res = await fetch(`${API_BASE}/itc-summary?${params}`, {
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [getToken, dateFrom, dateTo]);

  useEffect(() => { fetchSummary(); }, [fetchSummary]);

  const s = styles;
  const maxVendorAmount = data ? Math.max(1, ...Object.values(data.by_vendor)) : 1;
  const totalByStatus = data
    ? Object.values(data.by_hsn_status).reduce((a, b) => a + b, 0)
    : 0;

  return (
    <div style={s.root}>
      <div style={s.header}>
        <div>
          <h2 style={s.heading}>ITC Summary</h2>
          <p style={s.subheading}>
            Total claimable Input Tax Credit across all processed invoices.
          </p>
        </div>
        <div style={s.dateFilters}>
          <input
            type="date"
            value={dateFrom}
            onChange={e => setDateFrom(e.target.value)}
            style={s.dateInput}
            aria-label="From date"
          />
          <span style={{ color: "#9CA3AF", fontSize: 12 }}>to</span>
          <input
            type="date"
            value={dateTo}
            onChange={e => setDateTo(e.target.value)}
            style={s.dateInput}
            aria-label="To date"
          />
          {(dateFrom || dateTo) && (
            <button
              onClick={() => { setDateFrom(""); setDateTo(""); }}
              style={s.clearDatesBtn}
            >
              Clear
            </button>
          )}
          <button onClick={fetchSummary} disabled={loading} style={s.refreshBtn} title="Refresh">
            ↻ Refresh
          </button>
        </div>
      </div>

      {err && (
        <div style={s.errBanner}>
          Failed to load ITC summary: {err}
          <button onClick={fetchSummary} style={s.retryBtn}>Retry</button>
        </div>
      )}

      {loading && !data && (
        <div style={s.loadingBox}>Loading…</div>
      )}

      {!loading && data && (
        <>
          {/* ── Total card ── */}
          <div style={s.totalCard}>
            <div style={s.totalLabel}>Total claimable ITC</div>
            <div style={s.totalAmount}>{formatINR(data.total_claimable_itc)}</div>
            <div style={s.totalMeta}>
              Across {data.line_items_counted} line item{data.line_items_counted !== 1 ? "s" : ""}
            </div>
            <div style={s.totalNote}>{data.note}</div>
          </div>

          <div style={s.grid}>
            {/* ── By HSN status ── */}
            <div style={s.panel}>
              <div style={s.panelTitle}>By HSN profile status</div>
              {Object.entries(data.by_hsn_status).map(([status, amount]) => {
                const pct = totalByStatus > 0 ? (amount / totalByStatus) * 100 : 0;
                return (
                  <div key={status} style={s.statusRow}>
                    <div style={s.statusRowTop}>
                      <span style={{ color: STATUS_COLOR[status], fontWeight: 600, fontSize: 12 }}>
                        {STATUS_LABEL[status]}
                      </span>
                      <span style={{ fontSize: 12, fontWeight: 600, color: "#111318" }}>
                        {formatINR(amount)}
                      </span>
                    </div>
                    <div style={s.barTrack}>
                      <div style={{ ...s.barFill, width: `${pct}%`, background: STATUS_COLOR[status] }} />
                    </div>
                  </div>
                );
              })}
              {data.by_hsn_status.unknown > 0 && (
                <p style={s.helperNote}>
                  "Unknown" line items have an HSN code not in your saved
                  business profile (Settings → Business profile). Add it
                  there if it's a normal purchase for your business.
                </p>
              )}
            </div>

            {/* ── By vendor ── */}
            <div style={s.panel}>
              <div style={s.panelTitle}>By vendor</div>
              {Object.keys(data.by_vendor).length === 0 ? (
                <p style={s.emptyText}>No data for this period.</p>
              ) : (
                Object.entries(data.by_vendor).map(([vendor, amount]) => (
                  <div key={vendor} style={s.vendorRow}>
                    <div style={s.vendorRowTop}>
                      <span style={s.vendorName} title={vendor}>{vendor}</span>
                      <span style={s.vendorAmount}>{formatINR(amount)}</span>
                    </div>
                    <div style={s.barTrack}>
                      <div style={{ ...s.barFill, width: `${(amount / maxVendorAmount) * 100}%`, background: "#312E81" }} />
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* ── Ambiguous lines needing review ── */}
          {data.ambiguous_lines_needing_review.length > 0 && (
            <div style={s.ambiguousPanel}>
              <div style={s.ambiguousTitle}>
                ⚠ {data.ambiguous_lines_needing_review.length} line item(s) flagged ambiguous
              </div>
              <p style={s.ambiguousHint}>
                These use an HSN code your business profile marked "watch — classify
                on first use" (e.g. could be raw material or a fixed asset depending on
                how it's actually used). Included in the total above, but worth a look.
              </p>
              <table style={s.ambiguousTable}>
                <thead>
                  <tr>
                    <th style={s.ambiguousTh}>HSN code</th>
                    <th style={s.ambiguousTh}>Claimable</th>
                    <th style={s.ambiguousTh}>Invoice</th>
                  </tr>
                </thead>
                <tbody>
                  {data.ambiguous_lines_needing_review.map((row, i) => (
                    <tr key={i}>
                      <td style={s.ambiguousTd}><code>{row.hsn_code || "—"}</code></td>
                      <td style={s.ambiguousTd}>{formatINR(row.claimable)}</td>
                      <td style={{ ...s.ambiguousTd, fontSize: 11, color: "#9CA3AF" }}>
                        {row.invoice_id}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
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
    marginBottom: 24,
  },
  heading: {
    margin: "0 0 4px",
    fontSize: 18,
    fontWeight: 700,
    color: "#111318",
    letterSpacing: "-0.01em",
  },
  subheading: {
    margin: 0,
    fontSize: 13,
    color: "#6B7280",
  },
  dateFilters: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    flexWrap: "wrap",
  },
  dateInput: {
    border: "1px solid #D1D5DB",
    borderRadius: 6,
    padding: "6px 8px",
    fontSize: 12,
    fontFamily: "inherit",
    background: "#fff",
    color: "#111318",
  },
  clearDatesBtn: {
    background: "none",
    border: "none",
    color: "#6B7280",
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "inherit",
    textDecoration: "underline",
  },
  refreshBtn: {
    background: "#fff",
    border: "1px solid #312E81",
    borderRadius: 6,
    color: "#312E81",
    fontSize: 12,
    fontWeight: 500,
    padding: "6px 12px",
    cursor: "pointer",
    fontFamily: "inherit",
    whiteSpace: "nowrap",
  },
  errBanner: {
    background: "#FEF2F2",
    border: "1px solid #FECACA",
    borderRadius: 8,
    color: "#B91C1C",
    padding: "10px 14px",
    marginBottom: 16,
    fontSize: 13,
    display: "flex",
    alignItems: "center",
    gap: 12,
  },
  retryBtn: {
    background: "none",
    border: "1px solid #B91C1C",
    borderRadius: 6,
    color: "#B91C1C",
    padding: "3px 10px",
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  loadingBox: {
    color: "#9CA3AF",
    fontSize: 13,
    padding: "40px 0",
    textAlign: "center",
  },
  totalCard: {
    background: "#312E81",
    borderRadius: 12,
    padding: "24px 28px",
    marginBottom: 20,
    color: "#fff",
  },
  totalLabel: {
    fontSize: 12,
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.05em",
    color: "rgba(255,255,255,0.7)",
    marginBottom: 6,
  },
  totalAmount: {
    fontSize: 36,
    fontWeight: 700,
    letterSpacing: "-0.02em",
    fontVariantNumeric: "tabular-nums",
    marginBottom: 4,
  },
  totalMeta: {
    fontSize: 12,
    color: "rgba(255,255,255,0.7)",
    marginBottom: 12,
  },
  totalNote: {
    fontSize: 11,
    color: "rgba(255,255,255,0.55)",
    lineHeight: 1.5,
    borderTop: "1px solid rgba(255,255,255,0.15)",
    paddingTop: 10,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 16,
    marginBottom: 20,
  },
  panel: {
    background: "#fff",
    border: "1px solid #E5E7EB",
    borderRadius: 10,
    padding: "18px 20px",
  },
  panelTitle: {
    fontSize: 12,
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    color: "#6B7280",
    marginBottom: 14,
  },
  statusRow: { marginBottom: 14 },
  statusRowTop: {
    display: "flex",
    justifyContent: "space-between",
    marginBottom: 4,
  },
  vendorRow: { marginBottom: 12 },
  vendorRowTop: {
    display: "flex",
    justifyContent: "space-between",
    gap: 10,
    marginBottom: 4,
  },
  vendorName: {
    fontSize: 12,
    color: "#374151",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    flex: 1,
  },
  vendorAmount: {
    fontSize: 12,
    fontWeight: 600,
    color: "#111318",
    whiteSpace: "nowrap",
  },
  barTrack: {
    height: 6,
    background: "#F3F4F6",
    borderRadius: 3,
    overflow: "hidden",
  },
  barFill: {
    height: "100%",
    borderRadius: 3,
  },
  emptyText: {
    fontSize: 12,
    color: "#9CA3AF",
  },
  helperNote: {
    fontSize: 11,
    color: "#6B7280",
    lineHeight: 1.5,
    marginTop: 10,
    paddingTop: 10,
    borderTop: "1px dashed #E5E7EB",
  },
  ambiguousPanel: {
    background: "#FFFBEB",
    border: "1px solid #FDE68A",
    borderRadius: 10,
    padding: "18px 20px",
  },
  ambiguousTitle: {
    fontSize: 13,
    fontWeight: 700,
    color: "#92400E",
    marginBottom: 4,
  },
  ambiguousHint: {
    margin: "0 0 12px",
    fontSize: 12,
    color: "#92400E",
    lineHeight: 1.5,
  },
  ambiguousTable: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: 12,
  },
  ambiguousTh: {
    textAlign: "left",
    padding: "6px 10px",
    fontWeight: 600,
    color: "#92400E",
    borderBottom: "1px solid #FDE68A",
  },
  ambiguousTd: {
    padding: "6px 10px",
    borderBottom: "1px solid #FEF3C7",
  },
};