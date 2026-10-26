import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  ArrowLeft, ArrowUpRight, BookOpen, Check, CheckCircle2, ChevronRight,
  Copy, Database, FileText, FlaskConical, Code2, Layers3, LoaderCircle,
  Menu, Plus, Quote, Search, Wifi, WifiOff, X,
} from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { demoSearch, initialResponse, papers, type Evidence, type Paper, type SearchResponse } from "./data";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const REPOSITORY_URL = import.meta.env.VITE_REPOSITORY_URL || "";
type View = "search" | "cards" | "about";
type Connection = "checking" | "live" | "demo";
type ReviewCard = {
  id: string;
  question: string;
  answer: string;
  evidence_id: string;
  citation: { title: string; section: string; page: string };
};
type ApiPaper = {
  id?: string;
  paper_id?: string;
  title: string;
  authors: string | string[];
  year: number;
  chunks?: number;
  note_count?: number;
};

function normalizePapers(data: { papers?: ApiPaper[] } | ApiPaper[]): Paper[] {
  const list = Array.isArray(data) ? data : data.papers || [];
  const colors = ["#d76b45", "#4d7468", "#8b7752", "#705d85"];
  return list.map((paper, index) => ({
    id: paper.id || paper.paper_id || `paper-${index}`,
    title: paper.title,
    authors: Array.isArray(paper.authors) ? paper.authors.join("; ") : paper.authors,
    year: paper.year,
    section: "",
    pages: "",
    color: colors[index % colors.length],
    chunks: paper.note_count || paper.chunks || 0,
  }));
}

async function responseJson(response: Response) {
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "请求失败，请检查检索服务。 ");
  return data;
}

