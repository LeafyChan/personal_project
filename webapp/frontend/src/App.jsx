/**
 * App.jsx
 * =======
 * Top-level shell. Handles:
 * - Login page (redesigned — indigo/white, not "bland monospace button")
 * - Org creation/selection for new users
 * - Top nav with Invoices / Settings tabs + org switcher + user button (logout)
 *
 * Children: InvoiceList, Settings
 */

import { useState } from "react";
import {
  SignedIn,
  SignedOut,
  SignInButton,
  UserButton,
  useAuth,
  useOrganization,
  OrganizationSwitcher,
  CreateOrganization,
} from "@clerk/clerk-react";
import InvoiceList from "./InvoiceList";
import Settings from "./Settings";
import ActivityLog from "./ActivityLog";
import ItcSummary from "./Itcsummary";

// ── Login page ────────────────────────────────────────────────────────────────

function LoginPage() {
  return (
    <div style={ls.root}>
      <div style={ls.panel}>
        {/* Logo mark */}
        <div style={ls.logoWrap}>
          <div style={ls.logoMark}>IIS</div>
        </div>

        <h1 style={ls.heading}>Invoice Intelligence</h1>
        <p style={ls.subheading}>
          GST-aware invoice extraction and compliance tracking for Indian SMEs.
        </p>

        <SignInButton mode="modal">
          <button style={ls.signInBtn}>Sign in to your account →</button>
        </SignInButton>

        <div style={ls.features}>
          {[
            ["📄", "PDF & image extraction", "Digital text, scanned, and handwritten invoices"],
            ["✓", "GST validation", "GSTIN format, amount reconciliation, CGST/SGST/IGST checks"],
            ["🔒", "Full data isolation", "RLS-backed multi-tenant — no cross-client data access ever"],
          ].map(([icon, title, desc]) => (
            <div key={title} style={ls.featureRow}>
              <span style={ls.featureIcon}>{icon}</span>
              <div>
                <div style={ls.featureTitle}>{title}</div>
                <div style={ls.featureDesc}>{desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const ls = {
  root: {
    minHeight: "100vh",
    background: "#312E81",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontFamily: "Inter, system-ui, -apple-system, sans-serif",
    padding: 24,
  },
  panel: {
    background: "#fff",
    borderRadius: 14,
    padding: "48px 44px",
    width: "100%",
    maxWidth: 420,
    boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
  },
  logoWrap: { marginBottom: 24 },
  logoMark: {
    width: 44,
    height: 44,
    borderRadius: 10,
    background: "#312E81",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 12,
    fontWeight: 800,
    letterSpacing: "0.05em",
    color: "#fff",
  },
  heading: {
    margin: "0 0 8px",
    fontSize: 24,
    fontWeight: 700,
    color: "#111318",
    letterSpacing: "-0.02em",
  },
  subheading: {
    margin: "0 0 28px",
    fontSize: 14,
    color: "#6B7280",
    lineHeight: 1.6,
  },
  signInBtn: {
    width: "100%",
    background: "#312E81",
    color: "#fff",
    border: "none",
    borderRadius: 8,
    padding: "12px 20px",
    fontSize: 14,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
    letterSpacing: "-0.01em",
    marginBottom: 32,
  },
  features: { display: "flex", flexDirection: "column", gap: 16 },
  featureRow: { display: "flex", gap: 14, alignItems: "flex-start" },
  featureIcon: { fontSize: 18, lineHeight: 1, marginTop: 1, flexShrink: 0 },
  featureTitle: { fontSize: 13, fontWeight: 600, color: "#111318", marginBottom: 2 },
  featureDesc: { fontSize: 12, color: "#6B7280", lineHeight: 1.5 },
};

// ── Org creation screen ───────────────────────────────────────────────────────

function CreateOrgScreen() {
  return (
    <div style={{
      minHeight: "100vh",
      background: "#F7F8FA",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: "Inter, system-ui, sans-serif",
      flexDirection: "column",
      gap: 20,
      padding: 24,
    }}>
      <div style={{
        background: "#fff",
        border: "1px solid #E5E7EB",
        borderRadius: 12,
        padding: "32px 36px",
        maxWidth: 480,
        width: "100%",
      }}>
        <div style={{ fontSize: 13, color: "#6B7280", marginBottom: 16 }}>
          You're signed in, but not part of an organization yet. Create one to
          get started — this becomes your isolated workspace.
        </div>
        <CreateOrganization />
      </div>
    </div>
  );
}

// ── Top navigation ────────────────────────────────────────────────────────────

function TopNav({ activeTab, setActiveTab }) {
  return (
    <div style={ns.bar}>
      <div style={ns.left}>
        <div style={ns.logoMark}>IIS</div>
        <span style={ns.appName}>Invoice Intelligence</span>
        <nav style={ns.tabs}>
          {["Invoices", "ITC Summary", "Activity", "Settings"].map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              style={{
                ...ns.tab,
                ...(activeTab === tab ? ns.tabActive : ns.tabInactive),
              }}
            >
              {tab}
            </button>
          ))}
        </nav>
      </div>
      <div style={ns.right}>
        <OrganizationSwitcher
          appearance={{
            elements: {
              organizationSwitcherTrigger: {
                background: "rgba(255,255,255,0.12)",
                border: "1px solid rgba(255,255,255,0.2)",
                borderRadius: 6,
                color: "#fff",
                fontSize: 13,
                padding: "5px 10px",
              },
            },
          }}
        />
        <UserButton afterSignOutUrl="/" />
      </div>
    </div>
  );
}

const ns = {
  bar: {
    background: "#312E81",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "0 20px",
    height: 52,
    flexShrink: 0,
  },
  left: { display: "flex", alignItems: "center", gap: 16 },
  right: { display: "flex", alignItems: "center", gap: 12 },
  logoMark: {
    width: 28,
    height: 28,
    borderRadius: 6,
    background: "rgba(255,255,255,0.15)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 9,
    fontWeight: 800,
    color: "#fff",
    letterSpacing: "0.05em",
    fontFamily: "Inter, system-ui, sans-serif",
  },
  appName: {
    fontSize: 14,
    fontWeight: 600,
    color: "#fff",
    letterSpacing: "-0.01em",
    fontFamily: "Inter, system-ui, sans-serif",
  },
  tabs: { display: "flex", gap: 2 },
  tab: {
    background: "none",
    border: "none",
    cursor: "pointer",
    fontSize: 13,
    fontFamily: "Inter, system-ui, sans-serif",
    padding: "6px 12px",
    borderRadius: 6,
  },
  tabActive: {
    background: "rgba(255,255,255,0.15)",
    color: "#fff",
    fontWeight: 600,
  },
  tabInactive: {
    color: "rgba(255,255,255,0.65)",
    fontWeight: 400,
  },
};

// ── App shell ─────────────────────────────────────────────────────────────────

function AppShell() {
  const { getToken } = useAuth();
  const { organization } = useOrganization();
  const [activeTab, setActiveTab] = useState("Invoices");

  if (!organization) return <CreateOrgScreen />;

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "100vh",
      overflow: "hidden",
      fontFamily: "Inter, system-ui, sans-serif",
    }}>
      <TopNav activeTab={activeTab} setActiveTab={setActiveTab} />
      {activeTab === "Invoices" && <InvoiceList getToken={getToken} />}
      {activeTab === "ITC Summary" && <ItcSummary getToken={getToken} />}
      {activeTab === "Activity" && <ActivityLog getToken={getToken} />}
      {activeTab === "Settings" && <Settings getToken={getToken} />}
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

export default function App() {
  return (
    <>
      <SignedOut>
        <LoginPage />
      </SignedOut>
      <SignedIn>
        <AppShell />
      </SignedIn>
    </>
  );
}