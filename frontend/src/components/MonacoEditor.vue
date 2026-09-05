<template>
  <div ref="container" class="monaco-host" />
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor/editor/editor.api.js";
import "monaco-editor/language/json/monaco.contribution.js";
import { javascriptDefaults, typescriptDefaults } from "monaco-editor/language/typescript/monaco.contribution.js";
import "monaco-editor/language/css/monaco.contribution.js";
import "monaco-editor/language/html/monaco.contribution.js";
import "monaco-editor/languages/definitions/python/register.js";
import "monaco-editor/languages/definitions/markdown/register.js";
import "monaco-editor/languages/definitions/yaml/register.js";
import "monaco-editor/languages/definitions/shell/register.js";
import "monaco-editor/languages/definitions/powershell/register.js";
import "monaco-editor/languages/definitions/sql/register.js";
import "monaco-editor/languages/definitions/cpp/register.js";
import "monaco-editor/languages/definitions/csharp/register.js";
import "monaco-editor/languages/definitions/java/register.js";
import "monaco-editor/languages/definitions/go/register.js";
import "monaco-editor/languages/definitions/rust/register.js";
import "monaco-editor/languages/definitions/php/register.js";
import "monaco-editor/languages/definitions/ruby/register.js";
import "monaco-editor/languages/definitions/dockerfile/register.js";
import "monaco-editor/languages/definitions/bat/register.js";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";
import TsWorker from "monaco-editor/language/typescript/ts.worker?worker";
import CssWorker from "monaco-editor/language/css/css.worker?worker";
import HtmlWorker from "monaco-editor/language/html/html.worker?worker";
import { apiPost } from "../api/client";

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    if (label === "json") return new JsonWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    return new EditorWorker();
  },
};

const props = defineProps({
  modelValue: { type: String, default: "" },
  path: { type: String, default: "untitled.txt" },
  conversationId: { type: [Number, String], required: true },
});
const emit = defineEmits(["update:modelValue", "save", "navigate", "diagnostics"]);
const container = ref(null);
const models = new Map();
const viewStates = new Map();
const disposables = [];
let editor;
let applyingExternalValue = false;
let switchingModel = false;
let diagnosticsTimer;
let diagnosticsVersion = 0;

onMounted(() => {
  configureLanguageDefaults();
  const model = ensureModel(props.path, props.modelValue);
  editor = monaco.editor.create(container.value, {
    model,
    theme: "vs-dark",
    automaticLayout: true,
    minimap: { enabled: true },
    fontSize: 13,
    lineHeight: 21,
    wordWrap: "off",
    scrollBeyondLastLine: false,
    tabSize: 2,
    insertSpaces: true,
    formatOnPaste: true,
    formatOnType: true,
    quickSuggestions: { other: true, comments: false, strings: false },
    suggestOnTriggerCharacters: true,
    parameterHints: { enabled: true },
    glyphMargin: true,
  });
  disposables.push(editor.onDidChangeModelContent(() => {
    if (!applyingExternalValue) emit("update:modelValue", editor.getValue());
    scheduleDiagnostics();
  }));
  disposables.push(editor.onDidChangeModel(() => {
    scheduleDiagnostics();
    if (switchingModel) return;
    const modelPath = pathFromUri(editor.getModel()?.uri);
    if (modelPath && modelPath !== props.path) {
      emit("navigate", { path: modelPath, content: editor.getValue() });
    }
  }));
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => emit("save"));
  registerPythonProviders();
  scheduleDiagnostics();
});

watch(() => props.modelValue, (value) => {
  if (!editor || editor.getValue() === value) return;
  applyingExternalValue = true;
  editor.setValue(value);
  applyingExternalValue = false;
});

watch(() => props.path, (path, previous) => {
  if (!editor || !path) return;
  if (previous && editor.getModel()) viewStates.set(previous, editor.saveViewState());
  const model = ensureModel(path, props.modelValue);
  if (editor.getModel() === model) return;
  switchingModel = true;
  editor.setModel(model);
  const state = viewStates.get(path);
  if (state) editor.restoreViewState(state);
  editor.focus();
  switchingModel = false;
  scheduleDiagnostics();
});

