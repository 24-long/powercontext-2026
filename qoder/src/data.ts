export type Paper = {
  id: string;
  title: string;
  authors: string;
  year: number;
  section: string;
  pages: string;
  color: string;
  chunks: number;
};

export type Evidence = {
  id: string;
  text: string;
  distance?: number;
  metadata: {
    paper_id: string;
    title: string;
    authors: string;
    year: number;
    section: string;
    pages?: string;
    page?: number;
  };
};

export type SearchResponse = {
  trace_id: string;
  engine: string;
  mode: string;
  duration_ms: number;
  results: Evidence[];
};

export const papers: Paper[] = [
  {
    id: "attention",
    title: "Attention Is All You Need",
    authors: "Ashish Vaswani et al.",
    year: 2017,
    section: "Model Architecture",
    pages: "§3 · 2–5",
    color: "#d76b45",
    chunks: 3,
  },
  {
    id: "bert",
    title: "BERT: Pre-training of Deep Bidirectional Transformers",
    authors: "Jacob Devlin et al.",
    year: 2018,
    section: "Pre-training & Fine-tuning",
    pages: "§3–4 · 3–7",
    color: "#4d7468",
    chunks: 3,
  },
  {
    id: "rag",
    title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
    authors: "Patrick Lewis et al.",
    year: 2020,
    section: "Retrieval-Augmented Generation",
    pages: "§2–3 · 2–5",
    color: "#8b7752",
    chunks: 3,
  },
];

const evidence: Evidence[] = [
  {
    id: "attention-architecture",
    text: "Transformer 完全依靠注意力机制建立输入与输出之间的全局依赖，移除了循环结构。序列中的位置可以同时计算，因此训练并行度显著提升。",
    distance: 0.0164,
    metadata: { paper_id: "attention", title: papers[0].title, authors: papers[0].authors, year: 2017, section: "模型结构", pages: "§3.1 · p.2" },
  },
  {
    id: "attention-self",
    text: "自注意力把序列中不同位置联系起来，以计算当前位置的表示。缩放点积注意力用 Key 的维度平方根缩放点积，避免 softmax 进入梯度过小的区域。",
    distance: 0.0171,
    metadata: { paper_id: "attention", title: papers[0].title, authors: papers[0].authors, year: 2017, section: "注意力机制", pages: "§3.2 · p.3" },
  },
  {
    id: "attention-multihead",
    text: "多头注意力在不同表示子空间并行执行注意力，再拼接各头结果。它让模型能够同时关注来自不同位置、不同子空间的信息。",
    distance: 0.0182,
    metadata: { paper_id: "attention", title: papers[0].title, authors: papers[0].authors, year: 2017, section: "多头注意力", pages: "§3.2.2 · p.4" },
  },
  {
    id: "bert-mlm",
    text: "BERT 使用掩码语言模型进行预训练：随机遮盖部分输入词元，再根据左右两侧上下文预测被遮盖的词元，从而学习双向表示。",
    distance: 0.019,
    metadata: { paper_id: "bert", title: papers[1].title, authors: papers[1].authors, year: 2018, section: "掩码语言模型", pages: "§3.1 · p.4" },
  },
  {
    id: "bert-finetune",
    text: "预训练后的 BERT 可以通过增加任务输出层并微调全部参数适配下游任务，使同一套预训练表示服务于分类、问答等任务。",
    distance: 0.020,
    metadata: { paper_id: "bert", title: papers[1].title, authors: papers[1].authors, year: 2018, section: "下游微调", pages: "§3.4 · p.5" },
  },
  {
    id: "rag-retrieval",
    text: "RAG 将预训练生成模型与非参数记忆结合。输入先用于检索相关文档，再把文档作为额外上下文交给生成模型，使知识可以更新并追溯来源。",
    distance: 0.021,
    metadata: { paper_id: "rag", title: papers[2].title, authors: papers[2].authors, year: 2020, section: "检索增强生成", pages: "§2 · p.2" },
  },
];

export function demoSearch(query: string, keyword: string): SearchResponse {
  const normalized = `${query} ${keyword}`.toLowerCase();
  let selected = evidence.slice(0, 3);
  if (/bert|掩码|双向|微调/.test(normalized)) selected = [evidence[3], evidence[4], evidence[1]];
  if (/rag|检索增强|知识密集|非参数/.test(normalized)) selected = [evidence[5], evidence[0], evidence[3]];
  if (keyword.trim()) {
    selected = selected.filter((item) => item.text.includes(keyword.trim()));
  }
  return { trace_id: "DEMO-DATA", engine: "内置样例", mode: "demo", duration_ms: 0, results: selected };
}

export const initialResponse = demoSearch("Transformer 为什么能并行训练？", "");
