<script setup lang="ts">
// Markdown, drawn with `h()` from the token tree — no `innerHTML` at any point.

import { computed, h, type VNode } from "vue";
import { RouterLink } from "vue-router";
import { parseMarkdown, type Block, type Inline } from "@/lib/markdown";

// `/view/{id}` is how the appliance writes a link to another node inside `rendered_content` —
// the footnotes and inline citations it renders from `link_handling` all point that way. Those
// are this console's own pages, so they route rather than reload.
const INTERNAL = /^\/view\/(\d+)$/;

const props = defineProps<{ content: string }>();
const blocks = computed(() => parseMarkdown(props.content));

function inline(nodes: Inline[]): (VNode | string)[] {
  return nodes.map((node) => {
    switch (node.t) {
      case "text":
        return node.v;
      case "code":
        return h("code", node.v);
      case "strong":
        return h("strong", inline(node.c));
      case "em":
        return h("em", inline(node.c));
      case "link": {
        const internal = INTERNAL.exec(node.href);
        if (internal) {
          return h(RouterLink, { to: `/node/${internal[1]}` }, () => inline(node.c));
        }
        // Everything else points outside the appliance. `noopener` because a new tab with a
        // handle on this one is a way out of it.
        return h(
          "a",
          { href: node.href, target: "_blank", rel: "noopener noreferrer" },
          inline(node.c)
        );
      }
    }
  });
}

function block(node: Block): VNode {
  switch (node.t) {
    case "heading":
      return h(`h${Math.min(node.level + 1, 6)}`, inline(node.c));
    case "para":
      return h("p", inline(node.c));
    case "code":
      return h("pre", { class: "block-code" }, [
        h("code", { class: node.lang ? `language-${node.lang}` : undefined }, node.v),
      ]);
    case "quote":
      return h("blockquote", node.c.map(block));
    case "list":
      return h(
        node.ordered ? "ol" : "ul",
        { start: node.ordered && node.start !== 1 ? node.start : undefined },
        node.items.map((item) => h("li", item.map(block)))
      );
    case "table":
      return h("div", { class: "table-scroll" }, [
        h("table", [
          h("thead", [h("tr", node.head.map((cell) => h("th", inline(cell))))]),
          h(
            "tbody",
            node.rows.map((row) => h("tr", row.map((cell) => h("td", inline(cell)))))
          ),
        ]),
      ]);
    case "rule":
      return h("hr");
  }
}

const Rendered = () => h("div", { class: "markdown" }, blocks.value.map(block));
</script>

<template>
  <Rendered />
</template>

<style>
.markdown > :first-child {
  margin-top: 0;
}

.markdown h2,
.markdown h3,
.markdown h4,
.markdown h5,
.markdown h6 {
  margin: 1.5em 0 0.4em;
  line-height: 1.25;
}

.markdown h2 {
  font-size: var(--t-mid);
}
.markdown h3 {
  font-size: var(--t-base);
}
.markdown h4,
.markdown h5,
.markdown h6 {
  font-size: var(--t-base);
  font-weight: 600;
  color: var(--ink-soft);
}

.markdown p {
  margin: 0 0 0.85em;
}

.markdown code {
  background: var(--sunk);
  padding: 0.08em 0.3em;
  border-radius: 2px;
}

.markdown pre.block-code {
  margin: 0 0 1em;
  padding: var(--s3) var(--s4);
  background: var(--sunk);
  border-left: 2px solid var(--rule);
  overflow-x: auto;
  font-size: var(--t-small);
  line-height: 1.5;
}

.markdown pre.block-code code {
  background: none;
  padding: 0;
}

.markdown blockquote {
  margin: 0 0 1em;
  padding-left: var(--s4);
  border-left: 2px solid var(--rule);
  color: var(--ink-soft);
}

.markdown ul,
.markdown ol {
  margin: 0 0 0.9em;
  padding-left: 1.4em;
}

.markdown li {
  margin-bottom: 0.2em;
}

.markdown li > p:last-child {
  margin-bottom: 0;
}

.markdown .table-scroll {
  overflow-x: auto;
  margin-bottom: 1em;
}

.markdown table {
  border-collapse: collapse;
  font-size: var(--t-small);
}

.markdown th,
.markdown td {
  padding: var(--s1) var(--s3) var(--s1) 0;
  border-bottom: 1px solid var(--rule-soft);
  text-align: left;
  vertical-align: top;
}

.markdown th {
  color: var(--ink-soft);
  font-weight: 600;
  border-bottom-color: var(--rule);
}
</style>
