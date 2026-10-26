"""Hand-written Chinese paraphrase notes for the initial literature library."""

from typing import Any

_PAPERS = {
    "attention": {
        "title": "Attention Is All You Need",
        "authors": "Ashish Vaswani; Noam Shazeer; Niki Parmar; Jakob Uszkoreit; Llion Jones; Aidan N. Gomez; Lukasz Kaiser; Illia Polosukhin",
        "year": 2017,
        "source_url": "https://arxiv.org/abs/1706.03762",
    },
    "bert": {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "authors": "Jacob Devlin; Ming-Wei Chang; Kenton Lee; Kristina Toutanova",
        "year": 2019,
        "source_url": "https://arxiv.org/abs/1810.04805",
    },
    "rag": {
        "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        "authors": "Patrick Lewis; Ethan Perez; Aleksandra Piktus; Fabio Petroni; Vladimir Karpukhin; Naman Goyal; Heinrich Küttler; Mike Lewis; Wen-tau Yih; Tim Rocktäschel; Sebastian Riedel; Douwe Kiela",
        "year": 2020,
        "source_url": "https://arxiv.org/abs/2005.11401",
    },
}

_BLOCKS = [
    (
        "attention",
        "摘要与顺序计算",
        1,
        "这篇论文提出 Transformer：不再用循环网络逐步读取 token，而是让自注意力直接建立序列内任意位置的联系。这样训练时各位置可以并行计算，长距离依赖也不必沿时间步逐站传递；论文用机器翻译实验说明这一路线的效果。",
    ),
    (
        "attention",
        "缩放点积注意力",
        3,
        "缩放点积注意力把查询 Q、键 K 和值 V 放在一起计算。先用 Q 与 K 的相似度得到权重，再对 V 加权求和；除以键维度平方根，是为了避免维度较大时点积过大、softmax 梯度过小。它既可以看当前位置之外的全局信息，也能用掩码阻止解码器偷看未来。",
    ),
    (
        "attention",
        "多头机制与位置编码",
        4,
        "多头注意力不是把单次计算简单重复，而是把表示投影到多个子空间并行关注不同关系，再拼接各头输出。由于模型没有循环结构，正弦位置编码被加入输入表示，让模型区分先后次序；因此 self-attention、multi-head 与 positional encoding 是互补设计。",
    ),
    (
        "bert",
        "双向上下文与预训练目标",
        3,
        "BERT 的核心是让同一层表示同时利用左右文，而不是把两个单向语言模型简单拼接。预训练时随机遮住部分 token，让模型依据两侧上下文恢复它们；原论文还加入下一句预测，使句子对关系进入预训练。masked language model 与 next sentence prediction 是原始 BERT 的两个训练任务。",
    ),
    (
        "bert",
        "输入表示",
        4,
        "每个输入位置由 token、句段和位置三类嵌入相加构成。[CLS] 放在开头，其最终表示常用于分类；[SEP] 分隔句子或标记边界。无论单句还是句子对，都被整理成统一序列后交给多层双向 Transformer 编码器。",
    ),
    (
        "bert",
        "面向下游任务微调",
        5,
        "BERT 把较重的语言知识学习放在预训练阶段，下游只需按任务增加简单输出层并整体微调。分类可以读取 [CLS] 表示，序列标注和问答则使用 token 级输出。同一个预训练编码器因此能迁移到自然语言推断、命名实体识别与抽取式问答，而不必为每项任务重造主体网络。",
    ),
    (
        "rag",
        "参数化知识与外部记忆",
        1,
        "RAG 把生成模型与可检索文本库连接起来：模型参数保存参数化知识，检索得到的文档提供非参数化知识。面对知识密集任务，系统先依据问题找出相关段落，再把段落交给生成器，使回答能够利用外部证据，也便于通过替换索引更新知识。",
    ),
    (
        "rag",
        "两类检索生成方式",
        3,
        "RAG-Sequence 为整段输出共享同一份检索文档分布，适合答案整体依赖同一来源的情形；RAG-Token 则允许生成不同 token 时吸收不同文档的贡献。两者都把检索结果作为潜在变量进行边缘化，而不是把单一搜索结果当作永远正确的答案。",
    ),
    (
        "rag",
        "训练、推理与证据",
        4,
        "训练时，检索器和序列生成器围绕目标文本共同优化；推理时，问题向量用于在预建索引中寻找候选段落。生成质量仍受检索覆盖、文档质量和解码方式影响，所以 RAG 并不等于自动消除幻觉。对学习者而言，保留命中的文档 ID 与原文片段，才能从答案回溯到证据。",
    ),
]

SEED_NOTES: list[dict[str, Any]] = []
for _index, (_paper_id, _section, _page, _text) in enumerate(_BLOCKS, start=1):
    _paper = _PAPERS[_paper_id]
    _chunk = ((_index - 1) % 3) + 1
    SEED_NOTES.append(
        {
            "id": f"{_paper_id}-note-{_chunk:02d}",
            "text": _text,
            "metadata": {
                "paper_id": _paper_id,
                "title": _paper["title"],
                "authors": _paper["authors"],
                "year": _paper["year"],
                "section": _section,
                "page": _page,
                "chunk_index": _chunk,
                "source": "builtin_zh_paraphrase",
                "source_url": _paper["source_url"],
            },
        }
    )