onBeforeUnmount(() => {
  clearTimeout(diagnosticsTimer);
  disposables.forEach((item) => item.dispose());
  editor?.dispose();
  models.forEach((model) => {
    monaco.editor.setModelMarkers(model, "workspace-python", []);
    model.dispose();
  });
});

function registerPythonProviders() {
  disposables.push(monaco.languages.registerCompletionItemProvider("python", {
    triggerCharacters: ["."],
    async provideCompletionItems(model, position) {
      if (!isWorkspaceModel(model)) return { suggestions: [] };
      try {
        const data = await languageRequest("completions", model, position);
        return {
          suggestions: (data.items || []).map((item) => ({
            label: item.label,
            kind: completionKind(item.kind),
            insertText: item.insert_text || item.label,
            detail: item.detail,
            documentation: { value: item.documentation || "" },
            range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column),
          })),
        };
      } catch {
        return { suggestions: [] };
      }
    },
  }));

  disposables.push(monaco.languages.registerDocumentFormattingEditProvider("python", {
    async provideDocumentFormattingEdits(model) {
      if (!isWorkspaceModel(model)) return [];
      const data = await languageRequest("format", model);
      if (typeof data.content !== "string" || data.content === model.getValue()) return [];
      return [{ range: model.getFullModelRange(), text: data.content }];
    },
  }));

  disposables.push(monaco.languages.registerDefinitionProvider("python", {
    async provideDefinition(model, position) {
      if (!isWorkspaceModel(model)) return [];
      try {
        const data = await languageRequest("definition", model, position);
        return (data.items || []).map((item) => {
          const target = ensureModel(item.path, item.content || "");
          return {
            uri: target.uri,
            range: new monaco.Range(item.line, item.column, item.line, item.column + 1),
          };
        });
      } catch {
        return [];
      }
    },
  }));

  disposables.push(monaco.languages.registerHoverProvider("python", {
    async provideHover(model, position) {
      if (!isWorkspaceModel(model)) return null;
      try {
        const data = await languageRequest("hover", model, position);
        if (!data.item?.contents) return null;
        return { contents: [{ value: data.item.contents }], range: fromLspRange(data.item.range) };
      } catch { return null; }
    },
  }));

  disposables.push(monaco.languages.registerReferenceProvider("python", {
    async provideReferences(model, position) {
      if (!isWorkspaceModel(model)) return [];
      try {
        const data = await languageRequest("references", model, position);
        return (data.items || []).map((item) => {
          const target = ensureModel(item.path, item.content || "");
          return { uri: target.uri, range: new monaco.Range(item.line, item.column, item.line, item.column + 1) };
        });
      } catch { return []; }
    },
  }));

  disposables.push(monaco.languages.registerRenameProvider("python", {
    async provideRenameEdits(model, position, newName) {
      if (!isWorkspaceModel(model)) return { edits: [] };
      try {
        const payload = {
          path: pathFromUri(model.uri), content: model.getValue(),
          line: position.lineNumber, column: position.column, new_name: newName,
        };
        const data = await apiPost(`/api/conversations/${props.conversationId}/workspace/language/rename`, payload);
        const edits = [];
        for (const file of data.items || []) {
          const target = ensureModel(file.path, file.content || "");
          for (const edit of file.edits || []) {
            edits.push({ resource: target.uri, textEdit: { range: fromLspRange(edit.range), text: edit.newText || "" }, versionId: target.getVersionId() });
          }
        }
        return { edits };
      } catch (error) {
        return { edits: [], rejectReason: error.message || "Rename failed" };
      }
    },
    resolveRenameLocation(model, position) {
      return { range: model.getWordAtPosition(position)
        ? new monaco.Range(position.lineNumber, model.getWordAtPosition(position).startColumn, position.lineNumber, model.getWordAtPosition(position).endColumn)
        : new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column), text: model.getWordAtPosition(position)?.word || "" };
    },
  }));
}

function fromLspRange(range) {
  if (!range) return undefined;
  return new monaco.Range(
    (range.start?.line || 0) + 1, (range.start?.character || 0) + 1,
    (range.end?.line || 0) + 1, (range.end?.character || 0) + 1,
  );
}

