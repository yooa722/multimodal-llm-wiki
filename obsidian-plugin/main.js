const { ItemView, Notice, Plugin, requestUrl, setIcon } = require("obsidian");

const VIEW_TYPE = "multimodal-wiki-query-view";

class QueryView extends ItemView {
  getViewType() { return VIEW_TYPE; }
  getDisplayText() { return "多模态 Wiki 查询"; }
  getIcon() { return "messages-square"; }

  async onOpen() {
    const root = this.containerEl.children[1];
    root.empty();
    root.addClass("mmwiki-query");

    const hero = root.createDiv({ cls: "mmwiki-hero" });
    const heroTop = hero.createDiv({ cls: "mmwiki-hero-top" });
    const icon = heroTop.createDiv({ cls: "mmwiki-logo" });
    setIcon(icon, "scan-search");
    const heading = heroTop.createDiv({ cls: "mmwiki-heading" });
    heading.createEl("small", { text: "MULTIMODAL LLM WIKI" });
    heading.createEl("h2", { text: "让 Wiki 读懂图表" });
    hero.createEl("p", {
      text: "从知识页定位主题，回到文字、表格和原始图片，再由视觉模型给出可追溯答案。"
    });
    const connection = hero.createDiv({ cls: "mmwiki-connection is-checking" });
    connection.createSpan({ cls: "mmwiki-status-dot" });
    const connectionText = connection.createSpan({ text: "正在连接问答服务" });

    const composer = root.createDiv({ cls: "mmwiki-composer" });
    const sourceGroup = composer.createDiv({ cls: "mmwiki-field" });
    const sourceLabel = sourceGroup.createEl("label", { text: "知识来源" });
    const sourceHint = sourceLabel.createSpan({ text: "默认全库；限定来源仅用于已知文档内查询" });
    sourceHint.addClass("mmwiki-field-hint");
    const selectWrap = sourceGroup.createDiv({ cls: "mmwiki-select-wrap" });
    const selectIcon = selectWrap.createSpan({ cls: "mmwiki-field-icon" });
    setIcon(selectIcon, "library-big");
    const source = selectWrap.createEl("select");
    source.createEl("option", { text: "全部已入库来源" }).value = "";

    const retrievalGroup = composer.createDiv({ cls: "mmwiki-field" });
    const retrievalLabel = retrievalGroup.createEl("label", { text: "检索链路" });
    const retrievalHint = retrievalLabel.createSpan({ text: "可现场对比三档方案" });
    retrievalHint.addClass("mmwiki-field-hint");
    const retrievalWrap = retrievalGroup.createDiv({ cls: "mmwiki-select-wrap" });
    const retrievalIcon = retrievalWrap.createSpan({ cls: "mmwiki-field-icon" });
    setIcon(retrievalIcon, "route");
    const retrievalMode = retrievalWrap.createEl("select");
    const retrievalLabels = {
      lexical: "Wiki 导航 + BM25 基线",
      hybrid: "Wiki 导航 + 文本混合检索",
      multimodal: "Wiki 导航 + 多模态检索"
    };
    for (const [value, label] of Object.entries(retrievalLabels)) {
      const option = retrievalMode.createEl("option", { text: label });
      option.value = value;
    }
    retrievalMode.value = "hybrid";

    const questionGroup = composer.createDiv({ cls: "mmwiki-field" });
    const questionLabel = questionGroup.createEl("label", { text: "你的问题" });
    const counter = questionLabel.createSpan({ text: "0 字" });
    counter.addClass("mmwiki-field-hint");
    const question = questionGroup.createEl("textarea", {
      attr: { placeholder: "可询问正文事实、表格数值、图片结构或跨模态关系…" }
    });
    question.addEventListener("input", () => counter.setText(`${question.value.trim().length} 字`));

    const examples = composer.createDiv({ cls: "mmwiki-examples" });
    examples.createEl("span", { text: "试试这些" });
    const prompts = [
      {
        label: "读 Figure 4",
        question: "根据 Figure 4，ReToken 推理时的数据流是什么？请按顺序说明，并指出图中缓存的对象。"
      },
      {
        label: "读取 Form 7004",
        question: "Form 7004 可以为哪些申报表申请延期？请根据表格列举前五项。"
      },
      {
        label: "核对工期预算",
        question: "工期与预算表中，开发测试阶段需要多少天、多少人、多少预算？"
      }
    ];
    for (const prompt of prompts) {
      const chip = examples.createEl("button", { text: prompt.label, cls: "mmwiki-chip" });
      chip.onclick = () => {
        question.value = prompt.question;
        source.value = "";
        counter.setText(`${prompt.question.length} 字`);
        question.focus();
      };
    }

    const button = composer.createEl("button", { cls: "mmwiki-submit" });
    const buttonIcon = button.createSpan({ cls: "mmwiki-submit-icon" });
    setIcon(buttonIcon, "sparkles");
    const buttonText = button.createSpan({ text: "调用视觉模型回答" });
    button.disabled = true;
    const status = root.createEl("div", { cls: "mmwiki-run-status" });
    const answer = root.createEl("div", { cls: "mmwiki-answer" });

    try {
      const health = await requestUrl({ url: "http://127.0.0.1:19828/api/v1/health" });
      const value = health.json;
      if (!value.configured) throw new Error("服务已启动，但 .env 尚未配置");
      const retrieval = value.retrieval || {};
      for (const option of Array.from(retrievalMode.options)) {
        if (option.value === "hybrid" && !retrieval.text_ready) option.disabled = true;
        if (option.value === "multimodal" && !retrieval.visual_ready) option.disabled = true;
      }
      if (!retrieval.text_ready) retrievalMode.value = "lexical";
      const sources = await requestUrl({ url: "http://127.0.0.1:19828/api/v1/sources" });
      for (const item of sources.json.sources || []) {
        const visualCount = item.visual_evidence_count || 0;
        const option = source.createEl("option", {
          text: `${item.title} · ${item.modalities.join("/")} · 多模态证据 ${visualCount}`
        });
        option.value = item.source_id;
      }
      connection.removeClass("is-checking");
      connection.addClass("is-online");
      const retrievalState = retrieval.visual_ready
        ? "三路检索就绪"
        : retrieval.text_ready
          ? "混合检索就绪"
          : "关键词基线";
      connectionText.setText(`在线 · ${value.model} · ${retrievalState}`);
      const coverage = retrieval.wiki_coverage || {};
      if ((coverage.stable_page_source_coverage || 0) < (coverage.stable_page_source_total || 0)) {
        status.setText(
          `来源页已全量可检索；稳定知识页当前覆盖 ${coverage.stable_page_source_coverage || 0}/${coverage.stable_page_source_total || 0} 个来源。`
        );
      }
      button.disabled = false;
    } catch (error) {
      connection.removeClass("is-checking");
      connection.addClass("is-offline");
      connectionText.setText("问答服务未连接");
      status.setText("请在项目目录运行 python3 app.py api，然后重新打开此面板。" );
    }

    button.onclick = async () => {
      const text = question.value.trim();
      if (!text) return new Notice("请先输入问题");
      button.disabled = true;
      button.addClass("is-loading");
      buttonText.setText("正在读取多模态证据…");
      status.setText(`正在执行${retrievalLabels[retrievalMode.value]}，随后回溯原始 Evidence`);
      answer.empty();
      try {
        const response = await requestUrl({
          url: "http://127.0.0.1:19828/api/v1/query",
          method: "POST",
          contentType: "application/json",
          body: JSON.stringify({
            question: text,
            top_k: 5,
            source_ids: source.value ? [source.value] : [],
            retrieval_mode: retrievalMode.value
          })
        });
        const value = response.json;
        if (response.status >= 400) throw new Error(value.message || value.error || "查询失败");
        status.empty();
        const metrics = status.createDiv({ cls: "mmwiki-metrics" });
        const navigationSources = value.retrieval?.wiki_navigation_sources || [];
        const navigationPages = value.retrieval?.wiki_navigation || [];
        const matchedVisuals = value.retrieval?.matched_visual_assets || [];
        const metricValues = [
          ["模型", value.model],
          ["检索", retrievalLabels[value.retrieval?.mode] || value.retrieval?.mode || "关键词基线"],
          ["证据模态", (value.modalities || []).join(" + ") || "text"],
          ["命中原图", matchedVisuals.length ? `${matchedVisuals.length} 张` : "—"],
          ["Wiki 导航", navigationSources.length ? `${navigationSources.length} 个来源` : `${navigationPages.length} 页`],
          ["响应耗时", `${(value.latency_ms / 1000).toFixed(2)} s`]
        ];
        for (const [label, metric] of metricValues) {
          const card = metrics.createDiv({ cls: "mmwiki-metric" });
          card.createEl("small", { text: label });
          card.createEl("strong", { text: metric });
        }

        if (value.retrieval?.fallback_reason) {
          answer.createEl("p", {
            text: `检索回退：${value.retrieval.fallback_reason}`,
            cls: "mmwiki-snippet"
          });
        }

        if (navigationSources.length || navigationPages.length) {
          const navHeader = answer.createDiv({ cls: "mmwiki-section-title" });
          const navIcon = navHeader.createSpan();
          setIcon(navIcon, "route");
          navHeader.createEl("h3", { text: "Wiki 导航" });
          const navigation = navigationSources.length ? navigationSources : navigationPages;
          const navText = navigation
            .slice(0, 5)
            .map(item => navigationSources.length ? `${item.title}（来源 ${item.rank}）` : `${item.title}（${item.kind}）`)
            .join(" → ");
          answer.createEl("p", { text: navText, cls: "mmwiki-snippet" });
        }

        const answerCard = answer.createDiv({ cls: "mmwiki-answer-card" });
        const answerTitle = answerCard.createDiv({ cls: "mmwiki-section-title" });
        const answerIcon = answerTitle.createSpan();
        setIcon(answerIcon, "message-square-text");
        answerTitle.createEl("h3", { text: "回答" });
        answerCard.createEl("p", { text: value.answer, cls: "mmwiki-answer-text" });

        const evidenceHeader = answer.createDiv({ cls: "mmwiki-section-title mmwiki-evidence-title" });
        const evidenceIcon = evidenceHeader.createSpan();
        setIcon(evidenceIcon, "quote");
        evidenceHeader.createEl("h3", { text: `证据 · ${(value.citations || []).length}` });
        for (const item of value.citations || []) {
          const row = answer.createEl("div", { cls: "mmwiki-citation" });
          const rowTop = row.createDiv({ cls: "mmwiki-citation-top" });
          rowTop.createEl("strong", { text: item.title });
          const badge = rowTop.createSpan({
            text: (item.modalities || []).join(" + ") || "text",
            cls: "mmwiki-badge"
          });
          badge.dataset.modality = (item.modalities || ["text"])[0];
          if (item.matched_asset_path) {
            const assetFile = this.app.vault.getAbstractFileByPath(item.matched_asset_path);
            if (assetFile && assetFile.extension) {
              const preview = row.createEl("img", {
                cls: "mmwiki-citation-image",
                attr: {
                  src: this.app.vault.getResourcePath(assetFile),
                  alt: `命中的视觉证据 ${item.matched_asset_id || ""}`
                }
              });
              preview.onclick = () => this.app.workspace.getLeaf(false).openFile(assetFile);
            }
          }
          row.createEl("p", { text: item.snippet, cls: "mmwiki-snippet" });
          const footer = row.createDiv({ cls: "mmwiki-citation-footer" });
          const channelText = (item.retrieval_channels || []).join(" + ");
          footer.createEl("small", {
            text: `${item.source_id} · 第 ${(item.pages || []).join(", ")} 页 · score ${item.score}${channelText ? ` · ${channelText}` : ""}`
          });
          const link = footer.createEl("button", { cls: "mmwiki-open-source" });
          const linkIcon = link.createSpan();
          setIcon(linkIcon, "external-link");
          link.createSpan({ text: "打开证据" });
          link.onclick = () => {
            const evidence = (item.evidence_ids || [])[0] || "";
            const itemId = evidence.includes("#") ? evidence.split("#").pop() : "";
            const target = item.path.replace(/\.md$/, "") + (itemId ? `#${itemId}` : "");
            this.app.workspace.openLinkText(target, "", false);
          };
        }
      } catch (error) {
        status.setText(`在线问答失败：${error.message}`);
      } finally {
        button.disabled = false;
        button.removeClass("is-loading");
        buttonText.setText("调用视觉模型回答");
      }
    };
  }
}

module.exports = class MultimodalWikiQueryPlugin extends Plugin {
  async onload() {
    this.registerView(VIEW_TYPE, leaf => new QueryView(leaf));
    this.addRibbonIcon("scan-search", "多模态 Wiki 在线问答", () => this.activate());
    this.addCommand({ id: "open-query", name: "打开多模态 Wiki 在线问答", callback: () => this.activate() });
  }
  async activate() {
    let leaf = this.app.workspace.getLeavesOfType(VIEW_TYPE)[0];
    if (!leaf) {
      leaf = this.app.workspace.getRightLeaf(false);
      await leaf.setViewState({ type: VIEW_TYPE, active: true });
    }
    this.app.workspace.revealLeaf(leaf);
  }
};
