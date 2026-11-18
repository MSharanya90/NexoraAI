import React, { useState } from "react";
import "./App.css";

const API_BASE = "http://localhost:8000";

function App() {
  const [url, setUrl] = useState("https://stripe.com");
  const [refreshCache, setRefreshCache] = useState(false);
  const [preferredProvider, setPreferredProvider] = useState("mock");
  const [loading, setLoading] = useState(false);
  const [loadingStep, setLoadingStep] = useState(0);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);
  const [activeTab, setActiveTab] = useState("brand");
  const [copyNotice, setCopyNotice] = useState(false);

  const steps = [
    "Validating URL & SSRF guards...",
    "Extracting HTML & parsing headings...",
    "Analyzing brand & identifying competitors...",
    "Synthesizing SEO & search queries...",
    "Validating Pydantic schemas...",
  ];

  const handleAnalyze = async (e) => {
    if (e) e.preventDefault();
    if (!url) return;

    setError(null);
    setLoading(true);
    setLoadingStep(0);

    const stepInterval = setInterval(() => {
      setLoadingStep((prev) => (prev < steps.length - 1 ? prev + 1 : prev));
    }, 700);

    try {
      const response = await fetch(`${API_BASE}/link-analysis`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url.trim(),
          refresh_cache: refreshCache,
          preferred_provider: preferredProvider || null,
        }),
      });

      clearInterval(stepInterval);
      const resJson = await response.json();

      if (!response.ok || !resJson.success) {
        throw new Error(
          resJson.detail || resJson.error || `Server responded with status ${response.status}`
        );
      }

      setData(resJson);
      setActiveTab("brand");
    } catch (err) {
      clearInterval(stepInterval);
      setError(err.message || "Failed to analyze website. Ensure the backend is running at http://localhost:8000");
    } finally {
      setLoading(false);
    }
  };

  const handleCopyJson = () => {
    if (!data) return;
    navigator.clipboard.writeText(JSON.stringify(data, null, 2));
    setCopyNotice(true);
    setTimeout(() => setCopyNotice(false), 2000);
  };

  const handleExportJson = () => {
    if (!data) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = `link-analysis-${data.brand?.name || "report"}.json`;
    a.click();
    URL.revokeObjectURL(blobUrl);
  };

  return (
    <div className="app-container">
      {/* Sidebar Navigation */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="brand-logo">
            <div className="logo-badge">AI</div>
            <div className="logo-text">
              <h1>Nexora LinkAI</h1>
              <span>Website Intelligence</span>
            </div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-section-title">Analysis Sections</div>
          <button
            className={`nav-btn ${activeTab === "brand" ? "active" : ""}`}
            onClick={() => setActiveTab("brand")}
          >
            <span>🏢</span> Brand Overview
          </button>
          <button
            className={`nav-btn ${activeTab === "competitors" ? "active" : ""}`}
            onClick={() => setActiveTab("competitors")}
          >
            <span>⚔️</span> Competitor Comparison
          </button>
          <button
            className={`nav-btn ${activeTab === "queries" ? "active" : ""}`}
            onClick={() => setActiveTab("queries")}
          >
            <span>🔍</span> Target Search Queries
          </button>
          <button
            className={`nav-btn ${activeTab === "seo" ? "active" : ""}`}
            onClick={() => setActiveTab("seo")}
          >
            <span>📈</span> SEO Recommendations
          </button>
          <button
            className={`nav-btn ${activeTab === "services" ? "active" : ""}`}
            onClick={() => setActiveTab("services")}
          >
            <span>🛠️</span> Top 5 Services
          </button>
          <button
            className={`nav-btn ${activeTab === "audience" ? "active" : ""}`}
            onClick={() => setActiveTab("audience")}
          >
            <span>🎯</span> Target Audience
          </button>
          <button
            className={`nav-btn ${activeTab === "model" ? "active" : ""}`}
            onClick={() => setActiveTab("model")}
          >
            <span>⚡</span> LLM Cost & Telemetry
          </button>
        </nav>

        <div className="sidebar-footer">
          <div>Powered by FastAPI & LLMs</div>
          <div style={{ marginTop: 4, opacity: 0.8 }}>v1.0.0 • Production Ready</div>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="main-content">
        <header className="top-bar">
          <h2>Website Link Intelligence Dashboard</h2>
          <div className="top-bar-meta">
            <span className="badge badge-success">API: Ready</span>
          </div>
        </header>

        <div className="content-body">
          {/* Analyzer Input Card */}
          <section className="analyzer-card">
            <div className="analyzer-title">
              <h3>Analyze Any Public Website</h3>
              <p>
                Extracts verified live HTML, checks security boundaries, and generates evidence-backed business intelligence.
              </p>
            </div>

            <form onSubmit={handleAnalyze}>
              <div className="input-row">
                <input
                  type="url"
                  className="url-input"
                  placeholder="https://example.com"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  disabled={loading}
                  required
                />
                <button type="submit" className="btn-primary" disabled={loading}>
                  {loading ? "Analyzing..." : "Analyze Website"}
                </button>
              </div>

              <div className="controls-row">
                <label className="control-item">
                  <input
                    type="checkbox"
                    checked={refreshCache}
                    onChange={(e) => setRefreshCache(e.target.checked)}
                    disabled={loading}
                  />
                  <span>Bypass Cache (Force Refresh)</span>
                </label>

                <div className="control-item">
                  <span>Provider:</span>
                  <select
                    className="control-select"
                    value={preferredProvider}
                    onChange={(e) => setPreferredProvider(e.target.value)}
                    disabled={loading}
                  >
                    <option value="mock">Offline Mock (Deterministic & Free)</option>
                    <option value="gemini">Google Gemini (Gemini 1.5 Flash)</option>
                    <option value="openai">OpenAI (GPT-4o-mini)</option>
                    <option value="anthropic">Anthropic (Claude 3.5 Haiku)</option>
                  </select>
                </div>
              </div>
            </form>
          </section>

          {/* Loading Indicator */}
          {loading && (
            <div className="loading-box">
              <div className="spinner"></div>
              <h4>{steps[loadingStep]}</h4>
              <p style={{ color: "#64748b", fontSize: "0.85rem", marginTop: 6 }}>
                Extracting live DOM, isolating scripts, and formulating structured insights...
              </p>
              <div className="progress-steps">
                {steps.map((st, idx) => (
                  <div key={idx} className={`step-item ${idx <= loadingStep ? "active" : ""}`}>
                    <span>{idx <= loadingStep ? "✓" : "○"}</span>
                    <span>Step {idx + 1}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Error Banner */}
          {error && (
            <div className="error-banner">
              <h4>Extraction or Analysis Error</h4>
              <p>{error}</p>
            </div>
          )}

          {/* Analysis Results Display */}
          {data && !loading && (
            <div>
              {/* Results Top Header */}
              <div className="results-header">
                <div className="results-meta">
                  <h3>
                    <span>{data.brand?.name || "Website Analysis"}</span>
                    <span className="badge badge-info">{data.brand?.category || "Technology"}</span>
                  </h3>
                  <a
                    href={data.website}
                    target="_blank"
                    rel="noreferrer"
                    className="website-link"
                  >
                    🔗 {data.website}
                  </a>

                  <div className="metrics-strip">
                    <span className="metric-pill">
                      📄 Pages Analyzed: {data.extraction?.pages_analyzed || 1}
                    </span>
                    <span className="metric-pill">
                      🔤 Content Length: {data.extraction?.content_length?.toLocaleString() || 0} chars
                    </span>
                    <span className="metric-pill">
                      ⏱️ Latency: {data.llm_metadata?.latency_ms || 0} ms
                    </span>
                    <span className="metric-pill">
                      💰 Cost: ${data.llm_metadata?.estimated_cost_usd?.toFixed(6) || "0.000000"}
                    </span>
                    {data.llm_metadata?.cached && (
                      <span className="metric-pill" style={{ background: "#dcfce7", color: "#166534" }}>
                        ⚡ Served from Cache
                      </span>
                    )}
                  </div>
                </div>

                <div className="action-buttons">
                  <button className="btn-secondary" onClick={handleCopyJson}>
                    {copyNotice ? "✓ Copied!" : "📋 Copy JSON"}
                  </button>
                  <button className="btn-secondary" onClick={handleExportJson}>
                    💾 Export JSON
                  </button>
                </div>
              </div>

              {/* SECTION 1: BRAND OVERVIEW */}
              {activeTab === "brand" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>🏢 Brand and Business Overview</h3>
                    <span className="badge badge-info">Website Verified</span>
                  </div>

                  <div className="kv-list">
                    <div className="kv-item">
                      <div className="kv-label">Business Executive Summary</div>
                      <div className="kv-value">{data.brand?.summary}</div>
                    </div>
                    <div className="kv-item">
                      <div className="kv-label">Market Positioning</div>
                      <div className="kv-value">{data.brand?.business_positioning || "N/A"}</div>
                    </div>
                    <div className="kv-item">
                      <div className="kv-label">Target Market</div>
                      <div className="kv-value">{data.brand?.target_market || "N/A"}</div>
                    </div>
                  </div>

                  <div style={{ marginTop: 24 }}>
                    <div className="kv-label" style={{ marginBottom: 12 }}>Core Competitive Advantages</div>
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                      {(data.brand?.competitive_advantages || []).map((adv, i) => (
                        <div
                          key={i}
                          style={{
                            background: "#eff6ff",
                            border: "1px solid #bfdbfe",
                            borderRadius: 8,
                            padding: "8px 14px",
                            fontSize: "0.85rem",
                            color: "#1e40af",
                            fontWeight: 500,
                          }}
                        >
                          ✓ {adv}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* SECTION 2: COMPETITOR COMPARISON */}
              {activeTab === "competitors" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>⚔️ Competitor vs Brand Analysis</h3>
                    <span className="badge badge-info">Evidence Labeling Active</span>
                  </div>

                  <p style={{ color: "#64748b", marginBottom: 20, fontSize: "0.9rem" }}>
                    Competitors are classified with strict provenance:
                    <strong style={{ color: "#1d4ed8" }}> website-derived</strong>,
                    <strong style={{ color: "#065f46" }}> externally verified</strong>, or
                    <strong style={{ color: "#92400e" }}> ai-generated suggestion</strong>.
                    Unverified URLs are omitted to prevent hallucinations.
                  </p>

                  <div className="table-container">
                    <table className="styled-table">
                      <thead>
                        <tr>
                          <th>Competitor</th>
                          <th>Classification</th>
                          <th>Overview</th>
                          <th>Strengths</th>
                          <th>Weaknesses / Gaps</th>
                          <th>Strategic Comparison & Opportunities</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(data.analysis?.competitor_analysis?.competitors || []).map((comp, idx) => {
                          const statusClass =
                            comp.verification_status === "website-derived"
                              ? "badge-provenance-derived"
                              : comp.verification_status === "externally verified"
                              ? "badge-provenance-verified"
                              : "badge-provenance-suggestion";

                          return (
                            <tr key={idx}>
                              <td>
                                <strong>{comp.competitor_name}</strong>
                                {comp.website_url ? (
                                  <div style={{ marginTop: 4 }}>
                                    <a
                                      href={comp.website_url}
                                      target="_blank"
                                      rel="noreferrer"
                                      style={{ color: "#2563eb", fontSize: "0.78rem" }}
                                    >
                                      Visit site ↗
                                    </a>
                                  </div>
                                ) : (
                                  <div style={{ color: "#94a3b8", fontSize: "0.75rem", marginTop: 4 }}>
                                    URL Unverified
                                  </div>
                                )}
                              </td>
                              <td>
                                <span className={`badge ${statusClass}`}>
                                  {comp.verification_status}
                                </span>
                              </td>
                              <td style={{ maxWidth: 260 }}>{comp.business_overview}</td>
                              <td>
                                <ul style={{ paddingLeft: 16 }}>
                                  {(comp.strengths || []).map((s, i) => (
                                    <li key={i}>{s}</li>
                                  ))}
                                </ul>
                              </td>
                              <td>
                                <ul style={{ paddingLeft: 16 }}>
                                  {(comp.weaknesses_or_gaps || []).map((w, i) => (
                                    <li key={i}>{w}</li>
                                  ))}
                                </ul>
                              </td>
                              <td style={{ maxWidth: 280 }}>
                                <p style={{ marginBottom: 6 }}>{comp.comparison_with_brand}</p>
                                <div style={{ fontSize: "0.8rem", color: "#2563eb" }}>
                                  <strong>Opportunity:</strong>{" "}
                                  {(comp.competitive_opportunities || []).join(", ")}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* SECTION 3: TARGET SEARCH QUERIES */}
              {activeTab === "queries" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>🔍 Top 5 Target Search Queries Across Platforms</h3>
                    <span className="badge badge-info">Multi-Channel Strategy</span>
                  </div>

                  <p style={{ color: "#64748b", marginBottom: 20, fontSize: "0.9rem" }}>
                    Quantitative search volumes, CPC, and difficulties are explicitly marked as unavailable
                    without a connected third-party provider to prevent fabricated metrics.
                  </p>

                  <div className="cards-grid">
                    {(data.analysis?.target_search_queries || []).map((q, idx) => (
                      <div key={idx} className="card-item">
                        <div className="card-title">
                          <span>{q.relevant_platform}</span>
                          <span className="badge badge-info">{q.search_intent}</span>
                        </div>
                        <div style={{ fontSize: "1.05rem", fontWeight: 700, color: "#1e293b" }}>
                          "{q.search_query}"
                        </div>
                        <div className="kv-item">
                          <span className="kv-label">Business Relevance</span>
                          <span className="kv-value">{q.business_relevance}</span>
                        </div>
                        <div className="kv-item">
                          <span className="kv-label">Target Audience</span>
                          <span className="kv-value">{q.target_audience}</span>
                        </div>
                        <div className="kv-item">
                          <span className="kv-label">Keyword Category</span>
                          <span className="badge badge-low" style={{ alignSelf: "flex-start" }}>
                            {q.keyword_category}
                          </span>
                        </div>
                        <div style={{ fontSize: "0.75rem", color: "#94a3b8", marginTop: 4 }}>
                          {q.search_volume_status}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* SECTION 4: SEO IMPROVEMENTS */}
              {activeTab === "seo" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>📈 SEO Improvement Recommendations</h3>
                    <span className="badge badge-info">On-Page & Technical</span>
                  </div>

                  {/* Keywords Pill Box */}
                  <div style={{ marginBottom: 24 }}>
                    <div className="kv-label" style={{ marginBottom: 8 }}>Target Keywords</div>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                      <span className="badge badge-high" style={{ padding: "6px 12px", fontSize: "0.85rem" }}>
                        Primary: {data.analysis?.seo_improvements?.primary_keyword}
                      </span>
                      {(data.analysis?.seo_improvements?.secondary_keywords || []).map((sk, i) => (
                        <span key={i} className="badge badge-medium" style={{ padding: "6px 10px" }}>
                          {sk}
                        </span>
                      ))}
                      {(data.analysis?.seo_improvements?.long_tail_keywords || []).map((lk, i) => (
                        <span key={i} className="badge badge-low" style={{ padding: "6px 10px" }}>
                          {lk}
                        </span>
                      ))}
                    </div>
                  </div>

                  {/* Title & Description Comparison */}
                  <div className="compare-grid">
                    <div className="compare-box">
                      <h5>Existing SEO Title</h5>
                      <p>{data.analysis?.seo_improvements?.existing_title || "None Detected"}</p>
                    </div>
                    <div className="compare-box recommended">
                      <h5>Recommended SEO Title</h5>
                      <p><strong>{data.analysis?.seo_improvements?.recommended_title}</strong></p>
                      <small style={{ color: "#166534", display: "block", marginTop: 6 }}>
                        Why: {data.analysis?.seo_improvements?.title_recommendation_reason}
                      </small>
                    </div>
                  </div>

                  <div className="compare-grid">
                    <div className="compare-box">
                      <h5>Existing Meta Description</h5>
                      <p>{data.analysis?.seo_improvements?.existing_meta_description || "None Detected"}</p>
                    </div>
                    <div className="compare-box recommended">
                      <h5>Recommended Meta Description</h5>
                      <p><strong>{data.analysis?.seo_improvements?.meta_description}</strong></p>
                      <small style={{ color: "#166534", display: "block", marginTop: 6 }}>
                        Why: {data.analysis?.seo_improvements?.meta_description_improvement_reason}
                      </small>
                    </div>
                  </div>

                  {/* Verified Technical Issues Table */}
                  <div style={{ marginTop: 28 }}>
                    <h4 style={{ marginBottom: 12, fontSize: "1.05rem" }}>
                      Verified Technical SEO Diagnostics
                    </h4>
                    <div className="table-container">
                      <table className="styled-table">
                        <thead>
                          <tr>
                            <th>Check Name</th>
                            <th>Status</th>
                            <th>Priority</th>
                            <th>Diagnostic Evidence</th>
                            <th>Recommended Action</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(data.analysis?.seo_improvements?.technical_issues || []).map((t, idx) => {
                            const badgeClass =
                              t.status === "PASS"
                                ? "badge-pass"
                                : t.status === "FAIL"
                                ? "badge-fail"
                                : t.status === "WARNING"
                                ? "badge-warn"
                                : "badge-not-tested";

                            const prioClass =
                              t.priority === "HIGH"
                                ? "badge-high"
                                : t.priority === "MEDIUM"
                                ? "badge-medium"
                                : "badge-low";

                            return (
                              <tr key={idx}>
                                <td><strong>{t.issue_name}</strong></td>
                                <td><span className={`badge ${badgeClass}`}>{t.status}</span></td>
                                <td><span className={`badge ${prioClass}`}>{t.priority}</span></td>
                                <td style={{ maxWidth: 280 }}><code>{t.evidence}</code></td>
                                <td style={{ maxWidth: 280 }}>{t.details}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              )}

              {/* SECTION 5: TOP 5 SERVICES */}
              {activeTab === "services" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>🛠️ Top 5 Identified Services</h3>
                    <span className="badge badge-info">Explicit vs Inferred Provenance</span>
                  </div>

                  <div className="cards-grid">
                    {(data.analysis?.top_services || []).map((srv, idx) => {
                      const isExplicit = srv.identification_type === "EXPLICIT";
                      return (
                        <div key={idx} className="card-item">
                          <div className="card-title">
                            <span>{srv.service_name}</span>
                            <span className={`badge ${isExplicit ? "badge-provenance-derived" : "badge-provenance-suggestion"}`}>
                              {srv.identification_type}
                            </span>
                          </div>
                          <p style={{ color: "#334155", fontSize: "0.92rem" }}>{srv.description}</p>
                          <div className="kv-item">
                            <span className="kv-label">Target Industry</span>
                            <span className="kv-value">{srv.target_industry}</span>
                          </div>
                          <div className="kv-item">
                            <span className="kv-label">Core Customer Benefit</span>
                            <span className="kv-value">{srv.main_customer_benefit}</span>
                          </div>
                          <div className="kv-item">
                            <span className="kv-label">Source Evidence</span>
                            <div
                              style={{
                                background: "#f1f5f9",
                                padding: "8px 10px",
                                borderRadius: 6,
                                fontStyle: "italic",
                                fontSize: "0.8rem",
                                color: "#475569",
                              }}
                            >
                              "{srv.source_evidence}"
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* SECTION 6: TARGET AUDIENCE */}
              {activeTab === "audience" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>🎯 Target Audience Identification</h3>
                    <span className="badge badge-info">Persona Mapping</span>
                  </div>

                  <div className="cards-grid" style={{ marginBottom: 28 }}>
                    <div className="card-item">
                      <h4>Primary Audience</h4>
                      <ul style={{ paddingLeft: 18 }}>
                        {(data.analysis?.target_audience?.primary_audience || []).map((p, i) => (
                          <li key={i}>{p}</li>
                        ))}
                      </ul>
                    </div>

                    <div className="card-item">
                      <h4>Secondary Audience</h4>
                      <ul style={{ paddingLeft: 18 }}>
                        {(data.analysis?.target_audience?.secondary_audience || []).map((p, i) => (
                          <li key={i}>{p}</li>
                        ))}
                      </ul>
                    </div>

                    <div className="card-item">
                      <h4>Customer Pain Points</h4>
                      <ul style={{ paddingLeft: 18 }}>
                        {(data.analysis?.target_audience?.customer_pain_points || []).map((p, i) => (
                          <li key={i}>{p}</li>
                        ))}
                      </ul>
                    </div>
                  </div>

                  <h4 style={{ marginBottom: 16 }}>Recommended Marketing Audience Segments</h4>
                  <div className="cards-grid">
                    {(data.analysis?.target_audience?.audience_segments || []).map((seg, idx) => (
                      <div key={idx} className="card-item">
                        <div className="card-title">
                          <span>{seg.segment_name}</span>
                          <span className="badge badge-info">{seg.evidence_level}</span>
                        </div>
                        <p style={{ color: "#475569", fontSize: "0.9rem" }}>{seg.description}</p>
                        <div className="kv-item">
                          <span className="kv-label">Business Model</span>
                          <span className="kv-value">{seg.business_type}</span>
                        </div>
                        <div className="kv-item">
                          <span className="kv-label">Key Requirements</span>
                          <span className="kv-value">{(seg.requirements || []).join(", ")}</span>
                        </div>
                        <div className="kv-item">
                          <span className="kv-label">Mapped Services</span>
                          <span className="kv-value">{(seg.relevant_services || []).join(", ")}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* SECTION 7: LLM MODEL & TELEMETRY */}
              {activeTab === "model" && (
                <div className="section-block">
                  <div className="section-header">
                    <h3>⚡ LLM Telemetry, Official Pricing & Cost Attribution</h3>
                    <span className="badge badge-success">Audited Pricing</span>
                  </div>

                  <div className="cards-grid">
                    <div className="card-item">
                      <span className="kv-label">Active Provider</span>
                      <strong style={{ fontSize: "1.2rem", color: "#1e293b" }}>
                        {data.llm_metadata?.provider?.toUpperCase()}
                      </strong>
                    </div>
                    <div className="card-item">
                      <span className="kv-label">Model Identifier</span>
                      <strong style={{ fontSize: "1.2rem", color: "#1e293b" }}>
                        {data.llm_metadata?.model}
                      </strong>
                    </div>
                    <div className="card-item">
                      <span className="kv-label">Token Consumption</span>
                      <div style={{ fontSize: "1rem" }}>
                        Input: <strong>{data.llm_metadata?.input_tokens?.toLocaleString()}</strong> | Output:{" "}
                        <strong>{data.llm_metadata?.output_tokens?.toLocaleString()}</strong>
                      </div>
                    </div>
                    <div className="card-item">
                      <span className="kv-label">Estimated Cost (USD)</span>
                      <strong style={{ fontSize: "1.3rem", color: "#10b981" }}>
                        ${data.llm_metadata?.estimated_cost_usd?.toFixed(6)}
                      </strong>
                    </div>
                  </div>

                  <div style={{ marginTop: 28, background: "#f8fafc", padding: 20, borderRadius: 10, border: "1px solid #e2e8f0" }}>
                    <h4 style={{ marginBottom: 8, fontSize: "0.95rem" }}>Cost Calculation Methodology</h4>
                    <p style={{ fontSize: "0.85rem", color: "#64748b" }}>
                      Cost is calculated dynamically per API invocation based on official published rates:
                      <code> Cost = (InputTokens / 1M) * InputRate + (OutputTokens / 1M) * OutputRate</code>.
                      For Gemini 1.5 Flash, official rates are $0.075 per 1M input tokens and $0.30 per 1M output tokens.
                    </p>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

export default App;