function scheduleDiagnostics() {
  clearTimeout(diagnosticsTimer);
  const model = editor?.getModel();
  if (!model || model.getLanguageId() !== "python" || !isWorkspaceModel(model)) return;
  const version = ++diagnosticsVersion;
  diagnosticsTimer = setTimeout(async () => {
    try {
      const data = await languageRequest("diagnostics", model);
      if (version !== diagnosticsVersion || model.isDisposed()) return;
      const markers = (data.items || []).map((item) => ({
        message: item.message,
        code: item.code,
        severity: item.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
        startLineNumber: item.start_line,
        startColumn: item.start_column,
        endLineNumber: item.end_line,
        endColumn: Math.max(item.start_column + 1, item.end_column),
        source: "Ruff",
      }));
      monaco.editor.setModelMarkers(model, "workspace-python", markers);
      emit("diagnostics", { path: pathFromUri(model.uri), count: markers.length });
    } catch {
      // Diagnostics should never block editing while the backend reconnects.
    }
  }, 350);
}

async function languageRequest(action, model, position) {
  const payload = { path: pathFromUri(model.uri), content: model.getValue() };
  if (position) {
    payload.line = position.lineNumber;
    payload.column = position.column;
  }
  return apiPost(`/api/conversations/${props.conversationId}/workspace/language/${action}`, payload);
}

function ensureModel(path, content) {
  const key = String(path || "untitled.txt");
  let model = models.get(key);
  if (!model || model.isDisposed()) {
    model = monaco.editor.createModel(content || "", languageForPath(key), workspaceUri(key));
    models.set(key, model);
  } else if (model.getValue() !== content && key === props.path && !editor) {
    model.setValue(content || "");
  }
  return model;
}

function workspaceUri(path) {
  return monaco.Uri.from({ scheme: "workspace", authority: String(props.conversationId), path: `/${path}` });
}

function pathFromUri(uri) {
  return uri?.scheme === "workspace" ? decodeURIComponent(uri.path.replace(/^\//, "")) : "";
}

function isWorkspaceModel(model) {
  return model.uri.scheme === "workspace" && model.uri.authority === String(props.conversationId);
}

function closeModel(path) {
  const model = models.get(path);
  models.delete(path);
  viewStates.delete(path);
  if (!model) return;
  queueMicrotask(() => {
    if (!model.isDisposed() && editor?.getModel() !== model) model.dispose();
  });
}

function formatDocument() {
  editor?.getAction("editor.action.formatDocument")?.run();
}

function focus() {
  editor?.focus();
}

defineExpose({ closeModel, formatDocument, focus });

function configureLanguageDefaults() {
  typescriptDefaults.setEagerModelSync(true);
  javascriptDefaults.setEagerModelSync(true);
  typescriptDefaults.setDiagnosticsOptions({ noSemanticValidation: false, noSyntaxValidation: false });
  javascriptDefaults.setDiagnosticsOptions({ noSemanticValidation: false, noSyntaxValidation: false });
}

function completionKind(kind) {
  const map = {
    module: monaco.languages.CompletionItemKind.Module,
    class: monaco.languages.CompletionItemKind.Class,
    instance: monaco.languages.CompletionItemKind.Variable,
    function: monaco.languages.CompletionItemKind.Function,
    param: monaco.languages.CompletionItemKind.Variable,
    path: monaco.languages.CompletionItemKind.File,
    keyword: monaco.languages.CompletionItemKind.Keyword,
    property: monaco.languages.CompletionItemKind.Property,
    statement: monaco.languages.CompletionItemKind.Keyword,
  };
  return map[kind] || monaco.languages.CompletionItemKind.Text;
}

function languageForPath(path) {
  const extension = String(path).split(".").pop()?.toLowerCase();
  return {
    py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
    vue: "html", html: "html", css: "css", scss: "scss", less: "less", json: "json",
    md: "markdown", yml: "yaml", yaml: "yaml", sh: "shell", ps1: "powershell", sql: "sql",
    c: "cpp", h: "cpp", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp",
    cs: "csharp", java: "java", go: "go", rs: "rust", php: "php", rb: "ruby",
    dockerfile: "dockerfile", bat: "bat", cmd: "bat",
  }[extension] || "plaintext";
}
</script>

<style scoped>
.monaco-host {
  min-width: 0;
  min-height: 0;
  width: 100%;
  height: 100%;
}
</style>
