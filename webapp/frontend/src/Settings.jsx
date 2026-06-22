/**
 * Settings.jsx
 * ============
 * Org settings page. Currently: Drive folder registration.
 * This is the UI replacement for "run curl PUT /org/drive-folder in a terminal"
 * — real users/clients never open a terminal; they use this form instead.
 *
 * Props:
 *   getToken — async () => string, from Clerk's useAuth()
 */

import { useState, useEffect } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

export default function Settings({ getToken }) {
  const [currentFolder, setCurrentFolder] = useState(null);
  const [inputFolder, setInputFolder]     = useState("");
  const [loading, setLoading]             = useState(true);
  const [saving, setSaving]               = useState(false);
  const [syncing, setSyncing]             = useState(false);
  const [message, setMessage]             = useState(null); // {type: "ok"|"err", text}

  // Business description + HSN profile state
  const [bizDesc, setBizDesc]               = useState("");
  const [bizDescSaved, setBizDescSaved]     = useState("");
  const [bizDescSaving, setBizDescSaving]   = useState(false);

  // Saved profile (from GET /org/hsn-profile — source of truth after any apply)
  const [hsnProfile, setHsnProfile]         = useState(null);
  // Preview from POST /org/hsn-profile/generate — shown before confirming apply
  const [hsnPreview, setHsnPreview]         = useState(null);
  // Which codes the user chose to remove from the "no longer suggested" diff list
  const [removalSet, setRemovalSet]         = useState(new Set());

  const [hsnGenerating, setHsnGenerating]   = useState(false);
  const [hsnApplying, setHsnApplying]       = useState(false);
  const [hsnMessage, setHsnMessage]         = useState(null);

  // Manual code add form
  const [manualCode, setManualCode]         = useState("");
  const [manualCodeType, setManualCodeType] = useState("HSN");
  const [manualDesc, setManualDesc]         = useState("");
  const [manualAdding, setManualAdding]     = useState(false);
  const [removingCode, setRemovingCode]     = useState(null);

  async function authedFetch(path, options = {}) {
    const tok = await getToken();
    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { ...options.headers, Authorization: `Bearer ${tok}` },
    });
    return res;
  }

  // Load current registered folder + biz description + HSN profile on mount
  useEffect(() => {
    authedFetch("/org/drive-folder")
      .then(r => r.json())
      .then(d => {
        setCurrentFolder(d.drive_folder_id || null);
        setInputFolder(d.drive_folder_id || "");
      })
      .catch(() => {})
      .finally(() => setLoading(false));

    authedFetch("/org/settings")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.business_description) { setBizDesc(d.business_description); setBizDescSaved(d.business_description); } })
      .catch(() => {});

    // GET /org/hsn-profile returns { expected_hsn_codes, ambiguous_hsn_codes, has_profile }
    authedFetch("/org/hsn-profile")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.has_profile) setHsnProfile(d); })
      .catch(() => {});
  }, []);

  async function handleSave() {
    if (!inputFolder.trim()) return;
    setSaving(true);
    setMessage(null);
    try {
      const res = await authedFetch("/org/drive-folder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_id: inputFolder.trim() }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      setCurrentFolder(d.drive_folder_id);
      setMessage({ type: "ok", text: "Drive folder saved." });
    } catch (e) {
      setMessage({ type: "err", text: `Save failed: ${e.message}` });
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveBizDesc() {
    if (!bizDesc.trim()) return;
    setBizDescSaving(true);
    setHsnMessage(null);
    try {
      const res = await authedFetch("/org/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ business_description: bizDesc.trim() }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setBizDescSaved(bizDesc.trim());
      setHsnMessage({ type: "ok", text: "Business description saved." });
    } catch (e) {
      setHsnMessage({ type: "err", text: `Save failed: ${e.message}` });
    } finally {
      setBizDescSaving(false);
    }
  }

  async function handleGenerateHsn() {
    if (!bizDescSaved) return;
    setHsnGenerating(true);
    setHsnMessage(null);
    try {
      const res = await authedFetch("/org/hsn-profile/generate", { method: "POST" });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
      // d = { expected_codes, ambiguous_codes, diff: { new_codes, codes_no_longer_suggested, unchanged_codes } }
      setHsnPreview(d);
      setRemovalSet(new Set()); // reset removal choices for fresh preview
      setHsnMessage({ type: "ok", text: "Preview generated — review below and click Apply to save." });
    } catch (e) {
      setHsnMessage({ type: "err", text: `Generation failed: ${e.message}` });
    } finally {
      setHsnGenerating(false);
    }
  }

  async function handleApplyHsn() {
    if (!hsnPreview) return;
    setHsnApplying(true);
    setHsnMessage(null);
    try {
      const res = await authedFetch("/org/hsn-profile/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_codes: hsnPreview.expected_codes,
          ambiguous_codes: hsnPreview.ambiguous_codes,
          remove_codes: [...removalSet],
        }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
      setHsnProfile(d);
      setHsnPreview(null);
      setRemovalSet(new Set());
      const expCount = d.expected_hsn_codes?.length ?? 0;
      const ambCount = d.ambiguous_hsn_codes?.length ?? 0;
      setHsnMessage({ type: "ok", text: `Profile saved — ${expCount} expected code${expCount !== 1 ? "s" : ""}, ${ambCount} to watch.` });
    } catch (e) {
      setHsnMessage({ type: "err", text: `Apply failed: ${e.message}` });
    } finally {
      setHsnApplying(false);
    }
  }

  async function handleAddManualCode() {
    const code = manualCode.trim().toUpperCase();
    if (!code) return;
    setManualAdding(true);
    setHsnMessage(null);
    try {
      const res = await authedFetch("/org/hsn-profile/codes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, code_type: manualCodeType, description: manualDesc.trim() || null }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
      setHsnProfile(d);
      setManualCode("");
      setManualDesc("");
      setHsnMessage({ type: "ok", text: `Code ${code} added.` });
    } catch (e) {
      setHsnMessage({ type: "err", text: `Add failed: ${e.message}` });
    } finally {
      setManualAdding(false);
    }
  }

  async function handleRemoveCode(code) {
    setRemovingCode(code);
    setHsnMessage(null);
    try {
      const res = await authedFetch(`/org/hsn-profile/codes/${encodeURIComponent(code)}`, { method: "DELETE" });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
      setHsnProfile(d);
    } catch (e) {
      setHsnMessage({ type: "err", text: `Remove failed: ${e.message}` });
    } finally {
      setRemovingCode(null);
    }
  }

  async function handleSync() {
    setSyncing(true);
    setMessage(null);
    try {
      const res = await authedFetch("/drive/sync", { method: "POST" });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
      setMessage({
        type: "ok",
        text: `Sync complete — ${d.new_files_processed} new file(s) processed.`,
      });
    } catch (e) {
      setMessage({ type: "err", text: `Sync failed: ${e.message}` });
    } finally {
      setSyncing(false);
    }
  }

  const s = styles;

  return (
    <div style={s.root}>
      <div style={s.card}>
        <h2 style={s.heading}>Settings</h2>

        {/* ── Drive folder ── */}
        <div style={s.section}>
          <div style={s.sectionTitle}>Google Drive folder</div>
          <p style={s.hint}>
            Share a Drive folder with the service account, then paste the
            folder ID here. The folder ID is the last part of the folder's
            URL: <code style={s.code}>drive.google.com/drive/folders/<b>THIS_PART</b></code>.
            Once registered, use "Sync now" to pull new invoices — or they'll
            be picked up automatically every 15 minutes in production.
          </p>

          {loading ? (
            <div style={{ color: "#9CA3AF", fontSize: 13 }}>Loading…</div>
          ) : (
            <>
              {currentFolder && (
                <div style={s.currentFolder}>
                  <span style={s.currentLabel}>Current folder</span>
                  <code style={s.currentCode}>{currentFolder}</code>
                </div>
              )}

              <div style={s.inputRow}>
                <input
                  type="text"
                  placeholder="Paste Drive folder ID"
                  value={inputFolder}
                  onChange={e => setInputFolder(e.target.value)}
                  style={s.input}
                />
                <button
                  onClick={handleSave}
                  disabled={saving || !inputFolder.trim()}
                  style={s.primaryBtn}
                >
                  {saving ? "Saving…" : "Save"}
                </button>
                {currentFolder && (
                  <button
                    onClick={handleSync}
                    disabled={syncing || saving}
                    style={s.secondaryBtn}
                  >
                    {syncing ? "Syncing…" : "Sync now"}
                  </button>
                )}
              </div>
            </>
          )}

          {message && (
            <div style={message.type === "ok" ? s.msgOk : s.msgErr}>
              {message.text}
            </div>
          )}
        </div>

        {/* ── Business description + HSN profile ── */}
        <div style={s.section}>
          <div style={s.sectionTitle}>Business profile</div>
          <p style={s.hint}>
            Describe your business in one sentence (e.g. "furniture manufacturing and retail shop").
            Used to generate a list of HSN/SAC codes expected on your purchase invoices — line items
            are then automatically badged as expected, ambiguous, or unknown.
          </p>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexWrap: "wrap", marginBottom: 12 }}>
            <textarea
              rows={2}
              placeholder="e.g. furniture manufacturing and retail shop selling chairs, tables and wooden fixtures"
              value={bizDesc}
              onChange={e => setBizDesc(e.target.value)}
              style={{ ...s.input, flex: 1, minWidth: 240, resize: "vertical", fontFamily: "inherit", lineHeight: 1.5 }}
            />
            <button
              onClick={handleSaveBizDesc}
              disabled={bizDescSaving || !bizDesc.trim() || bizDesc.trim() === bizDescSaved}
              style={s.primaryBtn}
            >
              {bizDescSaving ? "Saving…" : "Save"}
            </button>
          </div>

          {bizDescSaved && (
            <button
              onClick={handleGenerateHsn}
              disabled={hsnGenerating}
              style={s.secondaryBtn}
            >
              {hsnGenerating
                ? "Generating preview…"
                : hsnProfile?.has_profile
                  ? "Regenerate HSN profile"
                  : "Generate HSN profile"}
            </button>
          )}

          {/* ── Preview diff (before apply) ── */}
          {hsnPreview && (
            <div style={{ marginTop: 16, background: "#F0F9FF", border: "1px solid #BAE6FD", borderRadius: 10, padding: "16px 18px" }}>
              <div style={{ fontWeight: 700, fontSize: 13, color: "#0369A1", marginBottom: 4 }}>
                Preview — not saved yet
              </div>
              <p style={{ fontSize: 12, color: "#0369A1", margin: "0 0 12px", lineHeight: 1.5 }}>
                Review the proposed changes below, then click Apply to save them.
                Manually-added codes are never removed by a regeneration.
              </p>

              {hsnPreview.diff?.new_codes?.length > 0 && (
                <div style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "#15803D", marginBottom: 4 }}>
                    + {hsnPreview.diff.new_codes.length} new code{hsnPreview.diff.new_codes.length !== 1 ? "s" : ""} to add
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {[...hsnPreview.expected_codes, ...hsnPreview.ambiguous_codes]
                      .filter(c => hsnPreview.diff.new_codes.includes(c.code))
                      .map(c => (
                        <span
                          key={c.code}
                          title={c.description + (c.reason ? `\n⚠ ${c.reason}` : "")}
                          style={{
                            fontFamily: "monospace", fontSize: 11, borderRadius: 4, padding: "2px 7px",
                            background: hsnPreview.ambiguous_codes.some(a => a.code === c.code) ? "#FEF9C3" : "#DCFCE7",
                            color: hsnPreview.ambiguous_codes.some(a => a.code === c.code) ? "#854D0E" : "#15803D",
                          }}
                        >
                          {c.code}
                          {hsnPreview.ambiguous_codes.some(a => a.code === c.code) && " ?"}
                        </span>
                      ))}
                  </div>
                </div>
              )}

              {hsnPreview.diff?.codes_no_longer_suggested?.length > 0 && (
                <div style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "#B91C1C", marginBottom: 4 }}>
                    Codes no longer suggested (tick to remove):
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {hsnPreview.diff.codes_no_longer_suggested.map(code => (
                      <label key={code} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12, cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={removalSet.has(code)}
                          onChange={e => {
                            const next = new Set(removalSet);
                            e.target.checked ? next.add(code) : next.delete(code);
                            setRemovalSet(next);
                          }}
                        />
                        <span style={{ fontFamily: "monospace", fontSize: 11, background: "#FEE2E2", color: "#B91C1C", borderRadius: 4, padding: "2px 7px" }}>
                          {code}
                        </span>
                      </label>
                    ))}
                  </div>
                  <p style={{ fontSize: 11, color: "#6B7280", margin: "6px 0 0" }}>
                    Unticked codes stay in your profile unchanged.
                  </p>
                </div>
              )}

              <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                <button onClick={handleApplyHsn} disabled={hsnApplying} style={s.primaryBtn}>
                  {hsnApplying ? "Saving…" : "Apply & save"}
                </button>
                <button
                  onClick={() => { setHsnPreview(null); setRemovalSet(new Set()); }}
                  style={s.secondaryBtn}
                >
                  Discard preview
                </button>
              </div>
            </div>
          )}

          {/* ── Saved profile view + edit ── */}
          {hsnProfile?.has_profile && !hsnPreview && (
            <div style={{ marginTop: 16 }}>
              {/* Expected codes */}
              {hsnProfile.expected_hsn_codes?.length > 0 && (
                <div style={{ marginBottom: 12 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "#15803D", marginBottom: 6 }}>
                    Expected HSN/SAC codes ({hsnProfile.expected_hsn_codes.length})
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {hsnProfile.expected_hsn_codes.map(item => (
                      <div
                        key={item.code}
                        title={item.description}
                        style={{ display: "flex", alignItems: "center", gap: 3,
                          fontFamily: "monospace", fontSize: 11, background: "#DCFCE7",
                          color: "#15803D", borderRadius: 4, padding: "2px 4px 2px 7px" }}
                      >
                        {item.code}
                        {item.source === "manual" && (
                          <span title="Manually added" style={{ fontSize: 9, opacity: 0.7, marginLeft: 2 }}>M</span>
                        )}
                        <button
                          onClick={() => handleRemoveCode(item.code)}
                          disabled={removingCode === item.code}
                          title="Remove"
                          style={{ background: "none", border: "none", cursor: "pointer", padding: "0 2px",
                            color: "#15803D", fontSize: 12, lineHeight: 1, opacity: 0.6,
                            display: "flex", alignItems: "center" }}
                        >
                          {removingCode === item.code ? "…" : "×"}
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Ambiguous codes */}
              {hsnProfile.ambiguous_hsn_codes?.length > 0 && (
                <div style={{ marginBottom: 12 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "#854D0E", marginBottom: 6 }}>
                    Watch — classify on first use ({hsnProfile.ambiguous_hsn_codes.length})
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {hsnProfile.ambiguous_hsn_codes.map(item => (
                      <div
                        key={item.code}
                        title={item.description + (item.reason ? `\n${item.reason}` : "")}
                        style={{ display: "flex", alignItems: "center", gap: 3,
                          fontFamily: "monospace", fontSize: 11, background: "#FEF9C3",
                          color: "#854D0E", borderRadius: 4, padding: "2px 4px 2px 7px" }}
                      >
                        {item.code}
                        {item.source === "manual" && (
                          <span title="Manually added" style={{ fontSize: 9, opacity: 0.7, marginLeft: 2 }}>M</span>
                        )}
                        <button
                          onClick={() => handleRemoveCode(item.code)}
                          disabled={removingCode === item.code}
                          title="Remove"
                          style={{ background: "none", border: "none", cursor: "pointer", padding: "0 2px",
                            color: "#854D0E", fontSize: 12, lineHeight: 1, opacity: 0.6,
                            display: "flex", alignItems: "center" }}
                        >
                          {removingCode === item.code ? "…" : "×"}
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Manual add form */}
              <div style={{ marginTop: 8, padding: "12px 14px", background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: 8 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: "#6B7280", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                  Add a code manually
                </div>
                <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  <input
                    type="text"
                    placeholder="HSN/SAC code"
                    value={manualCode}
                    onChange={e => setManualCode(e.target.value)}
                    style={{ ...s.input, width: 110, flex: "none", fontFamily: "monospace" }}
                  />
                  <select
                    value={manualCodeType}
                    onChange={e => setManualCodeType(e.target.value)}
                    style={{ ...s.input, width: 70, flex: "none", cursor: "pointer" }}
                  >
                    <option value="HSN">HSN</option>
                    <option value="SAC">SAC</option>
                  </select>
                  <input
                    type="text"
                    placeholder="Description (optional)"
                    value={manualDesc}
                    onChange={e => setManualDesc(e.target.value)}
                    style={{ ...s.input, flex: 1, minWidth: 140 }}
                  />
                  <button
                    onClick={handleAddManualCode}
                    disabled={manualAdding || !manualCode.trim()}
                    style={s.primaryBtn}
                  >
                    {manualAdding ? "Adding…" : "Add"}
                  </button>
                </div>
                <p style={{ fontSize: 11, color: "#6B7280", margin: "6px 0 0" }}>
                  Manually-added codes (marked M) are never removed or overwritten by regeneration.
                </p>
              </div>
            </div>
          )}

          {!hsnProfile?.has_profile && !hsnPreview && bizDescSaved && (
            <p style={{ fontSize: 12, color: "#9CA3AF", marginTop: 10 }}>
              No profile yet — click "Generate HSN profile" above.
            </p>
          )}

          {hsnMessage && (
            <div style={{ ...(hsnMessage.type === "ok" ? s.msgOk : s.msgErr), marginTop: 12 }}>
              {hsnMessage.text}
            </div>
          )}
        </div>

        {/* ── How to find the folder ID ── */}
        <div style={s.section}>
          <div style={s.sectionTitle}>How to share the folder</div>
          <ol style={s.steps}>
            <li>In Google Drive, right-click your invoices folder → Share.</li>
            <li>
              Add the service account email as a Viewer:{" "}
              <code style={s.code}>your-service-account@project.iam.gserviceaccount.com</code>
              {" "}(check <code style={s.code}>gdrive_key.json</code> → <code style={s.code}>client_email</code>).
            </li>
            <li>Copy the folder URL from your browser — the ID is the last segment.</li>
            <li>Paste it above and click Save, then Sync now.</li>
          </ol>
        </div>
      </div>
    </div>
  );
}

const styles = {
  root: {
    flex: 1,
    overflow: "auto",
    background: "#F7F8FA",
    padding: "32px 24px",
    fontFamily: "Inter, system-ui, -apple-system, sans-serif",
    fontSize: 13,
    color: "#111318",
  },
  card: {
    background: "#fff",
    border: "1px solid #E5E7EB",
    borderRadius: 10,
    maxWidth: 640,
    padding: "28px 32px",
  },
  heading: {
    margin: "0 0 24px",
    fontSize: 18,
    fontWeight: 700,
    color: "#111318",
    letterSpacing: "-0.01em",
  },
  section: {
    marginBottom: 32,
    paddingBottom: 32,
    borderBottom: "1px solid #F3F4F6",
  },
  sectionTitle: {
    fontWeight: 600,
    fontSize: 13,
    textTransform: "uppercase",
    letterSpacing: "0.05em",
    color: "#6B7280",
    marginBottom: 8,
  },
  hint: {
    margin: "0 0 16px",
    color: "#374151",
    lineHeight: 1.6,
  },
  code: {
    background: "#F3F4F6",
    borderRadius: 4,
    padding: "1px 5px",
    fontFamily: "monospace",
    fontSize: 12,
  },
  currentFolder: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    marginBottom: 12,
    padding: "8px 12px",
    background: "#F0FDF4",
    border: "1px solid #BBF7D0",
    borderRadius: 6,
  },
  currentLabel: {
    fontSize: 11,
    fontWeight: 600,
    color: "#15803D",
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    whiteSpace: "nowrap",
  },
  currentCode: {
    fontFamily: "monospace",
    fontSize: 12,
    color: "#111318",
    wordBreak: "break-all",
  },
  inputRow: {
    display: "flex",
    gap: 8,
    alignItems: "center",
    flexWrap: "wrap",
  },
  input: {
    flex: 1,
    minWidth: 200,
    border: "1px solid #D1D5DB",
    borderRadius: 6,
    padding: "7px 10px",
    fontSize: 13,
    fontFamily: "inherit",
    outline: "none",
    background: "#F9FAFB",
    color: "#111318",
  },
  primaryBtn: {
    background: "#312E81",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    padding: "7px 18px",
    fontSize: 13,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
    whiteSpace: "nowrap",
  },
  secondaryBtn: {
    background: "#fff",
    color: "#312E81",
    border: "1px solid #312E81",
    borderRadius: 6,
    padding: "7px 16px",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
    whiteSpace: "nowrap",
  },
  msgOk: {
    marginTop: 12,
    padding: "8px 12px",
    background: "#F0FDF4",
    border: "1px solid #BBF7D0",
    borderRadius: 6,
    color: "#15803D",
    fontSize: 13,
  },
  msgErr: {
    marginTop: 12,
    padding: "8px 12px",
    background: "#FEF2F2",
    border: "1px solid #FECACA",
    borderRadius: 6,
    color: "#B91C1C",
    fontSize: 13,
  },
  steps: {
    margin: "8px 0 0",
    paddingLeft: 20,
    lineHeight: 2,
    color: "#374151",
  },
};