export default function App() {
  const [view, setView] = useState<View>("search");
  const [connection, setConnection] = useState<Connection>("checking");
  const [query, setQuery] = useState("Transformer 为什么能并行训练？");
  const [keyword, setKeyword] = useState("");
  const [response, setResponse] = useState<SearchResponse>(initialResponse);
  const [selected, setSelected] = useState<Evidence | null>(initialResponse.results[0]);
  const [activePaper, setActivePaper] = useState("all");
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [livePapers, setLivePapers] = useState(papers);
  const [cardOpen, setCardOpen] = useState<number | null>(0);
  const [cards, setCards] = useState<ReviewCard[] | null>(null);

  const visibleResults = useMemo(
    () => response.results.filter((item) => activePaper === "all" || item.metadata.paper_id === activePaper),
    [response, activePaper],
  );

  useEffect(() => {
    const controller = new AbortController();
    async function connect() {
      try {
        const health = await fetch(`${API_BASE}/api/health`, { signal: controller.signal }).then(responseJson);
        if (health.engine !== "seekdb") throw new Error("未识别检索引擎");
        setConnection("live");
        const [paperData, searchData] = await Promise.all([
          fetch(`${API_BASE}/api/papers`, { signal: controller.signal }).then(responseJson),
          fetch(`${API_BASE}/api/search`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: "Transformer 为什么能并行训练？", n_results: 6 }), signal: controller.signal,
          }).then(responseJson),
        ]);
        setLivePapers(normalizePapers(paperData));
        applyResponse(searchData);
      } catch (error) {
        if (!controller.signal.aborted) setConnection("demo");
      }
    }
    connect();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3600);
    return () => window.clearTimeout(timer);
  }, [notice]);

  function applyResponse(data: SearchResponse) {
    const results = (Array.isArray(data.results) ? data.results : []).map((item) => ({
      ...item,
      metadata: {
        ...item.metadata,
        pages: item.metadata.pages || (item.metadata.page ? `p.${item.metadata.page}` : "未标注页码"),
      },
    }));
    setResponse({ ...data, results });
    setActivePaper("all");
    setSelected(results[0] || null);
    setCards(null);
  }

  async function runSearch(event?: FormEvent) {
    event?.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    try {
      if (connection === "live") {
        const data = await fetch(`${API_BASE}/api/search`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: query.trim(), keyword: keyword.trim() || undefined, n_results: 8 }),
        }).then(responseJson);
        applyResponse(data);
      } else {
        applyResponse(demoSearch(query, keyword));
      }
      setView("search");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "检索失败，请稍后重试。 ");
    } finally {
      setLoading(false);
    }
  }

  async function openCards() {
    if (isLive && response.results.length) {
      setLoading(true);
      try {
        const data = await fetch(`${API_BASE}/api/cards`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            trace_id: response.trace_id,
            question: query.trim() || "本次检索的证据要点",
            evidence_ids: response.results.slice(0, 3).map((item) => item.id),
          }),
        }).then(responseJson);
        setCards(data.cards.map((card: { id: string; question: string; answer: string; evidence_id: string; citations?: { title: string; section: string; page?: number }[] }) => ({
          id: card.id,
          question: card.question,
          answer: card.answer,
          evidence_id: card.evidence_id,
          citation: {
            title: card.citations?.[0]?.title || "未命名文献",
            section: card.citations?.[0]?.section || "未标注章节",
            page: card.citations?.[0]?.page ? `p.${card.citations[0].page}` : "未标注页码",
          },
        })));
      } catch (error) {
        setNotice(error instanceof Error ? error.message : "复习卡生成失败，请重试。 ");
      } finally {
        setLoading(false);
      }
    }
    setView("cards");
  }

  function choosePaper(id: string) {
    setActivePaper(id);
    const next = response.results.find((item) => id === "all" || item.metadata.paper_id === id);
    setSelected(next || null);
  }

  async function importNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    setLoading(true);
    try {
      await fetch(`${API_BASE}/api/documents`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: values.title,
          authors: String(values.authors).split(/[;；]/).map((author) => author.trim()).filter(Boolean),
          year: Number(values.year),
          section: values.section,
          page: Number(values.page),
          body: values.body,
        }),
      }).then(responseJson);
      form.reset();
      setImportOpen(false);
      setNotice("笔记已写入 seekdb，可在下一次检索中命中。 ");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "收录失败。 ");
    } finally {
      setLoading(false);
    }
  }

  async function copyText(text: string) {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  function navigate(next: View) {
    setView(next);
    setMobileOpen(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  const citation = selected ? `${selected.metadata.authors} (${selected.metadata.year}). ${selected.metadata.title}. ${selected.metadata.section}, ${selected.metadata.pages}.` : "";
  const isLive = connection === "live";
  const cardItems: ReviewCard[] = cards || response.results.slice(0, 3).map((item, index) => ({
    id: item.id,
    question: index === 0 ? query : `关于「${item.metadata.section}」，文献笔记记录了什么？`,
    answer: item.text,
    evidence_id: item.id,
    citation: {
      title: item.metadata.title,
      section: item.metadata.section,
      page: item.metadata.pages || (item.metadata.page ? `p.${item.metadata.page}` : "未标注页码"),
    },
  }));

  return (
    <div className="app-shell">
      <header className="site-header">
        <button className="brand" onClick={() => navigate("search")} aria-label="页间首页">
          <span className="brand-mark"><span>页</span></span>
          <span className="brand-name">页间<span>LITSEEK</span></span>
        </button>
        <nav className={mobileOpen ? "main-nav is-open" : "main-nav"} aria-label="主导航">
          <button className={view === "search" ? "active" : ""} onClick={() => navigate("search")}>检索台</button>
          <button className={view === "cards" ? "active" : ""} onClick={() => openCards()}>复习卡</button>
          <button className={view === "about" ? "active" : ""} onClick={() => navigate("about")}>项目与验证</button>
        </nav>
        <div className="header-actions">
          <span className={`connection-badge ${isLive ? "live" : ""}`} title={isLive ? "已连接真实 seekdb 服务" : "当前展示内置样例，不是实时检索"}>
            {connection === "checking" ? <LoaderCircle className="spin" /> : isLive ? <Wifi /> : <WifiOff />}
            {connection === "checking" ? "正在连接" : isLive ? "seekdb 已连接" : "样例演示"}
          </span>
          <button className="icon-button mobile-menu" onClick={() => setMobileOpen(!mobileOpen)} aria-label="打开导航">{mobileOpen ? <X /> : <Menu />}</button>
        </div>
      </header>

      {view !== "about" && (
        <div className={`mode-banner ${isLive ? "is-live" : ""}`} role="status">
          <FlaskConical />
          <span>{isLive ? "真实检索模式：结果由本地 seekdb 混合检索返回，出处可逐条回溯。" : "当前为内置样例交互演示，非实时检索。按运行说明启动本地服务后，此页面会自动切换至真实 seekdb。"}</span>
          {!isLive && <button onClick={() => navigate("about")}>查看复现方法 <ArrowUpRight /></button>}
        </div>
      )}

      {view === "search" && (
        <main>
          <section className="workspace-intro">
            <div>
              <p className="eyebrow"><span>01</span> RESEARCH WORKSPACE</p>
              <h1>从文献堆里，<br /><em>找回</em>需要的证据。</h1>
            </div>
            <p className="intro-copy">用自然语言查找自己的论文笔记。语义与关键词一起检索，每个答案都回到具体章节与页码。</p>
          </section>

          <section className="search-panel" aria-label="文献检索">
            <form onSubmit={runSearch}>
              <div className="search-main">
                <Search aria-hidden="true" />
                <input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="研究问题" placeholder="输入一个研究问题，例如：RAG 如何更新知识？" />
                <button className="primary-button" type="submit" disabled={loading || !query.trim()}>
                  {loading ? <LoaderCircle className="spin" /> : <Search />}
                  {loading ? "检索中" : "检索证据"}
                  <span className="keycap">↵</span>
                </button>
              </div>
              <div className="search-options">
                <label htmlFor="keyword">关键词辅助 <span>可选</span></label>
                <input id="keyword" value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="如：自注意力" />
                <span className="option-divider" />
                <span className="retrieval-method"><Layers3 /> 语义向量 RRF + 全文约束 <b>HYBRID</b></span>
              </div>
            </form>
          </section>

          <div className="workspace-grid">
            <aside className="library-panel">
              <div className="panel-heading"><span>我的文献库</span><span className="count-badge">{livePapers.length}</span></div>
              <button className={activePaper === "all" ? "paper-filter all-papers active" : "paper-filter all-papers"} onClick={() => choosePaper("all")}>
                <span className="library-icon"><Layers3 /></span><span><b>全部文献</b><small>{response.results.length} 条本次证据</small></span><ChevronRight />
              </button>
              <div className="paper-list">
                {livePapers.map((paper) => (
                  <button key={paper.id} className={activePaper === paper.id ? "paper-filter active" : "paper-filter"} onClick={() => choosePaper(paper.id)}>
                    <span className="paper-color" style={{ backgroundColor: paper.color }} />
                    <span><b>{paper.title}</b><small>{paper.year} · {paper.chunks || 3} 个笔记块</small></span>
                  </button>
                ))}
              </div>
              <button className="add-note-button" onClick={() => isLive ? setImportOpen(true) : navigate("about")}><Plus /> 收录论文笔记</button>
              <p className="library-footnote"><Database /> {isLive ? "笔记持久化保存在 seekdb" : "本地运行后可收录自己的笔记"}</p>
            </aside>

            <section className="results-panel" aria-live="polite">
              <div className="results-header">
                <div><p className="section-kicker">SEARCH RESULTS</p><h2>相关证据 <span>{visibleResults.length}</span></h2></div>
                <span className="trace-info">{isLive ? `${response.duration_ms} ms · ` : ""}{response.trace_id}</span>
              </div>
              {visibleResults.length ? (
                <div className="evidence-list">
                  {visibleResults.map((item, index) => (
                    <button key={item.id} className={selected?.id === item.id ? "evidence-item selected" : "evidence-item"} onClick={() => setSelected(item)}>
                      <span className="evidence-index">{String(index + 1).padStart(2, "0")}</span>
                      <span className="evidence-body">
                        <span className="evidence-source">{item.metadata.title}<span>{item.metadata.year}</span></span>
                        <span className="evidence-text">{item.text}</span>
                        <span className="evidence-meta"><FileText /> {item.metadata.section} · {item.metadata.pages}<span className="result-id">{item.id}</span></span>
                      </span>
                      <ChevronRight className="evidence-arrow" />
                    </button>
                  ))}
                </div>
              ) : <div className="empty-state"><Search /><h3>没有找到匹配证据</h3><p>尝试减少关键词限制，或换一个研究问题。</p></div>}
              <div className="results-footer"><span><CheckCircle2 /> {isLive ? "结果来自 seekdb，未使用直线关键词替代语义检索" : "以上为内置样例结果"}</span><button onClick={() => openCards()}>生成复习卡 <ArrowUpRight /></button></div>
            </section>

            <aside className="citation-panel">
              <div className="panel-heading"><span>出处卡</span><Quote /></div>
              {selected && <>
                <div className="citation-paper-tab" style={{ backgroundColor: papers.find((p) => p.id === selected.metadata.paper_id)?.color || "#4d7468" }} />
                <p className="citation-label">当前证据</p>
                <h3>{selected.metadata.title}</h3>
                <p className="citation-authors">{selected.metadata.authors}<br />{selected.metadata.year}</p>
                <dl className="citation-details"><div><dt>章节</dt><dd>{selected.metadata.section}</dd></div><div><dt>页码</dt><dd>{selected.metadata.pages}</dd></div><div><dt>证据 ID</dt><dd>{selected.id}</dd></div></dl>
                <div className="citation-excerpt"><Quote /><p>{selected.text}</p></div>
                <button className="copy-button" onClick={() => copyText(citation)}>{copied ? <Check /> : <Copy />}{copied ? "已复制引用" : "复制标准引用"}</button>
              </>}
              {!selected && <p className="citation-authors">选择一条证据后，在这里核对出处。</p>}
            </aside>
          </div>
        </main>
      )}

      {view === "cards" && (
        <main className="cards-page">
          <button className="back-link" onClick={() => navigate("search")}><ArrowLeft /> 返回检索台</button>
          <section className="cards-intro"><p className="eyebrow"><span>02</span> ACTIVE RECALL</p><h1>把证据，变成<br /><em>记得住</em>的知识。</h1><p>每张卡只使用本次检索命中的笔记。先回忆，再翻开证据，不生成没有出处的结论。</p></section>
          <div className="card-context"><BookOpen /><span>本次问题</span><b>{query}</b><span>{response.results.length} 条可用证据</span></div>
          <section className="review-grid">
            {cardItems.map((card, index) => (
              <article className={cardOpen === index ? "review-card is-open" : "review-card"} key={card.id}>
                <div className="review-card-top"><span>CARD {String(index + 1).padStart(2, "0")}</span><span>{card.citation.section}</span></div>
                <div className="review-question"><small>先试着回答</small><h2>{card.question}</h2></div>
                {cardOpen === index ? <div className="review-answer"><p>{card.answer}</p><div><FileText /> {card.citation.title}<br /><span>{card.citation.section} · {card.citation.page} · {card.evidence_id}</span></div></div> : <div className="concealed-answer"><span>答案已遮住</span><span>回忆后再查看证据</span></div>}
                <button onClick={() => setCardOpen(cardOpen === index ? null : index)}>{cardOpen === index ? "收起证据" : "翻开证据"}<ArrowUpRight /></button>
              </article>
            ))}
          </section>
          {!response.results.length && <div className="empty-state"><BookOpen /><h3>还没有可生成卡片的证据</h3><button onClick={() => navigate("search")}>先去检索</button></div>}
          <div className="cards-note"><Quote /> 复习卡是原文笔记的学习化呈现，不代表论文之外的额外事实。</div>
        </main>
      )}

      {view === "about" && (
        <main className="about-page">
          <section className="about-hero">
            <p className="eyebrow"><span>03</span> PROJECT & VERIFICATION</p>
            <h1>让文献检索<br /><em>有迹可循。</em></h1>
            <p>页间是一款面向研究生与知识工作者的文献复盘助手。它解决“记得读过，却找不到出处”的真实痛点，把分散笔记变成可检索、可引用、可复习的个人证据库。</p>
            <div className="about-actions"><button className="primary-button" onClick={() => navigate("search")}>打开检索台 <ArrowUpRight /></button>{REPOSITORY_URL ? <a className="secondary-button" href={REPOSITORY_URL} target="_blank" rel="noreferrer"><Code2 /> 代码仓库</a> : <span className="secondary-button muted"><Code2 /> 仓库地址随提测资料填写</span>}</div>
          </section>
          <section className="architecture-section">
            <div className="section-title"><p className="section-kicker">HOW IT WORKS</p><h2>不是连接测试，<br />而是核心检索链路。</h2></div>
            <div className="architecture-flow">
              <article><span>01</span><FileText /><h3>收录人工笔记</h3><p>按论文、章节与页码整理知识块，保留稳定证据 ID 和完整出处。</p></article>
              <article><span>02</span><Database /><h3>seekdb 混合检索</h3><p>同一 Collection 内做向量语义检索与 RRF 排序，并在服务层强制全文关键词约束。</p></article>
              <article><span>03</span><BookOpen /><h3>回到证据复习</h3><p>检索结果携出处返回，复习卡仅使用命中笔记，拒绝无来源补充。</p></article>
            </div>
          </section>
          <section className="verification-section" id="verification">
            <div><p className="section-kicker">REPRODUCE IN 4 STEPS</p><h2>评审如何验证</h2><p>项目默认附带 3 篇经典论文、9 个人工转述笔记块。无需准备初始数据，即可复现完整流程。</p></div>
            <ol className="verification-steps">
              <li><span>1</span><div><b>启动真实服务</b><p>在 Ubuntu / WSL2 安装 Python 依赖，启动 FastAPI 与嵌入式 seekdb。</p></div></li>
              <li><span>2</span><div><b>验证语义检索</b><p>搜索“为什么可以并行训练”，应命中 Transformer 架构笔记并返回出处。</p></div></li>
              <li><span>3</span><div><b>验证关键词过滤</b><p>以“怎样并行建立序列中任意位置的联系？”为问题、关键词“自注意力”，检查 hybrid 模式与全文过滤。</p></div></li>
              <li><span>4</span><div><b>验证持久化与卡片</b><p>重启后数据仍在；复习卡的证据 ID 必须属于本次检索结果。</p></div></li>
            </ol>
          </section>
          <section className="delivery-section">
            <div className="delivery-card"><span>TECH STACK</span><h3>实际技术分工</h3><ul><li><b>Qoder</b>：需求拆解、前后端编码、调试、文档与网站发布</li><li><b>seekdb</b>：笔记持久化、向量索引、RRF 排序，服务层强制全文关键词约束</li><li><b>FastAPI + React</b>：可复现服务和研究工作台</li></ul></div>
            <div className="delivery-card dark"><span>DELIVERY</span><h3>提交材料</h3><ul><li>完整源码与 .env.example，不提交真实凭据</li><li>从安装到启动的运行说明与验证样例</li><li>Qoder 开发记录与真实 smoke 输出</li><li>Qoder 账号 UID：01a1756c-0bc2-718f-9918-8eb2c0d18a7f</li></ul></div>
          </section>
        </main>
      )}

      <footer className="site-footer"><div className="footer-brand">页间 <span>LITSEEK</span></div><p>把每一次阅读，留在下次能找到的地方。</p><span>Built with Qoder · Retrieval by seekdb</span></footer>

      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent className="import-dialog">
          <DialogHeader><DialogTitle>收录一条论文笔记</DialogTitle><DialogDescription>笔记和出处会作为同一个知识块写入 seekdb。</DialogDescription></DialogHeader>
          <form onSubmit={importNote} className="import-form">
            <label>论文标题<input name="title" required placeholder="论文完整标题" /></label>
            <div className="form-row"><label>作者<input name="authors" required placeholder="多位作者用分号分隔" /></label><label>年份<input name="year" required type="number" min="1900" max="2100" placeholder="2026" /></label></div>
            <div className="form-row"><label>章节<input name="section" required placeholder="如：模型结构" /></label><label>页码<input name="page" required type="number" min="1" placeholder="如：4" /></label></div>
            <label>笔记正文<textarea name="body" required minLength={20} rows={5} placeholder="粘贴自己整理的笔记，建议每条聚焦一个知识点。" /></label>
            <button className="primary-button" type="submit" disabled={loading}>{loading ? <LoaderCircle className="spin" /> : <Plus />}写入 seekdb</button>
          </form>
        </DialogContent>
      </Dialog>
      {notice && <div className="toast" role="status"><CheckCircle2 />{notice}</div>}
    </div>
  );
}
