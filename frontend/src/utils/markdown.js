import { marked } from "marked";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import katex from "katex";

hljs.registerLanguage("bash", bash);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("plaintext", plaintext);
hljs.registerLanguage("python", python);
hljs.registerLanguage("py", python);

marked.setOptions({
  breaks: true,
  gfm: true,
  highlight(code, language) {
    const lang = hljs.getLanguage(language) ? language : "plaintext";
    return hljs.highlight(code, { language: lang }).value;
  },
});

export function renderMarkdown(input, options = {}) {
  if (!input) return "";
  const mathRendered = renderMath(input);
  const html = marked.parse(mathRendered);
  if (!options.enableRun) return html;
  return html.replace(
    /<pre><code class="language-(python|py)">([\s\S]*?)<\/code><\/pre>/g,
    (_, language, code) =>
      `<div class="runnable-code"><div class="code-actions"><span>${language}</span><button type="button" data-run-code>运行</button></div><pre><code class="language-${language}">${code}</code></pre><pre class="inline-code-output" hidden></pre></div>`,
  );
}

function renderMath(text) {
  return text
    .replace(/\$\$([\s\S]*?)\$\$/g, (_, formula) => renderFormula(formula, true))
    .replace(/\\\[([\s\S]*?)\\\]/g, (_, formula) => renderFormula(formula, true))
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, formula) => renderFormula(formula, false))
    .replace(/(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)/g, (_, formula) => {
      if (!/[\\^_{}=+\-*/]/.test(formula)) return `$${formula}$`;
      return renderFormula(formula, false);
    });
}

function renderFormula(formula, displayMode) {
  try {
    return katex.renderToString(formula.trim(), { displayMode, throwOnError: false });
  } catch {
    return escapeHtml(formula);
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
